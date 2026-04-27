"""End-to-end smoke test for ``Qwen3ASRSTT``.

Uses session-shared ``stt_0_6b`` / ``stt_1_7b`` fixtures from conftest;
the WAV fixture is ``tests/fixtures/zh.wav`` — a 10.6s recording of 出师表,
same file used for the Whisper log-mel golden.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from daobidao.stt.qwen3 import Qwen3ASRSTT

# CI 环境关掉这条端到端真识别测试。GitHub Actions runner 池 SKU 漂移导致
# Qwen3 ONNX int8 在长 prompt(~800 token)一次性 prefill 时数值不稳定,greedy
# 第 1 个 token 偶发翻成 EOS → transcribe 返空。本地一直稳。详见 docs/33-CI
# 失败修复/。CI 不跑这条;release 前手测兜底。
_SKIP_E2E = bool(os.environ.get("DAOBIDAO_SKIP_E2E_STT"))
_SKIP_E2E_REASON = (
    "DAOBIDAO_SKIP_E2E_STT set: 端到端真识别在 CI 上不稳定 (runner 抽签),"
    "本地照跑"
)


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


@pytest.mark.skipif(_SKIP_E2E, reason=_SKIP_E2E_REASON)
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
            "先帝创业未半而中道崩殂，今天下三分，益州疲弊，此诚危急存亡之秋也。"
        )


# --------------------------------------------------------------------------
# Warmup fail-fast 行为(round 33,堵 silent garbage)
#
# 这些用例不用 session-scoped real fixture —— 那俩 fixture 已经成功 load 过
# 了,warmup 跑完了。要测 "warmup 抛 RuntimeError",得拿 mock runner 控制
# decoder 输出。
# --------------------------------------------------------------------------


class _FakeTokenizer:
    eos_id = 151645

    def encode(self, prompt: str) -> list[int]:
        # 长度 ~10 个 token 就够,真长度无所谓,只要 _warmup 能拿到 ndarray。
        return [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


class _FakeRunner:
    """可控输出的 fake runner,只够 _warmup 用。

    ``logits_value`` 决定 prefill / 每步 decode 的 logits 长什么样:
        - None        → 正常随机非零 finite 值
        - "nan"       → 全 NaN
        - "zero"      → 全 0
        - "eos_first" → eos token 位置最大,argmax 立即出 EOS
    """

    def __init__(self, logits_value: str | None):
        self.logits_value = logits_value

    def encode_audio(self, mel: np.ndarray) -> np.ndarray:
        # Whisper 30s mel → audio_features 长度 ~750,dim 1024(0.6B)。
        # 实际数值不重要,_warmup 不查。
        return (
            np.random.default_rng(0)
            .standard_normal((1, 750, 1024))
            .astype(np.float32)
        )

    def alloc_decoder_caches(self) -> list:
        return []

    def decoder_step(
        self,
        input_ids: np.ndarray,
        audio_features: np.ndarray,
        caches: list,
        cur_len: int,
    ) -> np.ndarray:
        # 输出 shape (1, seq, vocab),vocab=151936(Qwen3 词表大小)
        seq = input_ids.shape[1]
        vocab = 151936
        if self.logits_value == "nan":
            return np.full((1, seq, vocab), np.nan, dtype=np.float32)
        if self.logits_value == "zero":
            return np.zeros((1, seq, vocab), dtype=np.float32)
        if self.logits_value == "eos_first":
            logits = np.zeros((1, seq, vocab), dtype=np.float32)
            logits[..., 151645] = 1.0  # eos id 最大 → argmax 选 EOS
            return logits
        # 正常路径:随机非零 finite logits,argmax 不会落在 eos 上(概率上)
        logits = (
            np.random.default_rng(42)
            .standard_normal((1, seq, vocab))
            .astype(np.float32)
        )
        # 防御性:把 eos 位置压低,避免随机 argmax 命中 eos 让 generated 空
        logits[..., 151645] = -1e6
        return logits


def _make_unloaded_stt(runner: _FakeRunner) -> Qwen3ASRSTT:
    """造一个绕过真 load() 的 STT,直接注入 fake runner / tokenizer。"""
    s = Qwen3ASRSTT(variant="0.6B")
    s._runner = runner  # type: ignore[assignment]
    s._tokenizer = _FakeTokenizer()  # type: ignore[assignment]
    return s


def test_warmup_raises_on_all_nan_logits():
    s = _make_unloaded_stt(_FakeRunner(logits_value="nan"))
    with pytest.raises(RuntimeError, match=r"warmup.*degenerate"):
        s._warmup()


def test_warmup_raises_on_all_zero_logits():
    s = _make_unloaded_stt(_FakeRunner(logits_value="zero"))
    with pytest.raises(RuntimeError, match=r"warmup.*degenerate"):
        s._warmup()


def test_warmup_raises_on_immediate_eos():
    """模型从第 1 步起就选 EOS → generated=[],典型 "transcribe 返空" 根因。"""
    s = _make_unloaded_stt(_FakeRunner(logits_value="eos_first"))
    with pytest.raises(RuntimeError, match=r"warmup.*degenerate"):
        s._warmup()


def test_warmup_passes_with_healthy_runner():
    """正常 fake runner(非零 finite logits + non-EOS argmax)warmup 不抛。"""
    s = _make_unloaded_stt(_FakeRunner(logits_value=None))
    s._warmup()  # 不抛即过
