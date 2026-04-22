"""End-to-end smoke test for ``Qwen3ASRSTT``.

Uses the cached 0.6B model via ``qwen3_cache_root`` fixture. The WAV fixture
is ``tests/fixtures/zh.wav`` — a 10.6s recording of 出师表, same file used
for the Whisper log-mel golden.

We patch ``download_qwen3_asr`` to return the cache root directly rather
than making a real network call; the tokenizer / ONNX sessions still read
from that root.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from whisper_input.stt.qwen3 import Qwen3ASRSTT


@pytest.fixture(scope="module")
def patched_downloader(qwen3_cache_root: Path):
    """Short-circuit ``download_qwen3_asr`` to the local cache root."""
    with patch(
        "whisper_input.stt.qwen3.qwen3_asr.download_qwen3_asr",
        return_value=qwen3_cache_root,
    ):
        yield


@pytest.fixture(scope="module")
def stt(patched_downloader) -> Qwen3ASRSTT:
    s = Qwen3ASRSTT(variant="0.6B")
    s.load()
    return s


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

def test_load_is_idempotent(stt: Qwen3ASRSTT, patched_downloader):
    runner_before = stt._runner
    tokenizer_before = stt._tokenizer
    stt.load()
    assert stt._runner is runner_before
    assert stt._tokenizer is tokenizer_before


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

def test_transcribe_zh_wav(stt: Qwen3ASRSTT):
    wav_path = Path(__file__).parent / "fixtures" / "zh.wav"
    text = stt.transcribe(wav_path.read_bytes())

    # Qwen3-ASR output is deterministic under greedy decoding, but we keep
    # the assertion on content markers rather than full string match so
    # minor future-version drift doesn't break the test.
    assert "先帝" in text, f"unexpected transcript: {text!r}"
    assert "益州" in text, f"unexpected transcript: {text!r}"
    # Should NOT leak raw markers
    assert "<asr_text>" not in text
    assert "<|im_end|>" not in text
    assert "language" not in text
    # Exact full output on current 0.6B int8 (documented for regression):
    assert text == (
        "先帝创业未半而中道崩殂，今天下三分，益州疲弊，"
        "此诚危急存亡之秋也。"
    )


# --------------------------------------------------------------------------
# Round 27: corrupt-file fallback in load()
# --------------------------------------------------------------------------

def test_load_falls_back_when_runner_construction_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """runner 第一次构造失败 → downloader 用 force_network 重下 → 再构造。

    模拟 local_only fast path 拿到了路径但 .onnx 损坏的极罕见场景。
    纯 mock,不触碰真 ONNX / 真下载。
    """
    from whisper_input.stt.qwen3 import qwen3_asr as mod

    download_calls: list[bool] = []

    def fake_download(variant, *, force_network=False):
        download_calls.append(force_network)
        return tmp_path

    runner_call_count = [0]

    def fake_runner(model_dir):
        runner_call_count[0] += 1
        if runner_call_count[0] == 1:
            raise RuntimeError("simulated InvalidProtobuf")
        return MagicMock()

    monkeypatch.setattr(mod, "download_qwen3_asr", fake_download)
    monkeypatch.setattr(mod, "Qwen3ONNXRunner", fake_runner)
    monkeypatch.setattr(mod, "Qwen3Tokenizer", lambda _: MagicMock())
    # 跳过 _warmup:否则会调 mock runner 的真方法,复杂度不值
    monkeypatch.setattr(Qwen3ASRSTT, "_warmup", lambda self: None)

    stt = Qwen3ASRSTT(variant="0.6B")
    stt.load()

    # 第一次 fast-path / 第二次 force_network
    assert download_calls == [False, True]
    assert runner_call_count[0] == 2
    assert stt._runner is not None


def test_load_second_runner_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """兜底只重试一次;重下后仍失败直接 raise,不无限循环。"""
    from whisper_input.stt.qwen3 import qwen3_asr as mod

    monkeypatch.setattr(
        mod, "download_qwen3_asr", lambda *a, **kw: tmp_path
    )

    def always_fail(model_dir):
        raise RuntimeError("simulated persistent corruption")

    monkeypatch.setattr(mod, "Qwen3ONNXRunner", always_fail)
    monkeypatch.setattr(mod, "Qwen3Tokenizer", lambda _: MagicMock())
    monkeypatch.setattr(Qwen3ASRSTT, "_warmup", lambda self: None)

    stt = Qwen3ASRSTT(variant="0.6B")
    with pytest.raises(RuntimeError, match="persistent corruption"):
        stt.load()
