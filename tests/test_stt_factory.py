"""Tests for ``whisper_input.stt.create_stt``."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from whisper_input.stt import BaseSTT, create_stt


def test_create_qwen3_returns_qwen3_stt():
    # We don't actually load the model — just verify the factory wiring.
    with patch(
        "whisper_input.stt.qwen3.Qwen3ASRSTT"
    ) as mock_cls:
        create_stt("qwen3", {"variant": "1.7B"})
    mock_cls.assert_called_once_with(variant="1.7B")


def test_create_qwen3_default_variant():
    with patch(
        "whisper_input.stt.qwen3.Qwen3ASRSTT"
    ) as mock_cls:
        create_stt("qwen3", {})
    mock_cls.assert_called_once_with(variant="0.6B")


def test_create_stt_unknown_engine_raises():
    # Old "sensevoice" engine name must now be rejected — migration in
    # ConfigManager should have rewritten it, but if a user edits config
    # by hand we fall through to the catch-all.
    with pytest.raises(ValueError):
        create_stt("sensevoice", {})
    with pytest.raises(ValueError):
        create_stt("unknown", {})


def test_base_stt_is_abstract():
    with pytest.raises(TypeError):
        BaseSTT()  # type: ignore[abstract]


def test_qwen3_stt_claims_streaming_support():
    """Qwen3ASRSTT 必须把 supports_streaming 翻到 True;__main__ 的
    `_should_stream` 决策依赖这个类变量。"""
    from whisper_input.stt.qwen3 import Qwen3ASRSTT

    assert Qwen3ASRSTT.supports_streaming is True


def test_base_stt_defaults_to_no_streaming():
    """新 STT 引擎默认不支持流式(BaseSTT 类变量是 False)。"""
    assert BaseSTT.supports_streaming is False


def test_base_stt_stream_methods_raise_not_implemented():
    """没 override 的子类调 init_stream_state / stream_step 应抛 NotImplementedError。"""
    import numpy as np

    class MinimalSTT(BaseSTT):
        def load(self):
            pass

        def transcribe(self, wav_data):
            return ""

    stt = MinimalSTT()
    with pytest.raises(NotImplementedError):
        stt.init_stream_state()
    with pytest.raises(NotImplementedError):
        stt.stream_step(np.zeros(0, dtype=np.float32), None, False)
