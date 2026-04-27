"""Three-stage ONNX inference for Qwen3-ASR.

Pipeline (各级中间 dim 由 ONNX schema 决定,因 variant 而异:0.6B = 896 →
1024,1.7B = 1024 → 2048;统一 last dim 由 ``audio_feature_dim`` 暴露):

    log-mel (N_MELS, n_frames)
        │  transpose, add batch dim → (1, n_frames, 128)
        ▼
    conv_frontend.onnx
        → conv_output (1, n_audio_tokens, encoder_in_dim)
        ▼
    encoder.int8.onnx  (+ feature_attention_mask, all True)
        → audio_features (1, n_audio_tokens, audio_feature_dim)
        ▼
    decoder.int8.onnx   [autoregressive, static-shape KV cache]
        in:  input_ids, audio_features, attention_mask, cache_position,
             cache_key_{0..L-1}, cache_value_{0..L-1}
        out: logits, key_delta_{0..L-1}, value_delta_{0..L-1}
        We scatter ``key_delta`` / ``value_delta`` back into the cache at
        ``cache_position`` so subsequent steps reuse the growing cache.

Round 26 is offline-only (single press/release → one prefill + N decode
steps). Round 28 added chunked streaming; the ``cur_len`` / ``cache_position``
plumbing supports absolute-position addressing, which is what chunked
streaming's rollback logic needs.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnxruntime as ort


class Qwen3ONNXRunner:
    """Load conv / encoder / decoder ONNX and expose a minimal inference API."""

    # Default KV-cache time dimension. Must fit: prompt (~700 audio_pads for
    # 30s audio + ~10 chat tokens) + generation (~300 tokens max). 1200 is
    # comfortable for this round's "single key-press utterance <60s" scope.
    DEFAULT_MAX_TOTAL_LEN = 1200

    def __init__(
        self,
        model_dir: Path,
        *,
        max_total_len: int = DEFAULT_MAX_TOTAL_LEN,
        providers: list[str] | None = None,
    ):
        model_dir = Path(model_dir)
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = providers or ["CPUExecutionProvider"]

        self.conv = ort.InferenceSession(
            str(model_dir / "conv_frontend.onnx"),
            sess_options=so,
            providers=providers,
        )
        self.encoder = ort.InferenceSession(
            str(model_dir / "encoder.int8.onnx"),
            sess_options=so,
            providers=providers,
        )
        self.decoder = ort.InferenceSession(
            str(model_dir / "decoder.int8.onnx"),
            sess_options=so,
            providers=providers,
        )

        self.max_total_len = max_total_len
        (
            self.num_layers,
            self.kv_heads,
            self.head_dim,
        ) = self._inspect_decoder()
        self.audio_feature_dim = self._inspect_audio_feature_dim()

        # Pre-compute output-name list so decoder_step doesn't rebuild it.
        self._decoder_output_names = ["logits"]
        for i in range(self.num_layers):
            self._decoder_output_names.append(f"key_delta_{i}")
            self._decoder_output_names.append(f"value_delta_{i}")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def _inspect_decoder(self) -> tuple[int, int, int]:
        """Return (num_layers, kv_heads, head_dim) derived from decoder inputs.

        Falls back to the spike-confirmed values (28 / 8 / 128) if the ONNX
        graph reports symbolic dims instead of integers.
        """
        key_inputs = [
            inp
            for inp in self.decoder.get_inputs()
            if inp.name.startswith("cache_key_")
        ]
        if not key_inputs:
            raise RuntimeError(
                "decoder.onnx has no cache_key_* inputs; "
                "unexpected graph layout"
            )
        num_layers = len(key_inputs)
        shape = key_inputs[0].shape
        kv_heads = shape[2] if isinstance(shape[2], int) else 8
        head_dim = shape[3] if isinstance(shape[3], int) else 128
        return num_layers, kv_heads, head_dim

    def _inspect_audio_feature_dim(self) -> int:
        """Return last-dim of decoder's ``audio_features`` input.

        0.6B → 1024, 1.7B → 2048. Reading from the decoder's input schema
        is the single source of truth — anything else (encoder output dim,
        a hard-coded constant per variant) duplicates this and drifts.
        """
        for inp in self.decoder.get_inputs():
            if inp.name == "audio_features":
                last = inp.shape[-1]
                if not isinstance(last, int):
                    raise RuntimeError(
                        f"decoder.onnx audio_features shape last dim is "
                        f"symbolic ({last!r}); cannot infer feature dim"
                    )
                return last
        raise RuntimeError(
            "decoder.onnx has no `audio_features` input; "
            "unexpected graph layout"
        )

    # ------------------------------------------------------------------
    # Audio encoding
    # ------------------------------------------------------------------

    def encode_audio(self, mel: np.ndarray) -> np.ndarray:
        """Run conv_frontend + encoder on a log-mel spectrogram.

        Parameters
        ----------
        mel:
            ``(N_MELS, n_frames)`` float32, as produced by
            ``_feature.log_mel_spectrogram``.

        Returns
        -------
        np.ndarray
            ``(1, n_audio_tokens, audio_feature_dim)`` float32.
            ``n_audio_tokens`` is determined by the conv stride inside the
            ONNX graph; ``audio_feature_dim`` is variant-specific (0.6B =
            1024, 1.7B = 2048; available as ``self.audio_feature_dim``).
        """
        if mel.ndim != 2:
            raise ValueError(f"expected (N_MELS, n_frames), got {mel.shape}")
        if mel.dtype != np.float32:
            mel = mel.astype(np.float32)

        # conv_frontend wants (batch, n_frames, n_mels=128)
        conv_input = mel.T[np.newaxis, ...]
        conv_output = self.conv.run(
            ["conv_output"], {"input_features": conv_input}
        )[0]

        n_audio = conv_output.shape[1]
        feature_attention_mask = np.ones((1, n_audio), dtype=bool)
        audio_features = self.encoder.run(
            ["audio_features"],
            {
                "input_features": conv_output,
                "feature_attention_mask": feature_attention_mask,
            },
        )[0]
        return audio_features

    # ------------------------------------------------------------------
    # Decoder
    # ------------------------------------------------------------------

    def alloc_decoder_caches(self) -> list[np.ndarray]:
        """Return zero-filled KV cache tensors, ordered

        ``[key_0, value_0, key_1, value_1, ..., key_{L-1}, value_{L-1}]``.
        """
        return [
            np.zeros(
                (1, self.max_total_len, self.kv_heads, self.head_dim),
                dtype=np.float32,
            )
            for _ in range(2 * self.num_layers)
        ]

    def decoder_step(
        self,
        input_ids: np.ndarray,
        audio_features: np.ndarray,
        caches: list[np.ndarray],
        cur_len: int,
    ) -> np.ndarray:
        """Run the decoder once; write KV deltas back into ``caches`` in-place.

        Parameters
        ----------
        input_ids:
            ``(1, seq)`` int64 — new tokens to process.
        audio_features:
            ``(1, n_audio_tokens, audio_feature_dim)`` float32 — from
            :meth:`encode_audio`.
        caches:
            List of 2L arrays allocated by :meth:`alloc_decoder_caches`.
            Slots ``0..cur_len-1`` along axis=1 hold the history; slots
            ``cur_len..cur_len+seq-1`` will be overwritten with the new
            deltas.
        cur_len:
            Number of cache positions already filled.

        Returns
        -------
        np.ndarray
            logits ``(1, seq, vocab_size)``.
        """
        seq = input_ids.shape[1]
        if cur_len + seq > self.max_total_len:
            raise RuntimeError(
                f"KV cache overflow: cur_len={cur_len} + seq={seq} > "
                f"max_total_len={self.max_total_len}. Split the utterance."
            )

        attention_mask = np.ones(input_ids.shape, dtype=np.int64)
        cache_position = np.arange(cur_len, cur_len + seq, dtype=np.int64)

        feed: dict[str, np.ndarray] = {
            "input_ids": input_ids,
            "audio_features": audio_features,
            "attention_mask": attention_mask,
            "cache_position": cache_position,
        }
        for i in range(self.num_layers):
            feed[f"cache_key_{i}"] = caches[2 * i]
            feed[f"cache_value_{i}"] = caches[2 * i + 1]

        outputs = self.decoder.run(self._decoder_output_names, feed)
        logits = outputs[0]

        # Scatter key/value deltas into the pre-allocated cache.
        end = cur_len + seq
        for i in range(self.num_layers):
            key_delta = outputs[1 + 2 * i]
            value_delta = outputs[1 + 2 * i + 1]
            caches[2 * i][:, cur_len:end, :, :] = key_delta
            caches[2 * i + 1][:, cur_len:end, :, :] = value_delta

        return logits
