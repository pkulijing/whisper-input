"""Tests for ``daobidao.stt.qwen3._feature``.

The golden test compares our numpy-only ``log_mel_spectrogram`` output to a
frozen snapshot produced by ``transformers.WhisperFeatureExtractor`` on
``tests/fixtures/zh.wav`` (see ``scripts/generate_whisper_mel_golden.py``).

If the golden test fails, the log-mel drifted from Whisper's reference — the
Qwen3 encoder was trained against Whisper features, so any drift will silently
degrade recognition quality. Re-run the generator, inspect the diff, and only
update the golden file after confirming the drift is intentional.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from daobidao.stt.qwen3._feature import (
    HOP_LENGTH,
    N_FFT,
    N_MELS,
    N_SAMPLES,
    SAMPLE_RATE,
    _hann_window,
    _hz_to_mel_slaney,
    _mel_filter_bank,
    _mel_to_hz_slaney,
    log_mel_spectrogram,
    pad_or_trim,
)

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_PATH = FIXTURES / "whisper_mel_golden_zh.npy"
ZH_WAV = FIXTURES / "zh.wav"


# --------------------------------------------------------------------------
# Mel scale conversion (Slaney)
# --------------------------------------------------------------------------


def test_hz_to_mel_zero():
    assert _hz_to_mel_slaney(np.array(0.0)) == pytest.approx(0.0)


def test_hz_to_mel_linear_region():
    # Below 1000 Hz: linear at 3 mels per 200 Hz = 0.015 mel/Hz
    assert _hz_to_mel_slaney(np.array(200.0)) == pytest.approx(3.0)
    assert _hz_to_mel_slaney(np.array(600.0)) == pytest.approx(9.0)


def test_hz_to_mel_log_region():
    # At 1000 Hz exactly: 15 mels (boundary)
    assert _hz_to_mel_slaney(np.array(1000.0)) == pytest.approx(15.0)
    # Above 1000 Hz: log growth, monotonic increasing
    assert _hz_to_mel_slaney(np.array(2000.0)) > 15.0
    assert _hz_to_mel_slaney(np.array(4000.0)) > _hz_to_mel_slaney(
        np.array(2000.0)
    )


def test_mel_to_hz_inverse_of_hz_to_mel():
    hz = np.array([0.0, 100.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0])
    mel = _hz_to_mel_slaney(hz)
    recovered = _mel_to_hz_slaney(mel)
    assert np.allclose(recovered, hz, rtol=1e-6, atol=1e-4)


# --------------------------------------------------------------------------
# Hann window
# --------------------------------------------------------------------------


def test_hann_window_shape_and_endpoints():
    w = _hann_window(N_FFT)
    assert w.shape == (N_FFT,)
    assert w.dtype == np.float32
    # Periodic Hann (length N) starts at 0 and does NOT return to 0 at the end
    assert w[0] == pytest.approx(0.0, abs=1e-6)
    assert w[N_FFT // 2] == pytest.approx(1.0, abs=1e-6)
    # Last sample is 0.5 - 0.5*cos(2*pi*(N-1)/N), non-zero for periodic
    assert w[-1] > 0.0


def test_hann_window_symmetry_about_center():
    # Periodic Hann is symmetric about N/2 for indices 1..N-1
    w = _hann_window(N_FFT)
    half = N_FFT // 2
    for i in range(1, half):
        assert w[half - i] == pytest.approx(w[half + i], abs=1e-6)


# --------------------------------------------------------------------------
# Mel filter bank
# --------------------------------------------------------------------------


def test_mel_filter_bank_shape():
    fb = _mel_filter_bank()
    assert fb.shape == (N_FFT // 2 + 1, N_MELS)
    assert fb.dtype == np.float32


def test_mel_filter_bank_non_negative():
    fb = _mel_filter_bank()
    assert (fb >= 0.0).all()


def test_mel_filter_bank_sparse_triangular():
    # Each filter is a triangle: most FFT bins should have zero weight
    fb = _mel_filter_bank()
    nonzero_per_filter = (fb > 0).sum(axis=0)
    # Every filter touches at least one bin
    assert (nonzero_per_filter > 0).all()
    # No filter should be dense (low-frequency ones are 2-3 bins wide)
    assert nonzero_per_filter.min() < 10


# --------------------------------------------------------------------------
# pad_or_trim
# --------------------------------------------------------------------------


def test_pad_or_trim_pads_short_audio():
    audio = np.ones(1000, dtype=np.float32)
    out = pad_or_trim(audio, length=5000)
    assert out.shape == (5000,)
    assert np.all(out[:1000] == 1.0)
    assert np.all(out[1000:] == 0.0)


def test_pad_or_trim_truncates_long_audio():
    audio = np.arange(100, dtype=np.float32)
    out = pad_or_trim(audio, length=50)
    assert out.shape == (50,)
    assert np.array_equal(out, np.arange(50, dtype=np.float32))


def test_pad_or_trim_exact_length_is_noop():
    audio = np.arange(50, dtype=np.float32)
    out = pad_or_trim(audio, length=50)
    assert np.array_equal(out, audio)


# --------------------------------------------------------------------------
# log_mel_spectrogram — shape + value-range sanity
# --------------------------------------------------------------------------


def test_log_mel_shape_30s():
    audio = np.zeros(N_SAMPLES, dtype=np.float32)
    mel = log_mel_spectrogram(audio)
    assert mel.shape == (N_MELS, 3000)
    assert mel.dtype == np.float32


def test_log_mel_shape_arbitrary_length():
    # 1 second at 16 kHz → center-padded STFT gives 101 frames (last dropped)
    audio = np.zeros(SAMPLE_RATE, dtype=np.float32)
    mel = log_mel_spectrogram(audio)
    # n_frames = 1 + (N_SAMPLES + N_FFT - N_FFT) // HOP_LENGTH - 1 pattern
    expected = 1 + (SAMPLE_RATE + 2 * (N_FFT // 2) - N_FFT) // HOP_LENGTH - 1
    assert mel.shape == (N_MELS, expected)


def test_log_mel_silence_is_bounded():
    audio = np.zeros(N_SAMPLES, dtype=np.float32)
    mel = log_mel_spectrogram(audio)
    # With log10 floor 1e-10 and (+4)/4 normalization, silence → -1.5
    # After clipping to max-8: silence mel value ≈ (-10 + 4)/4 = -1.5
    assert mel.max() <= 1.5 + 1e-6
    assert mel.min() >= -2.0


def test_log_mel_rejects_nonfloat32_gracefully():
    # Input is int16, should be coerced internally
    audio = (np.random.RandomState(0).randn(SAMPLE_RATE) * 1000).astype(
        np.int16
    )
    mel = log_mel_spectrogram(audio.astype(np.float32) / 32768.0)
    assert mel.shape[0] == N_MELS
    assert np.isfinite(mel).all()


def test_log_mel_coerces_float64_input():
    # Pass float64 directly; log_mel_spectrogram must cast internally
    rng = np.random.RandomState(42)
    audio64 = rng.randn(SAMPLE_RATE).astype(np.float64) * 0.1
    mel = log_mel_spectrogram(audio64)
    assert mel.dtype == np.float32
    # Result must match the float32-coerced path bit-for-bit
    mel_ref = log_mel_spectrogram(audio64.astype(np.float32))
    np.testing.assert_array_equal(mel, mel_ref)


def test_log_mel_rejects_non_1d():
    audio = np.zeros((2, N_SAMPLES), dtype=np.float32)
    with pytest.raises(ValueError, match="1D"):
        log_mel_spectrogram(audio)


# --------------------------------------------------------------------------
# GOLDEN TEST — must match transformers.WhisperFeatureExtractor output
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not GOLDEN_PATH.exists(),
    reason=(
        "Golden file not generated; run "
        "`uv run --with transformers --with torch python "
        "scripts/generate_whisper_mel_golden.py`"
    ),
)
def test_log_mel_matches_whisper_golden():
    audio, sr = sf.read(str(ZH_WAV), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    assert sr == SAMPLE_RATE

    padded = pad_or_trim(audio)
    mel = log_mel_spectrogram(padded)

    golden = np.load(GOLDEN_PATH)
    assert mel.shape == golden.shape, (
        f"shape mismatch: ours {mel.shape} vs golden {golden.shape}"
    )
    # Allow fp32 noise but require tight alignment — drift > 1e-3 means the
    # algorithm is wrong, not floating-point noise.
    max_abs_diff = float(np.abs(mel - golden).max())
    assert np.allclose(mel, golden, rtol=1e-4, atol=1e-5), (
        f"log-mel drifted from Whisper golden; max |diff| = {max_abs_diff}"
    )
