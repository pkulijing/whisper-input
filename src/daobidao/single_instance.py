"""检测并清理占着 settings_port 的旧 daobidao 实例。

启动序列前置检查 —— 让"双击启动 = 重启"的 UX 成立,避免老的僵尸进程把
端口占住导致新实例 SettingsServer.start() 抛 OSError 静默崩溃。

核心协议:settings_server 暴露 ``GET /api/pid`` 端点(见
``settings_server._SettingsHandler.do_GET``)。新实例:

1. socket.connect 探 127.0.0.1:<port>:连不上 → 没人占 → 直接放行
2. 能连上 → urllib GET /api/pid → 拿到 PID = 是 daobidao,执行 kill 升级链
3. /api/pid 失败(404 / 超时 / 非 daobidao 进程占了 51230)→ 不敢 kill,
   返 False,让调用方 sys.exit 提示用户手动处理

设计上不引入 psutil / lsof,跨平台一份代码;不做 PID 文件 / fcntl 锁 ——
端口本来就是天然独占资源,直接用。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import time
import urllib.error
import urllib.request

from daobidao.logger import get_logger

logger = get_logger(__name__)


# 升级链中每一档的等待窗口(秒)。SIGTERM 后给进程清理资源的时间;
# SIGKILL 后给内核回收 socket 的时间。
_STEP_TIMEOUT = 1.0
# /api/pid 探测超时。本机 HTTP,正常应在 ms 级返回。
_PID_QUERY_TIMEOUT = 1.0
# 端口探测的 socket connect 超时。本机 connect 应该 ms 级。
_PORT_PROBE_TIMEOUT = 0.1


def kill_stale_instance(port: int) -> bool:
    """检测并清理 ``port`` 上占着的旧 daobidao 实例。

    Returns
    -------
    bool
        ``True`` 表示端口现在空闲(没老实例,或已成功 kill),调用方可
        继续启动。``False`` 表示端口被占且不是 daobidao(或 kill 失败),
        调用方应报错退出。
    """
    if not _port_in_use(port):
        return True

    pid = _query_remote_pid(port)
    if pid is None:
        logger.warning(
            "stale_instance_unknown_owner",
            port=port,
            reason="port occupied but /api/pid did not respond as daobidao",
        )
        return False

    logger.info("stale_instance_detected", port=port, pid=pid)

    # SIGTERM → wait → 仍占 → SIGKILL → wait → 仍占 → 放弃
    for sig, name in [(signal.SIGTERM, "SIGTERM"), (signal.SIGKILL, "SIGKILL")]:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            # 进程已经自己退了,端口可能还在 TIME_WAIT,等一下
            logger.info("stale_instance_already_gone", pid=pid, signal=name)
        except PermissionError:
            logger.error("stale_instance_kill_denied", pid=pid, signal=name)
            return False

        if _wait_port_free(port, _STEP_TIMEOUT):
            logger.info(
                "killed_stale_instance",
                pid=pid,
                signal=name,
                port=port,
            )
            return True

    logger.error(
        "stale_instance_kill_failed",
        pid=pid,
        port=port,
        reason="port still occupied after SIGTERM + SIGKILL",
    )
    return False


def _port_in_use(port: int) -> bool:
    """``True`` 表示有进程在 listen 这个端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_PORT_PROBE_TIMEOUT)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _query_remote_pid(port: int) -> int | None:
    """问占用方"你的 PID 是多少",拿不到合法答案返 None。

    成功条件:HTTP 200 + JSON ``{"pid": <int>}``。任何异常 / 非 daobidao
    返回的内容都视为"不是我们自己的实例",让调用方放弃 kill。
    """
    url = f"http://127.0.0.1:{port}/api/pid"
    try:
        with urllib.request.urlopen(url, timeout=_PID_QUERY_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None

    pid = payload.get("pid") if isinstance(payload, dict) else None
    return pid if isinstance(pid, int) and pid > 0 else None


def _wait_port_free(port: int, timeout: float) -> bool:
    """阻塞等端口空闲,最多 ``timeout`` 秒。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_in_use(port):
            return True
        time.sleep(0.05)
    return False
