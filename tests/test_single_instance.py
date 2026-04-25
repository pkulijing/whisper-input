"""``daobidao.single_instance`` 单实例检测 + 清理逻辑单测。

策略:每条用例起一个真 HTTPServer 模拟"占着 settings_port 的进程",通过
mock ``os.kill`` 验证升级链(SIGTERM → SIGKILL)的调用顺序,通过控制 fake
server 的 ``/api/pid`` 响应验证身份验证逻辑。

不依赖真的 daobidao SettingsServer —— 只要 fake server 暴露 ``/api/pid``
返合法 JSON 就够,这是协议层的最低要求。
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from daobidao import single_instance


def _free_port() -> int:
    """让 OS 分一个空闲端口给我们。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeServerThread:
    """轻量 HTTP server 在后台线程跑,用来模拟"占着 port 的进程"。"""

    def __init__(self, port: int, handler_cls: type[BaseHTTPRequestHandler]):
        self.port = port
        self._server = HTTPServer(("127.0.0.1", port), handler_cls)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


def _make_pid_handler(pid_payload: dict | None, status: int = 200):
    """工厂:返一个 BaseHTTPRequestHandler 子类,/api/pid 按指定行为响应。

    ``pid_payload=None`` → 返指定 ``status`` 但不带 JSON(模拟非 daobidao
    占用方,例如 404 或返一个无关 HTML)。
    """

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/pid" and pid_payload is not None:
                body = json.dumps(pid_payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(status if pid_payload is None else 404)
                self.end_headers()

        def log_message(self, *a, **kw):  # 静音
            pass

    return _H


# --------------------------------------------------------------------------
# 端口空闲 → 直接放行
# --------------------------------------------------------------------------

def test_returns_true_when_port_free():
    port = _free_port()
    # 没有 server 占着这个 port
    assert single_instance.kill_stale_instance(port) is True


# --------------------------------------------------------------------------
# 端口被占 + /api/pid 拿到 PID → SIGTERM 成功
# --------------------------------------------------------------------------

def test_kills_stale_instance_with_sigterm(monkeypatch):
    port = _free_port()
    fake_pid = 99999
    server = _FakeServerThread(port, _make_pid_handler({"pid": fake_pid}))
    server.start()
    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        # 模拟 SIGTERM 成功:第一次 kill 之后立刻关掉 fake server,
        # 让端口在升级链里看上去"空了"。
        server.stop()

    monkeypatch.setattr(single_instance.os, "kill", fake_kill)

    assert single_instance.kill_stale_instance(port) is True
    # 只调一次 SIGTERM,没升级到 SIGKILL
    assert len(kill_calls) == 1
    assert kill_calls[0] == (fake_pid, single_instance.signal.SIGTERM)


# --------------------------------------------------------------------------
# SIGTERM 没用 → 升级 SIGKILL 成功
# --------------------------------------------------------------------------

def test_escalates_to_sigkill_when_sigterm_ignored(monkeypatch):
    port = _free_port()
    fake_pid = 99998
    server = _FakeServerThread(port, _make_pid_handler({"pid": fake_pid}))
    server.start()
    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        # SIGTERM 不动,SIGKILL 才停
        if sig == single_instance.signal.SIGKILL:
            server.stop()

    monkeypatch.setattr(single_instance.os, "kill", fake_kill)
    # 把每步 timeout 缩短,加快测试
    monkeypatch.setattr(single_instance, "_STEP_TIMEOUT", 0.3)

    assert single_instance.kill_stale_instance(port) is True
    assert len(kill_calls) == 2
    assert kill_calls[0] == (fake_pid, single_instance.signal.SIGTERM)
    assert kill_calls[1] == (fake_pid, single_instance.signal.SIGKILL)


# --------------------------------------------------------------------------
# SIGTERM + SIGKILL 都没用 → 返 False
# --------------------------------------------------------------------------

def test_returns_false_when_kill_fails(monkeypatch):
    port = _free_port()
    fake_pid = 99997
    server = _FakeServerThread(port, _make_pid_handler({"pid": fake_pid}))
    server.start()
    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        # 永远不停 server,模拟 kill 信号被忽略

    monkeypatch.setattr(single_instance.os, "kill", fake_kill)
    monkeypatch.setattr(single_instance, "_STEP_TIMEOUT", 0.2)

    try:
        assert single_instance.kill_stale_instance(port) is False
        # 都试过了
        assert len(kill_calls) == 2
    finally:
        server.stop()


# --------------------------------------------------------------------------
# /api/pid 返 404(不是 daobidao 占的)→ 不 kill,返 False
# --------------------------------------------------------------------------

def test_returns_false_when_pid_endpoint_returns_404(monkeypatch):
    port = _free_port()
    server = _FakeServerThread(port, _make_pid_handler(None, status=404))
    server.start()
    kill_calls: list = []
    monkeypatch.setattr(
        single_instance.os, "kill", lambda *a: kill_calls.append(a)
    )

    try:
        assert single_instance.kill_stale_instance(port) is False
        # 关键:绝不能 kill 一个未识别的进程
        assert kill_calls == []
    finally:
        server.stop()


# --------------------------------------------------------------------------
# /api/pid 返非法 JSON → 视为不可信,不 kill
# --------------------------------------------------------------------------

def test_returns_false_when_pid_response_malformed(monkeypatch):
    port = _free_port()

    class _BadHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"this is not json")

        def log_message(self, *a, **kw):
            pass

    server = _FakeServerThread(port, _BadHandler)
    server.start()
    kill_calls: list = []
    monkeypatch.setattr(
        single_instance.os, "kill", lambda *a: kill_calls.append(a)
    )

    try:
        assert single_instance.kill_stale_instance(port) is False
        assert kill_calls == []
    finally:
        server.stop()


