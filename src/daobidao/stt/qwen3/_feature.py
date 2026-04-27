"""Whisper-style log-mel spectrogram, implemented in numpy only.

Must match ``transformers.WhisperFeatureExtractor`` bit-for-bit (within fp32
noise) because Qwen3-ASR was trained on Whisper-style features. The reference
implementation in transformers lives in
``transformers/models/whisper/feature_extraction_whisper.py`` and
``transformers/audio_utils.py`` (functions ``mel_filter_bank``,
``spectrogram``, ``window_function``).

Key parameters (from Qwen3-ASR's ``preprocessor_config.json``):
    n_fft=400, hop_length=160, n_mels=128, sampling_rate=16000,
    chunk_length=30s, mel_scale="slaney", norm="slaney",
    window="hann" (periodic), center=True, pad_mode="reflect".
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16000
N_FFT = 400
HOP_LENGTH = 160
N_MELS = 128
CHUNK_LENGTH = 30
N_SAMPLES = CHUNK_LENGTH * SAMPLE_RATE  # 480000


def _hz_to_mel_slaney(hz: np.ndarray) -> np.ndarray:
    hz = np.asarray(hz, dtype=np.float64)
    f_sp = 200.0 / 3.0
    linear = hz / f_sp
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    log_mask = hz >= min_log_hz
    safe_hz = np.where(log_mask, hz, min_log_hz)
    log_part = min_log_mel + np.log(safe_hz / min_log_hz) / logstep
    return np.where(log_mask, log_part, linear)


def _mel_to_hz_slaney(mels: np.ndarray) -> np.ndarray:
    mels = np.asarray(mels, dtype=np.float64)
    f_sp = 200.0 / 3.0
    linear = mels * f_sp
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    log_mask = mels >= min_log_mel
    safe_mels = np.where(log_mask, mels, min_log_mel)
    log_part = min_log_hz * np.exp((safe_mels - min_log_mel) * logstep)
    return np.where(log_mask, log_part, linear)


def _mel_filter_bank(
    n_fft: int = N_FFT,
    n_mels: int = N_MELS,
    sampling_rate: int = SAMPLE_RATE,
    min_frequency: float = 0.0,
    max_frequency: float | None = None,
) -> np.ndarray:
    """Slaney-style triangular mel filter bank with Slaney area normalization.

    Returns shape ``(num_frequency_bins, n_mels)`` float32.
    """
    if max_frequency is None:
        max_frequency = sampling_rate / 2.0
    num_frequency_bins = n_fft // 2 + 1

    mel_min = _hz_to_mel_slaney(np.array(min_frequency))
    mel_max = _hz_to_mel_slaney(np.array(max_frequency))
    mel_freqs = np.linspace(mel_min, mel_max, n_mels + 2)
    filter_freqs = _mel_to_hz_slaney(mel_freqs)

    fft_freqs = np.linspace(0.0, sampling_rate / 2.0, num_frequency_bins)

    filter_diff = np.diff(filter_freqs)
    slopes = filter_freqs[np.newaxis, :] - fft_freqs[:, np.newaxis]
    down_slopes = -slopes[:, :-2] / filter_diff[:-1]
    up_slopes = slopes[:, 2:] / filter_diff[1:]
    mel_filters = np.maximum(0.0, np.minimum(down_slopes, up_slopes))

    enorm = 2.0 / (filter_freqs[2 : n_mels + 2] - filter_freqs[:n_mels])
    mel_filters = mel_filters * enorm[np.newaxis, :]

    return mel_filters.astype(np.float32)


def _hann_window(n_fft: int = N_FFT) -> np.ndarray:
    """Periodic Hann window (``torch.hann_window(N, periodic=True)`` style)."""
    n = np.arange(n_fft, dtype=np.float64)
    return (0.5 - 0.5 * np.cos(2.0 * np.pi * n / n_fft)).astype(np.float32)


_MEL_FILTERS = _mel_filter_bank()
_WINDOW = _hann_window()


def pad_or_trim(audio: np.ndarray, length: int = N_SAMPLES) -> np.ndarray:
    """Pad with zeros or trim to exactly ``length`` samples along last axis."""
    if audio.shape[-1] > length:
        return audio[..., :length]
    if audio.shape[-1] < length:
        pad_width = [(0, 0)] * (audio.ndim - 1) + [
            (0, length - audio.shape[-1])
        ]
        return np.pad(audio, pad_width, mode="constant", constant_values=0.0)
    return audio


def log_mel_spectrogram(audio: np.ndarray) -> np.ndarray:
    """Compute log-mel spectrogram matching ``WhisperFeatureExtractor``.

    Parameters
    ----------
    audio:
        float32 1D array at 16 kHz, nominally in [-1, 1]. Must be at least
        ``N_FFT // 2`` samples long for the reflect-padded STFT to be defined;
        typical input is pre-padded to 30s via :func:`pad_or_trim`.

    Returns
    -------
    np.ndarray
        ``(N_MELS, n_frames)`` float32. For 30s input, ``n_frames == 3000``.
    """
    if audio.ndim != 1:
        raise ValueError(
            f"log_mel_spectrogram expects 1D audio, got shape {audio.shape}"
        )
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)

    pad = N_FFT // 2
    padded = np.pad(audio, pad, mode="reflect")

    n_frames = 1 + (padded.shape[0] - N_FFT) // HOP_LENGTH
    stride = padded.strides[0]
    frames = np.lib.stride_tricks.as_strided(
        padded,
        shape=(n_frames, N_FFT),
        strides=(stride * HOP_LENGTH, stride),
    )
    frames = frames * _WINDOW

    stft = np.fft.rfft(frames, n=N_FFT, axis=1)
    power = (
        stft.real.astype(np.float32) ** 2 + stft.imag.astype(np.float32) ** 2
    )

    mel = power @ _MEL_FILTERS  # (n_frames, n_mels)
    mel = mel[:-1, :]  # drop final frame (matches whisper)

    log_spec = np.log10(np.maximum(mel, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0

    return log_spec.T.astype(np.float32)
