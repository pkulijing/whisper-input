"""结构化日志 —— structlog 接 stdlib logging，同时落盘 + 打 stderr。

日志目录按平台分：
- macOS: ~/Library/Logs/Daobidao/   (Apple 推荐;Console.app 会扫)
- Linux: $XDG_STATE_HOME/daobidao/  (兜底 ~/.local/state/daobidao/)
- Dev  : {repo_root}/logs/           (通过 .git + pyproject.toml 探测)

目录里有两个文件:
- daobidao.log          app 的结构化日志(logfmt),RotatingFileHandler,
                        1 MB × 3 份
- daobidao-launchd.log  macOS 专属,由 launchd 在 plist 里 StandardErrorPath
                        直接写入,捕获 pre-logger 阶段的崩溃。不经 Python
                        轮转,避免和 RotatingFileHandler 抢 fd
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import sys
from pathlib import Path

import structlog

from daobidao.backends import IS_MACOS

APP_LOG_FILENAME = "daobidao.log"
LAUNCHD_LOG_FILENAME = "daobidao-launchd.log"

_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3

_configured = False


def _dev_log_dir() -> Path | None:
    """Dev 模式:返回 {repo_root}/logs/,非 dev 返回 None。

    复用 config_manager._find_project_root 的 .git + pyproject.toml 探测,
    行为和 config 路径解析保持一致。
    """
    from daobidao.config_manager import _find_project_root

    root = _find_project_root()
    return (root / "logs") if root is not None else None


def get_log_dir() -> Path:
    """解析日志目录(不创建)。"""
    dev = _dev_log_dir()
    if dev is not None:
        return dev
    if IS_MACOS:
        return Path(os.path.expanduser("~/Library/Logs/Daobidao"))
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = (
        Path(xdg_state)
        if xdg_state
        else Path(os.path.expanduser("~/.local/state"))
    )
    return base / "daobidao"


def get_log_file() -> Path:
    """app 结构化日志文件路径。"""
    return get_log_dir() / APP_LOG_FILENAME


def get_launchd_log_file() -> Path:
    """macOS plist StandardErrorPath 指向的文件路径。"""
    return get_log_dir() / LAUNCHD_LOG_FILENAME


def _shared_processors() -> list:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]


def configure_logging(
    level: str = "INFO",
    *,
    stderr: bool = False,
) -> None:
    """一次性配好 structlog + stdlib logging。再调用会重新配置(幂等)。

    ``stderr=False`` (默认):只挂 file handler,terminal 不打 log。命令行
    启动时干净,文件日志照样在 ``get_log_dir()/daobidao.log`` 里。

    ``stderr=True``:同时挂 stderr handler,适合 ``--verbose`` 排错或
    launchd 把 stderr 重定向到日志文件的场景。
    """
    global _configured

    log_dir = get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    shared = _shared_processors()

    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 文件:logfmt (KeyValueRenderer),grep 友好、人也能读
    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.KeyValueRenderer(
                key_order=["timestamp", "level", "logger", "event"],
            ),
        ],
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / APP_LOG_FILENAME,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)

    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
        with contextlib.suppress(Exception):
            h.close()
    root.addHandler(file_handler)

    if stderr:
        # 终端是 TTY 就带颜色,否则纯文本(避免 ANSI 码污染重定向的日志)
        stderr_formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(
                    colors=sys.stderr.isatty(),
                ),
            ],
        )
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(stderr_formatter)
        root.addHandler(stderr_handler)

    root.setLevel(_normalize_level(level))

    _configured = True


def _normalize_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return logging.getLevelName(level.upper()) if level else logging.INFO


def get_logger(name: str | None = None):
    """薄封装:调用方统一写 `logger = get_logger(__name__)`。"""
    return structlog.get_logger(name)


def is_configured() -> bool:
    return _configured
