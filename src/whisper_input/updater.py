"""PyPI 更新检查与触发 —— 查询最新版本 + 跑 `uv tool upgrade`。

所有网络 / 子进程调用都是同步的，外面由 UpdateChecker 包后台线程（保持与
整个项目 threading + 阻塞 IO 的一致；future work 见 BACKLOG 的 asyncio 迁移）。

只支持 uv tool 装的场景（项目唯一官方分发路径），点按钮直接跑
`uv tool upgrade whisper-input`。dev 模式的 `__version__ == "dev"` 不是
合法 PEP 440 版本号,`is_newer()` 比较时天然返 False,横幅不会出现 —— 不用
单独判断。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

from whisper_input.logger import get_logger

from whisper_input.version import __version__

logger = get_logger(__name__)

PYPI_JSON_URL = "https://pypi.org/pypi/whisper-input/json"
PACKAGE_NAME = "whisper-input"

MANUAL_UPGRADE_HINT = (
    "未找到 uv 可执行文件，请在终端运行：\n"
    "  uv tool upgrade whisper-input"
)


def fetch_latest_version(timeout: float = 3.0) -> str | None:
    """同步查 PyPI。失败返回 None，不抛异常。"""
    try:
        req = urllib.request.Request(
            PYPI_JSON_URL,
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
        v = data.get("info", {}).get("version")
        return v if isinstance(v, str) and v else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        logger.debug("pypi_fetch_failed", error=str(e))
        return None


def is_newer(latest: str, current: str) -> bool:
    """packaging.Version 比较；任一不合法返回 False。"""
    try:
        from packaging.version import InvalidVersion, Version

        return Version(latest) > Version(current)
    except (InvalidVersion, ImportError, TypeError):
        return False


def get_upgrade_command() -> list[str] | None:
    """构造 `uv tool upgrade whisper-input`。找不到 uv 返回 None。"""
    uv = shutil.which("uv")
    if uv is None:
        return None
    return [uv, "tool", "upgrade", PACKAGE_NAME]


def apply_upgrade(timeout: float = 180.0) -> tuple[bool, str]:
    """执行 upgrade 命令。返回 (ok, combined_output)。"""
    cmd = get_upgrade_command()
    if cmd is None:
        return False, MANUAL_UPGRADE_HINT
    logger.info("upgrade_start", cmd=cmd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("upgrade_timeout", cmd=cmd, timeout=timeout)
        return False, f"升级超时（>{timeout:.0f}s），已中止。"
    except (OSError, FileNotFoundError) as e:
        logger.warning("upgrade_oserror", cmd=cmd, error=str(e))
        return False, f"无法启动升级命令: {e}"
    output = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0
    logger.info(
        "upgrade_done",
        returncode=proc.returncode,
        ok=ok,
    )
    return ok, output.strip() or (
        "（升级命令无输出）" if ok else f"退出码 {proc.returncode}"
    )


class UpdateChecker:
    """缓存最近一次 PyPI 检查结果，后台线程刷新。"""

    def __init__(self, current_version: str | None = None):
        self._current = current_version or __version__
        self._lock = threading.Lock()
        self._latest: str | None = None
        self._checked_at: float | None = None
        self._error: str | None = None
        self._checking: bool = False

    @property
    def snapshot(self) -> dict:
        with self._lock:
            has_update = (
                self._latest is not None
                and is_newer(self._latest, self._current)
            )
            return {
                "current": self._current,
                "latest": self._latest,
                "has_update": has_update,
                "checking": self._checking,
                "checked_at": self._checked_at,
                "error": self._error,
            }

    def trigger_async(self) -> bool:
        """启动后台检查。已在检查中则跳过。"""
        with self._lock:
            if self._checking:
                return False
            self._checking = True
            self._error = None
        t = threading.Thread(
            target=self._run_check,
            daemon=True,
            name="update-checker",
        )
        t.start()
        return True

    def _run_check(self) -> None:
        latest = fetch_latest_version()
        with self._lock:
            self._latest = latest
            self._checked_at = time.time()
            self._checking = False
            if latest is None:
                self._error = "无法获取最新版本（网络或 PyPI 异常）"
            else:
                self._error = None
        logger.info(
            "update_check_done",
            current=self._current,
            latest=latest,
        )
