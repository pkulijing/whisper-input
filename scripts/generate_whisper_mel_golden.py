"""Generate ``tests/fixtures/whisper_mel_golden_zh.npy``.

Dev-only one-shot script: runs ``transformers.WhisperFeatureExtractor`` on the
same audio file used in our STT smoke test, and saves the log-mel output as a
numpy archive. ``tests/test_qwen3_feature.py`` will then compare our
``log_mel_spectrogram`` implementation against this golden file with
``np.allclose(rtol=1e-4, atol=1e-5)``.

``transformers`` is NOT a project runtime dependency. Install it ad-hoc:

    uv run --with transformers --with torch \
        python scripts/generate_whisper_mel_golden.py

``torch`` is pulled only because ``transformers`` imports it at top level; our
usage is numpy-only.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

REPO_ROOT = Path(__file__).resolve().parent.parent
WAV = REPO_ROOT / "tests" / "fixtures" / "zh.wav"
OUT = REPO_ROOT / "tests" / "fixtures" / "whisper_mel_golden_zh.npy"


def main() -> None:
    # Imported lazily so the module is importable without the dev deps.
    from transformers import WhisperFeatureExtractor

    audio, sr = sf.read(str(WAV), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    assert sr == 16000, f"zh.wav must be 16 kHz, got {sr}"

    # Match Qwen3-ASR's preprocessor_config.json values.
    extractor = WhisperFeatureExtractor(
        feature_size=128,
        sampling_rate=16000,
        hop_length=160,
        n_fft=400,
        chunk_length=30,
        padding_value=0.0,
        dither=0.0,
    )
    inputs = extractor(
        audio,
        sampling_rate=16000,
        return_tensors="np",
        padding="max_length",
        truncation=True,
    )
    mel = inputs["input_features"][0]  # (128, 3000)
    assert mel.shape == (128, 3000), f"unexpected shape: {mel.shape}"
    print(f"mel shape: {mel.shape}, dtype: {mel.dtype}")
    print(
        f"mel stats: min={mel.min():.4f} max={mel.max():.4f} "
        f"mean={mel.mean():.4f}"
    )

    np.save(OUT, mel.astype(np.float32))
    print(f"saved golden mel to {OUT}")


if __name__ == "__main__":
    main()
