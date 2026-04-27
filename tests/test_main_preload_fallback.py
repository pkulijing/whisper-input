"""测试 WhisperInput.preload_model 在配置 variant 未下载时的兜底 — 36 轮。

场景:
- 用户配置 qwen3.variant=1.7B、本机只有 0.6B → preload 不应该卡 5-10 分钟
  下载;应该回退到 0.6B 临时跑,持久化 config 不动
- 用户配置 0.6B(默认)、0.6B 已下 → 正常 preload
- 两个 variant 都没下 → 跳过 preload,_notify_status('ready'),让用户进
  设置页主动下载
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from daobidao.__main__ import WhisperInput


@pytest.fixture
def wi(monkeypatch):
    fake_stt = MagicMock()
    fake_stt.variant = "1.7B"  # 模拟用户配的是 1.7B
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


def test_preload_falls_back_when_configured_variant_missing(wi):
    """1.7B 未下载、0.6B 已下载 → 切换到 0.6B 然后 preload。"""

    def fake_is_downloaded(variant):
        # 1.7B 没下,0.6B 已下
        return variant == "0.6B"

    fake_0_6b = MagicMock()
    fake_0_6b.variant = "0.6B"

    with (
        patch.object(
            wi.download_manager,
            "is_variant_downloaded",
            side_effect=fake_is_downloaded,
        ),
        patch(
            "daobidao.stt.qwen3.Qwen3ASRSTT", return_value=fake_0_6b
        ) as mock_cls,
    ):
        ok = wi.preload_model()

    assert ok is True
    # 持久化的 config 没动(只在内存里替换了 stt),且 stt 现在是 0.6B
    assert wi.stt is fake_0_6b
    fake_0_6b.load.assert_called_once()
    mock_cls.assert_called_once_with(variant="0.6B")


def test_preload_normal_when_configured_variant_available(wi):
    """配置的 variant 已下载 → 正常调 stt.load()。"""
    original_stt = wi.stt

    with patch.object(
        wi.download_manager, "is_variant_downloaded", return_value=True
    ):
        ok = wi.preload_model()

    assert ok is True
    # stt 没换
    assert wi.stt is original_stt
    original_stt.load.assert_called_once()


def test_preload_skips_when_no_variant_downloaded(wi):
    """两个 variant 都没下 → 不调 stt.load(),返 False。"""
    original_stt = wi.stt

    with patch.object(
        wi.download_manager, "is_variant_downloaded", return_value=False
    ):
        ok = wi.preload_model()

    assert ok is False
    # stt 没换 + load 没被调
    original_stt.load.assert_not_called()
