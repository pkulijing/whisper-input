"""logger 模块:路径解析 + configure_logging + 结构化输出。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
import structlog


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """还原 root logger handlers + structlog 全局 config + _configured 标志,
    避免 test_logger 用例污染同 session 后续其它测试文件的 logger 输出。

    背景:configure_logging 直接动 root.handlers 和 structlog.configure,
    跑完不还原会让后续 test_qwen3_* 的 logger.info 走进一个被改过的全局
    state,日志去向不可控(实测 → 沉默 / 写到 tmp_path 已删的文件)。
    """
    import daobidao.logger as log_mod

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_configured = log_mod._configured
    for h in root.handlers[:]:
        root.removeHandler(h)
    yield
    for h in root.handlers[:]:
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    structlog.reset_defaults()
    log_mod._configured = saved_configured


def test_get_log_dir_dev_mode(monkeypatch, tmp_path):
    """dev 模式下日志目录落在 repo_root/logs/。"""
    import daobidao.logger as log_mod

    fake_root = tmp_path / "repo"
    fake_root.mkdir()
    monkeypatch.setattr(
        "daobidao.config_manager._find_project_root",
        lambda: fake_root,
    )
    assert log_mod.get_log_dir() == fake_root / "logs"
    assert log_mod.get_log_file() == fake_root / "logs" / "daobidao.log"


def test_get_log_dir_macos(monkeypatch):
    import daobidao.logger as log_mod

    monkeypatch.setattr(
        "daobidao.config_manager._find_project_root",
        lambda: None,
    )
    monkeypatch.setattr(log_mod, "IS_MACOS", True)
    expected = Path(os.path.expanduser("~/Library/Logs/Daobidao"))
    assert log_mod.get_log_dir() == expected


def test_get_log_dir_linux_xdg(monkeypatch, tmp_path):
    import daobidao.logger as log_mod

    monkeypatch.setattr(
        "daobidao.config_manager._find_project_root",
        lambda: None,
    )
    monkeypatch.setattr(log_mod, "IS_MACOS", False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert log_mod.get_log_dir() == tmp_path / "daobidao"


def test_get_log_dir_linux_xdg_fallback(monkeypatch):
    import daobidao.logger as log_mod

    monkeypatch.setattr(
        "daobidao.config_manager._find_project_root",
        lambda: None,
    )
    monkeypatch.setattr(log_mod, "IS_MACOS", False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    expected = Path(os.path.expanduser("~/.local/state")) / "daobidao"
    assert log_mod.get_log_dir() == expected


def test_configure_logging_idempotent(monkeypatch, tmp_path):
    """多次调 configure_logging 不应累加 handler。"""
    import daobidao.logger as log_mod

    monkeypatch.setattr(
        "daobidao.config_manager._find_project_root",
        lambda: tmp_path,
    )
    # round 34 起默认 stderr=False,只挂 file handler。再调依旧只 1 个。
    log_mod.configure_logging("INFO")
    first = len(logging.getLogger().handlers)
    log_mod.configure_logging("DEBUG")
    second = len(logging.getLogger().handlers)
    assert first == second == 1  # file only
    assert logging.getLogger().level == logging.DEBUG

    # 显式 stderr=True 再调一次:多出一个 StreamHandler
    log_mod.configure_logging("INFO", stderr=True)
    third = len(logging.getLogger().handlers)
    assert third == 2  # file + stderr


def test_log_file_logfmt_format(monkeypatch, tmp_path):
    """文件输出应是 logfmt (key=value),结构化 event + 关键字段都能读到。"""
    import daobidao.logger as log_mod

    monkeypatch.setattr(
        "daobidao.config_manager._find_project_root",
        lambda: tmp_path,
    )
    log_mod.configure_logging("INFO")

    logger = log_mod.get_logger("test.logfmt")
    logger.info("hotkey_listening", hotkey="KEY_RIGHTCTRL")

    # 刷 handler
    for h in logging.getLogger().handlers:
        h.flush()

    content = (tmp_path / "logs" / "daobidao.log").read_text(encoding="utf-8")
    assert "event='hotkey_listening'" in content
    assert "hotkey='KEY_RIGHTCTRL'" in content
    assert "level='info'" in content
    assert "timestamp=" in content


def test_log_file_rotation(monkeypatch, tmp_path):
    """写够字节数后应当滚出 .log.1。"""
    import daobidao.logger as log_mod

    monkeypatch.setattr(
        "daobidao.config_manager._find_project_root",
        lambda: tmp_path,
    )
    # 收紧 maxBytes,让少量日志就能触发轮转
    monkeypatch.setattr(log_mod, "_MAX_BYTES", 200)
    log_mod.configure_logging("INFO")

    logger = log_mod.get_logger("test.rotation")
    # 每条 logfmt 行大概 80-120 字节,写 20 条稳触发
    for i in range(20):
        logger.info("event_x", i=i, payload="a" * 40)
    for h in logging.getLogger().handlers:
        h.flush()

    log_dir = tmp_path / "logs"
    main = log_dir / "daobidao.log"
    rotated = log_dir / "daobidao.log.1"
    assert main.exists()
    assert rotated.exists()


def test_exception_goes_to_log(monkeypatch, tmp_path):
    """logger.exception() 应把 traceback 写到文件。"""
    import daobidao.logger as log_mod

    monkeypatch.setattr(
        "daobidao.config_manager._find_project_root",
        lambda: tmp_path,
    )
    log_mod.configure_logging("INFO")

    logger = log_mod.get_logger("test.exc")
    try:
        raise ValueError("boom-marker-123")
    except ValueError:
        logger.exception("recognize_failed")
    for h in logging.getLogger().handlers:
        h.flush()

    content = (tmp_path / "logs" / "daobidao.log").read_text(encoding="utf-8")
    assert "recognize_failed" in content
    assert "ValueError" in content
    assert "boom-marker-123" in content


def test_launchd_log_file_path(monkeypatch, tmp_path):
    """plist StandardErrorPath 指向 get_launchd_log_file()。"""
    import daobidao.logger as log_mod

    monkeypatch.setattr(
        "daobidao.config_manager._find_project_root",
        lambda: tmp_path,
    )
    assert (
        log_mod.get_launchd_log_file()
        == tmp_path / "logs" / "daobidao-launchd.log"
    )
