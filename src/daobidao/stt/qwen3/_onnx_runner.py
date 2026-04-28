"""Two-stage ONNX inference for Qwen3-ASR (baicai1145 fp16 export).

Round 37 把模型库从 ``zengshuishui/Qwen3-ASR-onnx`` (int8, 3-session) 切到
``baicai1145/Qwen3-ASR-{0.6B,1.7B}-ONNX`` (fp16, 2-session)。前者 1.7B
在 offline ``transcribe()`` 路径上对特定 audio 数值组合确定性返空;后者
fp16 精度高一档,本地 + CI 实测稳过。详细背景见 docs/37-换fp16-ONNX修
1.7B-offline/PROMPT.md。

Pipeline:

    log-mel (N_MELS=128, n_frames)
        │  chunk-align + reshape → (chunks, 1, 128, window) fp16
        │  + chunk_lengths (chunks,) int64
        ▼
    encoder.onnx
        → audio_features (audio_seq, audio_output_dim) fp16   ← 无 batch 维
        ▼
    decoder.onnx   [autoregressive, static-shape KV cache]
        in:  input_ids (1, seq) int64,
             audio_features (audio_seq, dim) fp16,
             cache_position (seq,) int64,
             past_key_{00..L-1}, past_value_{00..L-1}
                  (1, kv_heads=8, cache_len=1664, head_dim=128) fp16
        out: logits (batch, vocab) fp16   ← 只输出最后一个位置
             present_key_{00..L-1}, present_value_{00..L-1}
                  (完整 cache,含历史 + 新写入)
        present_* 直接覆盖 past_* 即可(无 delta scatter)。

跟 round 26-30 设计的差异:
- conv_frontend 不再独立 — 全焊进 encoder.onnx
- KV cache axis 顺序从 (B, T, H, D) 改为 (B, H, T, D),time 在 axis=2
- decoder 输出整段 present cache(transformers HF 风格),无 delta 概念
- decoder logits 只输出最后位置 (B, vocab),不是 (B, seq, vocab) ——
  本类内部 unsqueeze 成 (B, 1, vocab),保持对外接口跟老 zengshuishui
  完全兼容(callers 仍用 ``logits[0, -1]``)
- 双 EOS [151645, 151643],由 ``self.eos_ids`` 暴露
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import onnxruntime as ort


class Qwen3ONNXRunner:
    """Load encoder + decoder ONNX (baicai1145 fp16) and expose minimal API."""

    def __init__(
        self,
        model_dir: Path,
        *,
        providers: list[str] | None = None,
    ):
        model_dir = Path(model_dir)
        meta_path = model_dir / "metadata.json"
        if not meta_path.exists():
            raise FileNotFoundError(
                f"metadata.json missing in {model_dir}; expected baicai1145 "
                f"export layout"
            )
        meta = json.loads(meta_path.read_text())

        self.dtype = np.float16 if meta["dtype"] == "float16" else np.float32
        self.num_layers = int(meta["num_layers"])
        self.max_total_len = int(meta["static_cache_len"])
        self.audio_feature_dim = int(meta["audio_output_dim"])
        self.eos_ids: tuple[int, ...] = tuple(
            int(t) for t in meta["eos_token_ids"]
        )
        # 兼容老 streaming state(它存了一个 ``eos_id`` 字段); 新代码用 eos_ids
        self.eos_id = self.eos_ids[0]
        self.encoder_window = int(meta["n_window"]) * 2

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = providers or ["CPUExecutionProvider"]

        self.encoder = ort.InferenceSession(
            str(model_dir / "encoder.onnx"),
            sess_options=so,
            providers=providers,
        )
        self.decoder = ort.InferenceSession(
            str(model_dir / "decoder.onnx"),
            sess_options=so,
            providers=providers,
        )

        # KV cache shape (B, H, T, D) — time 在 axis=2
        sample_kv = next(
            i
            for i in self.decoder.get_inputs()
            if i.name.startswith("past_key_")
        )
        self._kv_shape = list(sample_kv.shape)
        self.kv_heads = self._kv_shape[1]
        self.head_dim = self._kv_shape[3]

        self._decoder_output_names = [
            o.name for o in self.decoder.get_outputs()
        ]

    # ------------------------------------------------------------------
    # Audio encoding
    # ------------------------------------------------------------------

    def encode_audio(self, mel: np.ndarray) -> np.ndarray:
        """Encode log-mel → audio_features.

        Parameters
        ----------
        mel:
            ``(N_MELS=128, n_frames)`` float32. 任意长度 — 不需要 pad 到 30s。
            内部按 ``encoder_window`` (100 帧) chunk-align,最后一段以
            ``chunk_lengths`` 标识有效帧数。

        Returns
        -------
        np.ndarray
            ``(1, audio_seq, audio_feature_dim)`` fp16. 注意 batch 维是
            unsqueezed 出来的 — encoder 原始输出是 ``(audio_seq, dim)``,
            为了跟老 prompt-builder / streaming 调用约定一致,在这里加 1。
        """
        if mel.ndim != 2:
            raise ValueError(f"expected (N_MELS, n_frames), got {mel.shape}")
        if mel.dtype != np.float32:
            mel = mel.astype(np.float32)

        n_mels, feature_len = mel.shape
        window = self.encoder_window
        chunk_num = (feature_len + window - 1) // window
        padded_total = chunk_num * window
        if padded_total > feature_len:
            mel = np.pad(
                mel,
                ((0, 0), (0, padded_total - feature_len)),
                mode="constant",
            )

        padded_feature = (
            mel.T.reshape(chunk_num, window, n_mels).transpose(0, 2, 1)[
                :, None, :, :
            ]
        ).astype(self.dtype, copy=False)
        chunk_lengths = np.full((chunk_num,), window, dtype=np.int64)
        chunk_lengths[-1] = feature_len - window * (chunk_num - 1)

        af = self.encoder.run(
            ["audio_features"],
            {
                "padded_feature": padded_feature,
                "chunk_lengths": chunk_lengths,
            },
        )[0]
        # baicai1145 encoder 输出无 batch 维 (audio_seq, dim) — 加上 batch=1
        # 让外层 prompt builder / streaming code 以为是 (1, T, D) 一致
        return af[np.newaxis, ...]

    # ------------------------------------------------------------------
    # Decoder
    # ------------------------------------------------------------------

    def alloc_decoder_caches(self) -> list[np.ndarray]:
        """Zero-fill 28×2 个 KV cache,顺序 [k0, v0, k1, v1, ...]."""
        return [
            np.zeros(tuple(self._kv_shape), dtype=self.dtype)
            for _ in range(2 * self.num_layers)
        ]

    def decoder_step(
        self,
        input_ids: np.ndarray,
        audio_features: np.ndarray,
        caches: list[np.ndarray],
        cur_len: int,
    ) -> np.ndarray:
        """Run decoder once;``caches`` 用 present 整体覆盖。

        Parameters
        ----------
        input_ids:
            ``(1, seq)`` int64.
        audio_features:
            ``(1, audio_seq, audio_feature_dim)`` fp16/float32 — encoder 输出
            后加上的 batch 维。本方法内部 squeeze 给 decoder.onnx 用。
        caches:
            List of ``2 * num_layers`` arrays from :meth:`alloc_decoder_caches`.
            **本方法返回前会用 present 整体替换这些 array 的引用** —— callers
            必须仍然用 ``caches`` 这个 list 做下一轮调用。
        cur_len:
            当前已填的 cache 位置数(用来生成 ``cache_position``)。

        Returns
        -------
        np.ndarray
            ``(1, 1, vocab_size)``. baicai1145 的 decoder 只输出最后一位置,
            为了对外接口跟 zengshuishui 老接口兼容(prefill 时也只取
            ``logits[0, -1]``),unsqueeze axis=1 成 (1, 1, vocab)。
        """
        seq = input_ids.shape[1]
        if cur_len + seq > self.max_total_len:
            raise RuntimeError(
                f"KV cache overflow: cur_len={cur_len} + seq={seq} > "
                f"max_total_len={self.max_total_len}. Split the utterance."
            )

        # decoder 期望 audio_features 没有 batch 维
        if audio_features.ndim == 3 and audio_features.shape[0] == 1:
            audio_features_in = audio_features[0]
        else:
            audio_features_in = audio_features
        audio_features_in = audio_features_in.astype(self.dtype, copy=False)

        cache_position = np.arange(cur_len, cur_len + seq, dtype=np.int64)
        feed: dict[str, np.ndarray] = {
            "input_ids": input_ids,
            "audio_features": audio_features_in,
            "cache_position": cache_position,
        }
        for i in range(self.num_layers):
            feed[f"past_key_{i:02d}"] = caches[2 * i]
            feed[f"past_value_{i:02d}"] = caches[2 * i + 1]

        outputs = self.decoder.run(self._decoder_output_names, feed)
        out_map = dict(zip(self._decoder_output_names, outputs, strict=True))
        logits = out_map["logits"]

        # present_* 是完整新 cache,直接覆盖 caches 引用即可
        for i in range(self.num_layers):
            caches[2 * i] = out_map[f"present_key_{i:02d}"]
            caches[2 * i + 1] = out_map[f"present_value_{i:02d}"]

        # baicai1145 logits shape (B, vocab) — 加上 seq 维兼容老接口
        return logits[:, np.newaxis, :]
