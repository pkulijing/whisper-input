"""Qwen3-ASR fp16 ONNX backend(``baicai1145/Qwen3-ASR-{0.6B,1.7B}-ONNX``,round 37+)。

2 个 ONNX session(encoder + decoder,conv_frontend 焊进 encoder),静态 KV
cache ``(B, H, T=1664, D)`` float16,输出整段 ``present_*`` 直接覆盖。
chunk-aligned encoder 接受任意长度 mel,无需 30s pad。双 EOS = (151645, 151643)。

跟产品 ``src/daobidao/stt/qwen3/_onnx_runner.py`` 是平行实现 —— 那边是产品
代码,这里是 benchmark adapter。两边可以独立演化(产品代码加性能优化、
adapter 维持 baseline 对照)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort

from benchmarks.backends.base import Backend
from daobidao.stt.qwen3._feature import log_mel_spectrogram
from daobidao.stt.qwen3._tokenizer import Qwen3Tokenizer

CACHE_ROOT_BY_VARIANT = {
    "0.6B": Path.home()
    / ".cache/modelscope/hub/models/baicai1145/Qwen3-ASR-0___6B-ONNX",
    "1.7B": Path.home()
    / ".cache/modelscope/hub/models/baicai1145/Qwen3-ASR-1___7B-ONNX",
}


def _make_padded_feature(
    audio: np.ndarray, window: int, dtype: np.dtype
) -> tuple[np.ndarray, np.ndarray]:
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)
    mel = log_mel_spectrogram(audio)
    n_mels, feature_len = mel.shape
    chunk_num = (feature_len + window - 1) // window
    padded_total = chunk_num * window
    if padded_total > feature_len:
        mel = np.pad(
            mel, ((0, 0), (0, padded_total - feature_len)), mode="constant"
        )
    padded = (
        mel.T.reshape(chunk_num, window, n_mels).transpose(0, 2, 1)[
            :, None, :, :
        ]
    ).astype(dtype, copy=False)
    chunk_lengths = np.full((chunk_num,), window, dtype=np.int64)
    chunk_lengths[-1] = feature_len - window * (chunk_num - 1)
    return padded, chunk_lengths


class Qwen3Fp16Backend:
    """符合 ``benchmarks.backends.base.Backend`` 协议。"""

    def __init__(self, variant: str) -> None:
        if variant not in CACHE_ROOT_BY_VARIANT:
            raise ValueError(f"未知 variant {variant!r}")
        self.variant = variant
        self.family = "qwen3"
        self.quant = "fp16"
        self.name = f"qwen3-fp16-baicai1145-{variant}"
        self._cache_root = CACHE_ROOT_BY_VARIANT[variant]
        self._loaded = False
        self.tokenizer: Qwen3Tokenizer | None = None
        self.encoder: ort.InferenceSession | None = None
        self.decoder: ort.InferenceSession | None = None
        # eos_ids 在 load() 后从 metadata.json 填充
        self.eos_ids: set[int] = set()

    def load(self) -> None:
        if self._loaded:
            return
        if not self._cache_root.exists():
            raise RuntimeError(
                f"fp16 cache 未找到 {self._cache_root}。本 adapter 不联网,请先"
                " run daobidao(或 modelscope CLI 拉 baicai1145/Qwen3-ASR-"
                f"{self.variant}-ONNX)预下载"
            )

        meta = json.loads((self._cache_root / "metadata.json").read_text())
        self._dtype = np.float16 if meta["dtype"] == "float16" else np.float32
        self._num_layers = int(meta["num_layers"])
        self._cache_len = int(meta["static_cache_len"])
        self._audio_output_dim = int(meta["audio_output_dim"])
        self.eos_ids = set(meta["eos_token_ids"])
        self._window = int(meta["n_window"]) * 2

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ["CPUExecutionProvider"]
        self.encoder = ort.InferenceSession(
            str(self._cache_root / "encoder.onnx"),
            sess_options=so,
            providers=providers,
        )
        self.decoder = ort.InferenceSession(
            str(self._cache_root / "decoder.onnx"),
            sess_options=so,
            providers=providers,
        )
        sample_in = next(
            i
            for i in self.decoder.get_inputs()
            if i.name.startswith("past_key_")
        )
        self._kv_shape = list(sample_in.shape)

        self.tokenizer = Qwen3Tokenizer(self._cache_root)
        self._loaded = True

    # Backend API ------------------------------------------------------

    def encode_audio(self, audio: np.ndarray) -> np.ndarray:
        assert self.encoder is not None
        padded, lengths = _make_padded_feature(audio, self._window, self._dtype)
        out = self.encoder.run(
            ["audio_features"],
            {"padded_feature": padded, "chunk_lengths": lengths},
        )[0]
        return out

    def alloc_caches(self) -> dict[str, np.ndarray]:
        shape = tuple(self._kv_shape)
        caches: dict[str, np.ndarray] = {}
        for i in range(self._num_layers):
            caches[f"past_key_{i:02d}"] = np.zeros(shape, dtype=self._dtype)
            caches[f"past_value_{i:02d}"] = np.zeros(shape, dtype=self._dtype)
        return caches

    def decoder_step(
        self,
        input_ids: np.ndarray,
        audio_features: np.ndarray,
        caches: Any,
        cache_position: np.ndarray,
    ) -> np.ndarray:
        assert self.decoder is not None
        feed: dict[str, np.ndarray] = {
            "input_ids": input_ids,
            "audio_features": audio_features,
            "cache_position": cache_position,
            **caches,
        }
        names = [o.name for o in self.decoder.get_outputs()]
        outs = self.decoder.run(names, feed)
        out_map = dict(zip(names, outs, strict=True))
        for i in range(self._num_layers):
            caches[f"past_key_{i:02d}"] = out_map[f"present_key_{i:02d}"]
            caches[f"past_value_{i:02d}"] = out_map[f"present_value_{i:02d}"]
        logits = out_map["logits"]
        if logits.ndim == 2:
            logits = logits[:, None, :]
        return logits


def discover() -> list[Backend]:
    return [
        Qwen3Fp16Backend("0.6B"),
        Qwen3Fp16Backend("1.7B"),
    ]
