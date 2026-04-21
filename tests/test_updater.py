"""测试 whisper_input.updater —— PyPI 查询 + uv tool upgrade 子进程。

所有网络请求和子进程调用都 monkeypatch 掉，不打真实外网。
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from whisper_input import updater

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
        lambda req, timeout=3.0: _FakeResp(
            json.dumps({"info": {}}).encode()
        ),
    )
    assert updater.fetch_latest_version() is None


def test_fetch_latest_version_network_error(monkeypatch):
    def raise_error(req, timeout=3.0):
        raise updater.urllib.error.URLError("dns fail")

    monkeypatch.setattr(
        updater.urllib.request, "urlopen", raise_error
    )
    assert updater.fetch_latest_version() is None


# --- get_upgrade_command ---


def test_get_upgrade_command_ok(monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/opt/bin/uv")
    cmd = updater.get_upgrade_command()
    assert cmd == ["/opt/bin/uv", "tool", "upgrade", "whisper-input"]


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
