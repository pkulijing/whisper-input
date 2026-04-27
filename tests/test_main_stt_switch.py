"""测试 WhisperInput 的 STT 热切换逻辑。

核心不变量:
1. 切换在后台线程进行,on_config_changed 立刻返回
2. 切换完成后 self.stt 被原子替换为新实例
3. 并发切换请求被忽略(第二次调用直接 return)
4. 切换失败时旧 self.stt 保留,error 字段记录在状态里
5. 相同 variant 立刻返回,不启动 worker
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from daobidao.__main__ import WhisperInput


@pytest.fixture
def wi(monkeypatch):
    """构造一个 WhisperInput,stt 是带 variant 属性的 Mock。"""
    fake_stt = MagicMock()
    fake_stt.variant = "0.6B"
    monkeypatch.setattr(
        "daobidao.__main__.create_stt_engine",
        lambda cfg: fake_stt,
    )
    instance = WhisperInput(
        {
            "audio": {"sample_rate": 16000, "channels": 1},
            "sound": {"enabled": False},
            "tray_status": {"enabled": False},
            "overlay": {"enabled": False},
        }
    )
    yield instance
    instance.stop_worker(timeout=1.0)


def _wait_until_not_switching(wi, timeout: float = 3.0) -> dict:
    """轮询 stt_switch_status 直到 switching=False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = wi.stt_switch_status()
        if not status["switching"]:
            return status
        time.sleep(0.02)
    raise AssertionError(f"switch never completed; last={status}")


def test_initial_switch_status_is_idle(wi: WhisperInput):
    status = wi.stt_switch_status()
    assert status == {
        "switching": False,
        "target_variant": None,
        "error": None,
    }


def test_switch_same_variant_is_noop(wi: WhisperInput):
    wi._switch_stt_variant("0.6B")
    # Same-variant path returns immediately without toggling switching=True
    status = wi.stt_switch_status()
    assert status["switching"] is False
    assert status["target_variant"] is None


def test_switch_success_replaces_stt(wi: WhisperInput):
    new_stt = MagicMock()
    new_stt.variant = "1.7B"

    with patch(
        "daobidao.stt.qwen3.Qwen3ASRSTT", return_value=new_stt
    ) as mock_cls:
        wi._switch_stt_variant("1.7B")
        status = _wait_until_not_switching(wi)

    mock_cls.assert_called_once_with(variant="1.7B")
    new_stt.load.assert_called_once()
    assert wi.stt is new_stt
    assert status == {
        "switching": False,
        "target_variant": None,
        "error": None,
    }


def test_switch_failure_preserves_old_stt(wi: WhisperInput):
    old_stt = wi.stt

    with patch(
        "daobidao.stt.qwen3.Qwen3ASRSTT",
        side_effect=RuntimeError("network down"),
    ):
        wi._switch_stt_variant("1.7B")
        status = _wait_until_not_switching(wi)

    assert wi.stt is old_stt  # 旧实例保留
    assert status["switching"] is False
    assert status["target_variant"] is None
    assert "network down" in status["error"]


def test_switch_concurrent_request_rejected(wi: WhisperInput):
    """切换途中再次请求会被忽略 —— 通过一个慢 load 制造窗口。"""
    start = threading.Event()
    unblock = threading.Event()

    def slow_stt(variant):
        fake = MagicMock()
        fake.variant = variant

        def slow_load():
            start.set()
            unblock.wait(timeout=5.0)

        fake.load = slow_load
        return fake

    with patch(
        "daobidao.stt.qwen3.Qwen3ASRSTT", side_effect=slow_stt
    ) as mock_cls:
        wi._switch_stt_variant("1.7B")
        assert start.wait(timeout=2.0), "first switch never started"

        # 此时 first switch 还在 load 里,second switch 应该被丢弃
        wi._switch_stt_variant("0.6B")
        assert mock_cls.call_count == 1, (
            "second switch should have been ignored while first runs"
        )

        unblock.set()
        _wait_until_not_switching(wi)

    # 只有第一次的 variant 生效
    assert wi.stt.variant == "1.7B"


def test_on_config_changed_triggers_switch(wi: WhisperInput):
    new_stt = MagicMock()
    new_stt.variant = "1.7B"

    with patch(
        "daobidao.stt.qwen3.Qwen3ASRSTT", return_value=new_stt
    ) as mock_cls:
        wi.on_config_changed({"qwen3.variant": "1.7B"})
        _wait_until_not_switching(wi)

    mock_cls.assert_called_once_with(variant="1.7B")
    assert wi.stt is new_stt


def test_on_config_changed_without_variant_does_not_switch(
    wi: WhisperInput,
):
    with patch("daobidao.stt.qwen3.Qwen3ASRSTT") as mock_cls:
        wi.on_config_changed({"sound.enabled": True})
        # 无 qwen3.variant key,不应构造新 STT
        assert mock_cls.call_count == 0
    assert wi.stt_switch_status()["switching"] is False


def test_switch_status_target_variant_during_switch(wi: WhisperInput):
    """切换进行中时 status 里 target_variant 反映目标值。"""
    start = threading.Event()
    unblock = threading.Event()

    def slow_stt(variant):
        fake = MagicMock()
        fake.variant = variant

        def slow_load():
            start.set()
            unblock.wait(timeout=5.0)

        fake.load = slow_load
        return fake

    with patch("daobidao.stt.qwen3.Qwen3ASRSTT", side_effect=slow_stt):
        wi._switch_stt_variant("1.7B")
        assert start.wait(timeout=2.0)

        status = wi.stt_switch_status()
        assert status["switching"] is True
        assert status["target_variant"] == "1.7B"
        assert status["error"] is None

        unblock.set()
        _wait_until_not_switching(wi)
