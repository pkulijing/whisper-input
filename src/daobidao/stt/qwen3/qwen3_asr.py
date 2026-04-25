"""Qwen3-ASR STT backend (offline + streaming).

Press-and-hold 离线路径(26 轮):

    load(): snapshot_download → build Qwen3ONNXRunner + Qwen3Tokenizer →
            run a one-off warmup so the first real transcription doesn't
            include graph-init overhead.

    transcribe(wav_bytes):
        1. decode wav → float32 mono 16 kHz
        2. pad/trim to 30s, log-mel spectrogram
        3. encode_audio → audio_features
        4. build chat-template prompt with N audio_pads (N = audio-token len)
        5. decoder prefill + greedy generation until <|im_end|>
        6. decode + postprocess → final transcript

流式路径(28 轮,策略 E):详见 ``_stream.py``。本类暴露
``init_stream_state()`` / ``stream_step()``;离线 ``transcribe()`` 保留,
用户在设置页关流式时走它。
"""

from __future__ import annotations

import io
import time
import wave
from pathlib import Path
from typing import ClassVar, Literal

import numpy as np

from daobidao.logger import get_logger
from daobidao.stt.base import BaseSTT, StreamEvent
from daobidao.stt.qwen3._feature import (
    SAMPLE_RATE,
    log_mel_spectrogram,
    pad_or_trim,
)
from daobidao.stt.qwen3._onnx_runner import Qwen3ONNXRunner
from daobidao.stt.qwen3._postprocess import parse_asr_output
from daobidao.stt.qwen3._prompt import build_prompt
from daobidao.stt.qwen3._stream import (
    Qwen3StreamState,
)
from daobidao.stt.qwen3._stream import (
    init_stream_state as _init_stream_state,
)
from daobidao.stt.qwen3._stream import (
    stream_step as _stream_step,
)
from daobidao.stt.qwen3._tokenizer import Qwen3Tokenizer

logger = get_logger(__name__)

REPO_ID = "zengshuishui/Qwen3-ASR-onnx"
Variant = Literal["0.6B", "1.7B"]
VALID_VARIANTS: tuple[Variant, ...] = ("0.6B", "1.7B")

# Upper bound on tokens generated per utterance. Based on empirical check: a
# 10s Chinese sample emits ~30 tokens; 60s ≤ ~250. 400 gives plenty of slack
# without risking runaway generation.
_MAX_NEW_TOKENS = 400

# Minimum audio duration (0.1s). Below this we skip inference — the user
# probably tapped the hotkey by accident.
_MIN_SAMPLES = int(SAMPLE_RATE * 0.1)


