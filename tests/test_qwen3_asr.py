"""End-to-end smoke test for ``Qwen3ASRSTT``.

Uses session-shared ``stt_0_6b`` / ``stt_1_7b`` fixtures from conftest;
the WAV fixture is ``tests/fixtures/zh.wav`` — a 10.6s recording of 出师表,
same file used for the Whisper log-mel golden.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daobidao.stt.qwen3 import Qwen3ASRSTT


@pytest.fixture(
    scope="module",
    params=["0.6B", "1.7B"],
    ids=["0.6B", "1.7B"],
)
def stt(request, stt_0_6b, stt_1_7b) -> Qwen3ASRSTT:
    """Hand back the loaded STT for the requested variant."""
    return {"0.6B": stt_0_6b, "1.7B": stt_1_7b}[request.param]


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------

def test_rejects_unknown_variant():
    with pytest.raises(ValueError, match="unknown variant"):
        Qwen3ASRSTT(variant="0.3B")


def test_default_variant_is_0_6b():
    s = Qwen3ASRSTT()
    assert s.variant == "0.6B"


# --------------------------------------------------------------------------
# Load / idempotency
# --------------------------------------------------------------------------

def test_load_is_idempotent(stt: Qwen3ASRSTT):
    runner_before = stt._runner
    tokenizer_before = stt._tokenizer
    cache_root_before = stt.cache_root
    stt.load()
    assert stt._runner is runner_before
    assert stt._tokenizer is tokenizer_before
    assert stt.cache_root == cache_root_before


def test_cache_root_set_after_load(stt: Qwen3ASRSTT):
    """``Qwen3ASRSTT.cache_root`` is the public entry point for path
    discovery (used by tests, settings UI, debugging)."""
    assert stt.cache_root is not None
    assert (stt.cache_root / "tokenizer" / "vocab.json").exists()
    assert (
        stt.cache_root / f"model_{stt.variant}" / "conv_frontend.onnx"
    ).exists()


# --------------------------------------------------------------------------
# Short-circuits
# --------------------------------------------------------------------------

def test_transcribe_empty_bytes_returns_empty(stt: Qwen3ASRSTT):
    assert stt.transcribe(b"") == ""


def test_transcribe_very_short_audio_returns_empty(stt: Qwen3ASRSTT):
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 800)  # 0.05s

    assert stt.transcribe(buf.getvalue()) == ""


# --------------------------------------------------------------------------
# End-to-end recognition (golden-ish)
# --------------------------------------------------------------------------

def test_transcribe_zh_wav(stt: Qwen3ASRSTT, request):
    wav_path = Path(__file__).parent / "fixtures" / "zh.wav"
    text = stt.transcribe(wav_path.read_bytes())

    # 跨 variant 都该出现的关键词;exact-string 匹配只对 0.6B(1.7B 输出可能
    # 差几个标点 / 字)。
    assert "先帝" in text, f"unexpected transcript: {text!r}"
    assert "益州" in text, f"unexpected transcript: {text!r}"
    # Should NOT leak raw markers
    assert "<asr_text>" not in text
    assert "<|im_end|>" not in text
    assert "language" not in text

    if request.node.callspec.id == "0.6B":
        # Exact full output on current 0.6B int8 (documented for regression):
        assert text == (
            "先帝创业未半而中道崩殂，今天下三分，益州疲弊，"
            "此诚危急存亡之秋也。"
        )
