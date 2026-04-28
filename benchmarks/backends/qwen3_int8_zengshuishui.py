"""Qwen3-ASR int8 ONNX backend(``zengshuishui/Qwen3-ASR-onnx``,round 26-36)。

3 个 ONNX session(conv_frontend + encoder.int8 + decoder.int8),静态 KV
cache ``(B, T=1200, H, D)`` float32,scatter delta 写回。30s 音频 pad 是
强制的(否则 conv_frontend 拒绝)。单 EOS = 151645 (``<|im_end|>``)。

Round 37 切到 fp16 后这套 runner 已从产品代码移除;此 adapter 是为了
benchmark 保留的对照实现,**仅依赖本地 cache,不联网**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort

from benchmarks.backends.base import Backend
from daobidao.stt.qwen3._feature import log_mel_spectrogram
from daobidao.stt.qwen3._tokenizer import Qwen3Tokenizer

SAMPLE_RATE = 16000
PAD_TARGET_SECONDS = 30
MAX_TOTAL_LEN = 1200
EOS_IDS: set[int] = {151645}

CACHE_ROOT = (
    Path.home() / ".cache/modelscope/hub/models/zengshuishui/Qwen3-ASR-onnx"
)
TOKENIZER_DIR = CACHE_ROOT / "tokenizer"
MODEL_DIR_BY_VARIANT = {
    "0.6B": CACHE_ROOT / "model_0.6B",
    "1.7B": CACHE_ROOT / "model_1.7B",
}


class Qwen3Int8Backend:
    """符合 ``benchmarks.backends.base.Backend`` 协议。"""

    def __init__(self, variant: str) -> None:
        if variant not in MODEL_DIR_BY_VARIANT:
            raise ValueError(f"未知 variant {variant!r}")
        self.variant = variant
        self.family = "qwen3"
        self.quant = "int8"
        self.name = f"qwen3-int8-zengshuishui-{variant}"
        self.eos_ids = EOS_IDS
        self._model_dir = MODEL_DIR_BY_VARIANT[variant]
        self._loaded = False
        self.tokenizer: Qwen3Tokenizer | None = None
        self.conv: ort.InferenceSession | None = None
        self.encoder: ort.InferenceSession | None = None
        self.decoder: ort.InferenceSession | None = None

    def load(self) -> None:
        if self._loaded:
            return
        if not self._model_dir.exists():
            raise RuntimeError(
                f"int8 cache 未找到 {self._model_dir}。本 adapter 不联网,请先用旧"
                " round 26-36 代码 / spike 脚本下载,或 modelscope CLI 拉 "
                "zengshuishui/Qwen3-ASR-onnx"
            )
        if not TOKENIZER_DIR.exists():
            raise RuntimeError(f"int8 tokenizer dir 缺 {TOKENIZER_DIR}")

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ["CPUExecutionProvider"]
        self.conv = ort.InferenceSession(
            str(self._model_dir / "conv_frontend.onnx"),
            sess_options=so,
            providers=providers,
        )
        self.encoder = ort.InferenceSession(
            str(self._model_dir / "encoder.int8.onnx"),
            sess_options=so,
            providers=providers,
        )
        self.decoder = ort.InferenceSession(
            str(self._model_dir / "decoder.int8.onnx"),
            sess_options=so,
            providers=providers,
        )

        key_inputs = [
            i
            for i in self.decoder.get_inputs()
            if i.name.startswith("cache_key_")
        ]
        self._num_layers = len(key_inputs)
        shape = key_inputs[0].shape
        self._kv_heads = shape[2] if isinstance(shape[2], int) else 8
        self._head_dim = shape[3] if isinstance(shape[3], int) else 128

        self._decoder_output_names = ["logits"]
        for i in range(self._num_layers):
            self._decoder_output_names.append(f"key_delta_{i}")
            self._decoder_output_names.append(f"value_delta_{i}")

        self.tokenizer = Qwen3Tokenizer(TOKENIZER_DIR)
        self._loaded = True

    # Backend API ------------------------------------------------------

    def encode_audio(self, audio: np.ndarray) -> np.ndarray:
        assert self.conv is not None and self.encoder is not None
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        target = PAD_TARGET_SECONDS * SAMPLE_RATE
        if len(audio) < target:
            audio = np.pad(audio, (0, target - len(audio)))
        elif len(audio) > target:
            audio = audio[:target]
        mel = log_mel_spectrogram(audio).astype(np.float32)
        conv_input = mel.T[np.newaxis, ...]
        conv_output = self.conv.run(
            ["conv_output"], {"input_features": conv_input}
        )[0]
        n_audio = conv_output.shape[1]
        feature_attention_mask = np.ones((1, n_audio), dtype=bool)
        return self.encoder.run(
            ["audio_features"],
            {
                "input_features": conv_output,
                "feature_attention_mask": feature_attention_mask,
            },
        )[0]

    def alloc_caches(self) -> list[np.ndarray]:
        return [
            np.zeros(
                (1, MAX_TOTAL_LEN, self._kv_heads, self._head_dim),
                dtype=np.float32,
            )
            for _ in range(2 * self._num_layers)
        ]

    def decoder_step(
        self,
        input_ids: np.ndarray,
        audio_features: np.ndarray,
        caches: Any,
        cache_position: np.ndarray,
    ) -> np.ndarray:
        assert self.decoder is not None
        cur_len = int(cache_position[0])
        seq = input_ids.shape[1]
        if cur_len + seq > MAX_TOTAL_LEN:
            raise RuntimeError(
                f"int8 KV cache overflow cur_len={cur_len}+seq={seq}>{MAX_TOTAL_LEN}"
            )
        attention_mask = np.ones(input_ids.shape, dtype=np.int64)
        feed: dict[str, np.ndarray] = {
            "input_ids": input_ids,
            "audio_features": audio_features,
            "attention_mask": attention_mask,
            "cache_position": cache_position,
        }
        for i in range(self._num_layers):
            feed[f"cache_key_{i}"] = caches[2 * i]
            feed[f"cache_value_{i}"] = caches[2 * i + 1]

        outputs = self.decoder.run(self._decoder_output_names, feed)
        logits = outputs[0]
        end = cur_len + seq
        for i in range(self._num_layers):
            caches[2 * i][:, cur_len:end, :, :] = outputs[1 + 2 * i]
            caches[2 * i + 1][:, cur_len:end, :, :] = outputs[1 + 2 * i + 1]
        return logits


def discover() -> list[Backend]:
    """枚举本 adapter 可提供的所有 backend instance(load 在 harness 里再做)。"""
    return [
        Qwen3Int8Backend("0.6B"),
        Qwen3Int8Backend("1.7B"),
    ]
