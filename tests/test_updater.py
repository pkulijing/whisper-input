"""测试 daobidao.updater —— PyPI 查询 + uv tool upgrade 子进程。

所有网络请求和子进程调用都 monkeypatch 掉，不打真实外网。
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from daobidao import updater

# --- is_newer ---


@pytest.mark.parametrize(
    "latest,current,expected",
    [
        ("0.7.3", "0.7.2", True),
        ("0.7.2", "0.7.2", False),
        ("0.7.1", "0.7.2", False),
        ("1.0.0", "0.9.9", True),
        ("not-a-version", "0.7.2", False),
        ("0.7.3", "not-a-version", False),
        ("", "0.7.2", False),
    ],
)
def test_is_newer(latest, current, expected):
    assert updater.is_newer(latest, current) is expected


# --- fetch_latest_version ---


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_latest_version_ok(monkeypatch):
    body = json.dumps({"info": {"version": "0.9.9"}}).encode()
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda req, timeout=3.0: _FakeResp(body),
    )
    assert updater.fetch_latest_version() == "0.9.9"


def test_fetch_latest_version_non_200(monkeypatch):
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda req, timeout=3.0: _FakeResp(b"", status=503),
    )
    assert updater.fetch_latest_version() is None


def test_fetch_latest_version_bad_json(monkeypatch):
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda req, timeout=3.0: _FakeResp(b"not json{{"),
    )
    assert updater.fetch_latest_version() is None


def test_fetch_latest_version_missing_field(monkeypatch):
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda req, timeout=3.0: _FakeResp(json.dumps({"info": {}}).encode()),
    )
    assert updater.fetch_latest_version() is None


def test_fetch_latest_version_network_error(monkeypatch):
    def raise_error(req, timeout=3.0):
        raise updater.urllib.error.URLError("dns fail")

    monkeypatch.setattr(updater.urllib.request, "urlopen", raise_error)
    assert updater.fetch_latest_version() is None


# --- get_upgrade_command ---


def test_get_upgrade_command_ok(monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/opt/bin/uv")
    cmd = updater.get_upgrade_command()
    assert cmd == ["/opt/bin/uv", "tool", "upgrade", "daobidao"]


def test_get_upgrade_command_uv_missing(monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda _: None)
    assert updater.get_upgrade_command() is None


def test_get_upgrade_command_never_uses_pip(monkeypatch):
    """防回归：不能生成含 pip / pipx / python 的 cmd。"""
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/opt/bin/uv")
    cmd = updater.get_upgrade_command()
    joined = " ".join(cmd or [])
    assert "pip" not in joined
    assert "pipx" not in joined
    assert "python" not in joined


# --- apply_upgrade ---


def test_apply_upgrade_missing_uv(monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda _: None)
    ok, output = updater.apply_upgrade()
    assert ok is False
    assert "uv tool upgrade" in output
    assert "pip" not in output
    assert "pipx" not in output


def test_apply_upgrade_success(monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/opt/bin/uv")
    fake = SimpleNamespace(
        returncode=0,
        stdout="upgraded to 0.9.9\n",
        stderr="",
    )
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *a, **kw: fake,
    )
    ok, output = updater.apply_upgrade()
    assert ok is True
    assert "upgraded to 0.9.9" in output


def test_apply_upgrade_nonzero(monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/opt/bin/uv")
    fake = SimpleNamespace(
        returncode=1,
        stdout="",
        stderr="network unreachable\n",
    )
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda *a, **kw: fake,
    )
    ok, output = updater.apply_upgrade()
    assert ok is False
    assert "network unreachable" in output


def test_apply_upgrade_timeout(monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/opt/bin/uv")

    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="uv", timeout=180)

    monkeypatch.setattr(updater.subprocess, "run", raise_timeout)
    ok, output = updater.apply_upgrade()
    assert ok is False
    assert "超时" in output


# --- UpdateChecker ---


def _wait_until(pred, timeout=2.0, interval=0.02):
    start = time.time()
    while time.time() - start < timeout:
        if pred():
            return True
        time.sleep(interval)
    return False


def test_update_checker_dev_current_version_never_has_update(monkeypatch):
    """dev 模式下 current='dev',is_newer 对非法版本返 False,
    天然不会显示更新横幅 —— 即使 PyPI 返回任何版本号也一样。
    """
    monkeypatch.setattr(
        updater,
        "fetch_latest_version",
        lambda timeout=3.0: "9.9.9",
    )
    checker = updater.UpdateChecker(current_version="dev")
    checker.trigger_async()
    assert _wait_until(lambda: checker.snapshot["checked_at"] is not None)
    snap = checker.snapshot
    assert snap["current"] == "dev"
    assert snap["latest"] == "9.9.9"
    assert snap["has_update"] is False


def test_update_checker_fetches_and_flags_update(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.7.2")
    monkeypatch.setattr(
        updater,
        "fetch_latest_version",
        lambda timeout=3.0: "0.9.9",
    )
    checker = updater.UpdateChecker(current_version="0.7.2")
    assert checker.trigger_async() is True

    assert _wait_until(lambda: checker.snapshot["checked_at"] is not None)
    snap = checker.snapshot
    assert snap["current"] == "0.7.2"
    assert snap["latest"] == "0.9.9"
    assert snap["has_update"] is True
    assert snap["error"] is None
    assert snap["checking"] is False
    # install_method 字段彻底从 snapshot 中移除（防回归）
    assert "install_method" not in snap


def test_update_checker_no_update_when_same(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.7.2")
    monkeypatch.setattr(
        updater,
        "fetch_latest_version",
        lambda timeout=3.0: "0.7.2",
    )
    checker = updater.UpdateChecker(current_version="0.7.2")
    checker.trigger_async()
    assert _wait_until(lambda: checker.snapshot["checked_at"] is not None)
    assert checker.snapshot["has_update"] is False


def test_update_checker_network_failure(monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.7.2")
    monkeypatch.setattr(
        updater,
        "fetch_latest_version",
        lambda timeout=3.0: None,
    )
    checker = updater.UpdateChecker(current_version="0.7.2")
    checker.trigger_async()
    assert _wait_until(lambda: checker.snapshot["checked_at"] is not None)
    snap = checker.snapshot
    assert snap["latest"] is None
    assert snap["has_update"] is False
    assert snap["error"] is not None


# --------------------------------------------------------------------------
# TTL / is_stale / trigger_if_stale (round 34)
# --------------------------------------------------------------------------


def test_is_stale_when_never_checked():
    checker = updater.UpdateChecker(current_version="1.0.0")
    assert checker.is_stale() is True


def test_is_stale_when_recently_checked(monkeypatch):
    """checked_at = now - 60s,远在 TTL 内 → 不 stale。"""
    monkeypatch.setattr(
        updater,
        "fetch_latest_version",
        lambda timeout=3.0: "1.0.0",
    )
    checker = updater.UpdateChecker(current_version="1.0.0")
    checker.trigger_async()
    assert _wait_until(lambda: checker.snapshot["checked_at"] is not None)
    # 上面 trigger_async 用 time.time() 写真实时间戳;它必然在 TTL 内
    assert checker.is_stale() is False


def test_is_stale_when_old_check(monkeypatch):
    """伪造 checked_at 为 2 小时前 → stale。"""
    monkeypatch.setattr(
        updater,
        "fetch_latest_version",
        lambda timeout=3.0: "1.0.0",
    )
    checker = updater.UpdateChecker(current_version="1.0.0")
    checker.trigger_async()
    assert _wait_until(lambda: checker.snapshot["checked_at"] is not None)

    # 直接捏内部 _checked_at,模拟 2 小时前的检查
    with checker._lock:
        checker._checked_at = time.time() - 7200.0
    assert checker.is_stale() is True


def test_trigger_if_stale_first_call(monkeypatch):
    """首次调,checked_at 为 None,是 stale,真启动。"""
    monkeypatch.setattr(
        updater,
        "fetch_latest_version",
        lambda timeout=3.0: "1.0.0",
    )
    checker = updater.UpdateChecker(current_version="1.0.0")
    assert checker.trigger_if_stale() is True
    assert _wait_until(lambda: checker.snapshot["checked_at"] is not None)


def test_trigger_if_stale_returns_false_when_fresh(monkeypatch):
    """缓存还新鲜,trigger_if_stale 不启动新检查,返 False。"""
    fetch_count = [0]

    def fake_fetch(timeout=3.0):
        fetch_count[0] += 1
        return "1.0.0"

    monkeypatch.setattr(updater, "fetch_latest_version", fake_fetch)
    checker = updater.UpdateChecker(current_version="1.0.0")
    checker.trigger_async()
    assert _wait_until(lambda: checker.snapshot["checked_at"] is not None)
    assert fetch_count[0] == 1

    # checked_at 刚写,fresh,trigger_if_stale 应当跳过
    assert checker.trigger_if_stale() is False
    # 给后台一点时间确认确实没有第二次 fetch
    time.sleep(0.1)
    assert fetch_count[0] == 1


def test_trigger_if_stale_returns_false_when_in_progress():
    """正在检查中,trigger_if_stale 不再启动,返 False。"""
    checker = updater.UpdateChecker(current_version="1.0.0")
    # 直接捏内部状态模拟"正在检查中"
    with checker._lock:
        checker._checking = True
    assert checker.trigger_if_stale() is False


# --------------------------------------------------------------------------
# Bundled: configure_logging stderr param (round 34 顺手做)
# --------------------------------------------------------------------------


def test_configure_logging_default_no_stderr_handler(monkeypatch, tmp_path):
    """默认不挂 stderr handler —— 命令行启动时不在 terminal 打 log。"""
    import logging

    import daobidao.logger as log_mod

    monkeypatch.setattr(
        "daobidao.config_manager._find_project_root",
        lambda: tmp_path,
    )
    # 备份并清空 root handlers
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_configured = log_mod._configured
    for h in root.handlers[:]:
        root.removeHandler(h)
    try:
        log_mod.configure_logging("INFO")
        # 默认只有 file handler,无 stderr
        assert len(root.handlers) == 1
        assert isinstance(
            root.handlers[0], logging.handlers.RotatingFileHandler
        )
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)
        import structlog

        structlog.reset_defaults()
        log_mod._configured = saved_configured


def test_configure_logging_with_stderr_handler(monkeypatch, tmp_path):
    """显式 stderr=True 时挂上 stderr handler。"""
    import logging

    import daobidao.logger as log_mod

    monkeypatch.setattr(
        "daobidao.config_manager._find_project_root",
        lambda: tmp_path,
    )
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_configured = log_mod._configured
    for h in root.handlers[:]:
        root.removeHandler(h)
    try:
        log_mod.configure_logging("INFO", stderr=True)
        # file + stderr 两个 handler
        assert len(root.handlers) == 2
        kinds = {type(h).__name__ for h in root.handlers}
        assert "RotatingFileHandler" in kinds
        assert "StreamHandler" in kinds
    finally:
        for h in root.handlers[:]:
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)
        import structlog

        structlog.reset_defaults()
        log_mod._configured = saved_configured