class Qwen3ASRSTT(BaseSTT):
    """Qwen3-ASR int8 ONNX inference, 支持离线 + 流式(策略 E)。"""

    supports_streaming: ClassVar[bool] = True

    def __init__(self, variant: str = "0.6B"):
        if variant not in VALID_VARIANTS:
            raise ValueError(
                f"unknown variant {variant!r}; expected one of {VALID_VARIANTS}"
            )
        self.variant: Variant = variant  # type: ignore[assignment]
        self.cache_root: Path | None = None
        self._runner: Qwen3ONNXRunner | None = None
        self._tokenizer: Qwen3Tokenizer | None = None

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self) -> None:
        if self._runner is not None and self._tokenizer is not None:
            return

        logger.info("qwen3_asr_loading", variant=self.variant)

        from modelscope import snapshot_download

        t0 = time.perf_counter()
        logger.info("qwen3_snapshot_start", variant=self.variant)
        allow_patterns = [
            f"model_{self.variant}/conv_frontend.onnx",
            f"model_{self.variant}/encoder.int8.onnx",
            f"model_{self.variant}/decoder.int8.onnx",
            "tokenizer/*",
        ]
        self.cache_root = Path(
            snapshot_download(REPO_ID, allow_patterns=allow_patterns)
        )
        logger.info(
            "qwen3_snapshot_done",
            variant=self.variant,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

        t0 = time.perf_counter()
        logger.info("qwen3_runner_start")
        self._runner = Qwen3ONNXRunner(
            self.cache_root / f"model_{self.variant}"
        )
        logger.info(
            "qwen3_runner_ready",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

        t0 = time.perf_counter()
        self._tokenizer = Qwen3Tokenizer(self.cache_root / "tokenizer")
        logger.info(
            "qwen3_tokenizer_ready",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

        t0 = time.perf_counter()
        logger.info("qwen3_warmup_start")
        self._warmup()
        logger.info(
            "qwen3_warmup_done",
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

        logger.info("qwen3_asr_loaded", variant=self.variant)

    def _warmup(self) -> None:
        """Run one tiny forward pass so the first real call isn't cold."""
        assert self._runner is not None
        assert self._tokenizer is not None

        # 0.5s of silence is enough to let ORT finalize graph partitioning.
        audio = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
        padded = pad_or_trim(audio)
        mel = log_mel_spectrogram(padded)
        audio_features = self._runner.encode_audio(mel)

        prompt = build_prompt(audio_features.shape[1])
        input_ids = np.array(
            [self._tokenizer.encode(prompt)], dtype=np.int64
        )
        caches = self._runner.alloc_decoder_caches()
        self._runner.decoder_step(
            input_ids, audio_features, caches, cur_len=0
        )

    # ------------------------------------------------------------------
    # Transcribe
    # ------------------------------------------------------------------

    def transcribe(self, wav_data: bytes) -> str:
        if not wav_data:
            return ""
        self.load()
        assert self._runner is not None and self._tokenizer is not None

        audio = _wav_bytes_to_float32(wav_data)
        if len(audio) < _MIN_SAMPLES:
            return ""

        padded = pad_or_trim(audio)
        mel = log_mel_spectrogram(padded)
        audio_features = self._runner.encode_audio(mel)

        prompt = build_prompt(audio_features.shape[1])
        prompt_ids = self._tokenizer.encode(prompt)
        input_ids = np.array([prompt_ids], dtype=np.int64)

        caches = self._runner.alloc_decoder_caches()
        logits = self._runner.decoder_step(
            input_ids, audio_features, caches, cur_len=0
        )
        cur_len = len(prompt_ids)

        eos_id = self._tokenizer.eos_id
        generated: list[int] = []
        for _ in range(_MAX_NEW_TOKENS):
            next_id = int(np.argmax(logits[0, -1]))
            if next_id == eos_id:
                break
            generated.append(next_id)
            next_input = np.array([[next_id]], dtype=np.int64)
            logits = self._runner.decoder_step(
                next_input, audio_features, caches, cur_len
            )
            cur_len += 1

        raw = self._tokenizer.decode(generated, skip_special_tokens=True)
        return parse_asr_output(raw)

    # ------------------------------------------------------------------
    # Streaming(策略 E,详见 _stream.py)
    # ------------------------------------------------------------------

    def init_stream_state(self) -> Qwen3StreamState:
        """为一次按键→说话→松手周期初始化状态。"""
        self.load()
        assert self._runner is not None and self._tokenizer is not None
        return _init_stream_state(self._runner, self._tokenizer)

    def stream_step(
        self,
        audio_chunk: np.ndarray,
        state: Qwen3StreamState,
        is_last: bool,
    ) -> StreamEvent:
        """增量喂一段音频 chunk;具体算法见 ``_stream.py``。

        ``audio_chunk`` 应该是 float32 1D array,16 kHz 单声道。空数组合法
        (用于纯 flush 场景)。
        """
        assert self._runner is not None and self._tokenizer is not None
        return _stream_step(
            state,
            audio_chunk,
            is_last,
            runner=self._runner,
            tokenizer=self._tokenizer,
        )


# --------------------------------------------------------------------------
# WAV byte decoding
# --------------------------------------------------------------------------

def _wav_bytes_to_float32(wav_data: bytes) -> np.ndarray:
    """Decode a 16 kHz 16-bit mono WAV blob to float32 [-1, 1] 1D array."""
    buf = io.BytesIO(wav_data)
    with wave.open(buf, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