# --------------------------------------------------------------------------
# /api/pid 返 200 但 payload 没 pid 字段 → 不 kill
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"pid": "not_an_int"},
        {"pid": 0},
        {"pid": -1},
        {"other_field": 12345},
    ],
)
def test_returns_false_when_pid_payload_invalid(monkeypatch, payload):
    port = _free_port()
    server = _FakeServerThread(port, _make_pid_handler(payload))
    server.start()
    kill_calls: list = []
    monkeypatch.setattr(
        single_instance.os, "kill", lambda *a: kill_calls.append(a)
    )

    try:
        assert single_instance.kill_stale_instance(port) is False
        assert kill_calls == []
    finally:
        server.stop()


# --------------------------------------------------------------------------
# 进程已经自己退了(ProcessLookupError),端口残留 → 等一下还在 → 升级 SIGKILL
# --------------------------------------------------------------------------

def test_handles_process_already_gone(monkeypatch):
    port = _free_port()
    fake_pid = 99996
    server = _FakeServerThread(port, _make_pid_handler({"pid": fake_pid}))
    server.start()
    kill_calls: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        # SIGTERM 时假装"进程已经没了",但端口还残留;SIGKILL 时才放
        if sig == single_instance.signal.SIGTERM:
            raise ProcessLookupError(f"no such process {pid}")
        server.stop()

    monkeypatch.setattr(single_instance.os, "kill", fake_kill)
    monkeypatch.setattr(single_instance, "_STEP_TIMEOUT", 0.2)

    assert single_instance.kill_stale_instance(port) is True
    assert len(kill_calls) == 2


# --------------------------------------------------------------------------
# PermissionError(没权限 kill 别人的进程,例如不同用户)→ 立刻返 False
# --------------------------------------------------------------------------

def test_returns_false_on_permission_error(monkeypatch):
    port = _free_port()
    fake_pid = 99995
    server = _FakeServerThread(port, _make_pid_handler({"pid": fake_pid}))
    server.start()

    def fake_kill(pid, sig):
        raise PermissionError("not allowed")

    monkeypatch.setattr(single_instance.os, "kill", fake_kill)

    try:
        assert single_instance.kill_stale_instance(port) is False
    finally:
        server.stop()


# --------------------------------------------------------------------------
# 直接对 helper 做小冒烟
# --------------------------------------------------------------------------

def test_port_in_use_helper():
    free = _free_port()
    assert single_instance._port_in_use(free) is False

    # 起一个 fake server 占着,再确认探测得到
    server = _FakeServerThread(free, _make_pid_handler({"pid": 1}))
    server.start()
    try:
        assert single_instance._port_in_use(free) is True
    finally:
        server.stop()


def test_query_remote_pid_helper():
    port = _free_port()
    server = _FakeServerThread(port, _make_pid_handler({"pid": 4242}))
    server.start()
    try:
        assert single_instance._query_remote_pid(port) == 4242
    finally:
        server.stop()


def test_query_remote_pid_returns_none_when_port_closed():
    port = _free_port()
    # 没人 listen,连不上
    assert single_instance._query_remote_pid(port) is None
