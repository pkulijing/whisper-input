"""测试设置页面 Web 服务的 REST API。

针对 src/whisper_input/settings_server.py。

启动一个真实的 SettingsServer 在 127.0.0.1 + 一个临时空闲端口上,用
stdlib http.client 打请求验证 handler。所有写操作落在 tmp_path 隔离的
ConfigManager 上,/api/quit 和 /api/restart 走 monkeypatch 后的 no-op
os.kill / os.execv,避免测试进程被真的杀掉。
"""

import http.client
import json
import socket
import time

import pytest

from whisper_input import settings_server as ss
from whisper_input.config_manager import DEFAULT_CONFIG, ConfigManager


def _free_port() -> int:
    """让 OS 分配一个可用端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def autostart_state(monkeypatch):
    """Stub 出 settings_server 模块里 import 进来的 autostart 后端,
    用一个可变 dict 模拟开关状态。
    """
    state = {"enabled": False}

    def fake_is_enabled():
        return state["enabled"]

    def fake_set(enabled: bool):
        state["enabled"] = enabled

    monkeypatch.setattr(ss, "_is_autostart_enabled", fake_is_enabled)
    monkeypatch.setattr(ss, "_set_autostart", fake_set)
    return state


@pytest.fixture
def running_server(tmp_path, autostart_state, monkeypatch):
    """启动 SettingsServer,yield (host, port, config_manager) 三元组。"""
    # 把 quit / restart 里的 os.kill / os.execv 替换成 no-op,避免杀测试进程
    monkeypatch.setattr(ss.os, "kill", lambda *a, **kw: None)
    monkeypatch.setattr(ss.os, "execv", lambda *a, **kw: None)

    cfg_path = tmp_path / "config.yaml"
    config_mgr = ConfigManager(config_path=str(cfg_path))
    port = _free_port()
    server = ss.SettingsServer(config_mgr, port=port)
    server.start()
    try:
        yield ("127.0.0.1", port, config_mgr)
    finally:
        server.stop()


def _request(method: str, host: str, port: int, path: str, body=None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


# --- 静态 HTML 渲染 ---


def test_get_settings_html_substitutes_placeholders():
    html = ss._get_settings_html()
    assert "Whisper Input" in html
    # 占位符全部被替换
    assert "HOTKEY_OPTIONS_PLACEHOLDER" not in html
    assert "HOTKEY_KEY_PLACEHOLDER" not in html
    # JS 数据数组被注入
    assert "const HOTKEY_CODES = [" in html
    # locale 数据被注入
    assert "const LOCALES = {" in html


# --- HTTP endpoint smoke tests ---


def test_get_root_returns_html(running_server):
    host, port, _ = running_server
    status, data = _request("GET", host, port, "/")
    assert status == 200
    assert b"Whisper Input" in data


def test_get_api_config_returns_defaults(running_server):
    host, port, _ = running_server
    status, data = _request("GET", host, port, "/api/config")
    assert status == 200
    cfg = json.loads(data)
    assert cfg["engine"] == DEFAULT_CONFIG["engine"]
    assert cfg["audio"]["sample_rate"] == 16000


def test_post_api_config_persists(running_server):
    host, port, mgr = running_server
    status, _ = _request(
        "POST",
        host,
        port,
        "/api/config",
        body={"sensevoice.use_itn": False},
    )
    assert status == 200
    # 内存里更新
    assert mgr.get("sensevoice.use_itn") is False
    # 磁盘里也持久化了:reload 一遍
    mgr.load()
    assert mgr.get("sensevoice.use_itn") is False


def test_post_api_config_invalid_json(running_server):
    host, port, _ = running_server
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(
        "POST",
        "/api/config",
        body=b"not json{{",
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    assert resp.status == 400
    conn.close()


def test_post_api_config_reset(running_server):
    host, port, mgr = running_server
    # 先改一个值
    _request(
        "POST",
        host,
        port,
        "/api/config",
        body={"sensevoice.use_itn": False},
    )
    # reset
    status, _ = _request(
        "POST", host, port, "/api/config/reset"
    )
    assert status == 200
    # 重新 load,值回到默认
    mgr.load()
    assert (
        mgr.get("sensevoice.use_itn")
        == DEFAULT_CONFIG["sensevoice"]["use_itn"]
    )


def test_get_api_autostart(running_server, autostart_state):
    host, port, _ = running_server
    status, data = _request("GET", host, port, "/api/autostart")
    assert status == 200
    assert json.loads(data) == {"enabled": False}

    autostart_state["enabled"] = True
    status, data = _request("GET", host, port, "/api/autostart")
    assert json.loads(data) == {"enabled": True}


def test_post_api_autostart(running_server, autostart_state):
    host, port, _ = running_server
    status, _ = _request(
        "POST", host, port, "/api/autostart", body={"enabled": True}
    )
    assert status == 200
    assert autostart_state["enabled"] is True

    _request(
        "POST",
        host,
        port,
        "/api/autostart",
        body={"enabled": False},
    )
    assert autostart_state["enabled"] is False


def test_quit_endpoint_returns_200(running_server):
    """os.kill 已被 monkeypatch 成 no-op,只验证 handler 不挂。"""
    host, port, _ = running_server
    status, _ = _request("POST", host, port, "/api/quit")
    assert status == 200
    # 给 threading.Timer (0.5s) 留点时间以确认 fake os.kill 不会炸
    time.sleep(0.6)


def test_restart_endpoint_returns_200(running_server):
    host, port, _ = running_server
    status, _ = _request("POST", host, port, "/api/restart")
    assert status == 200
    time.sleep(0.6)


def test_unknown_path_404(running_server):
    host, port, _ = running_server
    status, _ = _request("GET", host, port, "/api/nonexistent")
    assert status == 404


# --- commit 链接修复 (指向 tree/<sha> 而非 commit/<sha>) ---


def test_commit_link_points_to_tree(monkeypatch):
    """HTML 里 commit 链接必须是 /tree/<sha>,不能再出现 /commit/<sha>。"""
    monkeypatch.setattr(
        "whisper_input.settings_server.__commit__",
        "abc1234" + "0" * 33,
        raising=False,
    )
    # 注意:settings_server 里是在函数体里 import __commit__,所以要 patch
    # 源模块而不是 ss 顶层
    monkeypatch.setattr(
        "whisper_input.version.__commit__",
        "abc1234" + "0" * 33,
    )
    html = ss._get_settings_html()
    assert "/tree/abc1234" in html
    assert "/commit/abc1234" not in html


# --- /api/update/check + /api/update/apply ---


def test_update_check_disabled_skips_network(running_server, monkeypatch):
    host, port, mgr = running_server
    # 关掉开关
    mgr.set("update.check_enabled", False)
    mgr.save()

    # 把 fetch_latest_version patch 掉,若被调用则记下来
    calls = []
    monkeypatch.setattr(
        "whisper_input.updater.fetch_latest_version",
        lambda timeout=3.0: calls.append("hit") or "9.9.9",
    )

    status, data = _request("GET", host, port, "/api/update/check")
    assert status == 200
    body = json.loads(data)
    assert body["has_update"] is False
    # 开关关了:handler 不应该触发 fetch
    assert calls == []


def test_update_check_enabled_returns_snapshot(running_server, monkeypatch):
    host, port, _mgr = running_server
    # 把 checker 换成我们可控的
    monkeypatch.setattr(
        "whisper_input.updater.fetch_latest_version",
        lambda timeout=3.0: "9.9.9",
    )
    # server 启动时已经 trigger 过一次 async fetch;等它跑完
    for _ in range(100):
        status, data = _request("GET", host, port, "/api/update/check")
        body = json.loads(data)
        if body["checked_at"] is not None:
            break
        time.sleep(0.02)
    assert status == 200
    # has_update 取决于当前 __version__ 和 "9.9.9" 的比较,一般为 True
    assert "has_update" in body
    assert body["current"]
    # install_method 字段已废弃,防回归
    assert "install_method" not in body


def test_update_apply_invokes_upgrade(running_server, monkeypatch):
    host, port, _ = running_server
    seen = {"called": False}

    def fake_apply(timeout=180.0):
        seen["called"] = True
        return True, "upgraded to 9.9.9"

    monkeypatch.setattr(
        "whisper_input.settings_server.apply_upgrade", fake_apply
    )
    status, data = _request("POST", host, port, "/api/update/apply")
    assert status == 200
    body = json.loads(data)
    assert body["ok"] is True
    assert "9.9.9" in body["output"]
    assert seen["called"] is True


def test_update_apply_failure_propagates_output(running_server, monkeypatch):
    host, port, _ = running_server
    monkeypatch.setattr(
        "whisper_input.settings_server.apply_upgrade",
        lambda timeout=180.0: (False, "pypi unreachable"),
    )
    status, data = _request("POST", host, port, "/api/update/apply")
    assert status == 200
    body = json.loads(data)
    assert body["ok"] is False
    assert body["output"] == "pypi unreachable"
