"""一次性迁移 ≤ 0.7.x 老 whisper-input 用户的本地数据。

老用户从 `uv tool install whisper-input` 升级到改名后的版本时,旧路径
（whisper-input 时代）下的配置 / venv-path / .app bundle 不会被自动
搬过来,需要手动重新设置。本模块提供一个 best-effort 的搬运动作:

- macOS:
  - ~/Library/Application Support/Whisper Input/  → Daobidao/
  - ~/Library/Logs/Whisper Input/                  → Daobidao/
  - ~/Applications/Whisper Input.app               → Daobidao.app
  - LaunchAgent ~/Library/LaunchAgents/com.whisper-input.plist
    重写为 com.daobidao.plist 并 bootout 老条目
- Linux:
  - $XDG_CONFIG_HOME/whisper-input/                → daobidao/
  - $XDG_STATE_HOME/whisper-input/                 → daobidao/
  - $XDG_CONFIG_HOME/autostart/whisper-input.desktop
    重写为 daobidao.desktop

只在新路径不存在时执行,避免覆盖用户已有的设置;失败一律静默,因为这是
锦上添花而非阻断启动。模块在 __main__.py 里仅在桌面启动模式调用一次,
单测和 dev 模式不触发。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path

from daobidao.backends import IS_MACOS

_MIGRATED_MARKER = ".daobidao_migrated_from_whisper_input"


def _move_dir(old: Path, new: Path) -> bool:
    """老目录存在 + 新目录不存在 → 把老目录搬到新位置。返回是否搬运。"""
    if not old.is_dir():
        return False
    if new.exists():
        return False
    new.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        shutil.move(str(old), str(new))
        return True
    return False


def _migrate_macos() -> list[str]:
    home = Path.home()
    moved: list[str] = []

    pairs = [
        (
            home / "Library" / "Application Support" / "Whisper Input",
            home / "Library" / "Application Support" / "Daobidao",
        ),
        (
            home / "Library" / "Logs" / "Whisper Input",
            home / "Library" / "Logs" / "Daobidao",
        ),
        (
            home / "Applications" / "Whisper Input.app",
            home / "Applications" / "Daobidao.app",
        ),
    ]
    for old, new in pairs:
        if _move_dir(old, new):
            moved.append(f"{old} → {new}")

    # LaunchAgent 改写并 bootout 老 label
    old_label = "com.whisper-input"
    old_plist = home / "Library" / "LaunchAgents" / f"{old_label}.plist"
    if old_plist.is_file():
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [
                    "launchctl",
                    "bootout",
                    f"gui/{os.getuid()}/{old_label}",
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
        with contextlib.suppress(OSError):
            old_plist.unlink()
            moved.append(f"卸载旧 LaunchAgent {old_plist.name}")
    return moved


def _migrate_linux() -> list[str]:
    moved: list[str] = []
    config_home = Path(
        os.environ.get(
            "XDG_CONFIG_HOME",
            str(Path.home() / ".config"),
        )
    )
    state_home = Path(
        os.environ.get(
            "XDG_STATE_HOME",
            str(Path.home() / ".local" / "state"),
        )
    )

    pairs = [
        (config_home / "whisper-input", config_home / "daobidao"),
        (state_home / "whisper-input", state_home / "daobidao"),
    ]
    for old, new in pairs:
        if _move_dir(old, new):
            moved.append(f"{old} → {new}")

    old_desktop = config_home / "autostart" / "whisper-input.desktop"
    if old_desktop.is_file():
        with contextlib.suppress(OSError):
            old_desktop.unlink()
            moved.append(f"删除旧 autostart {old_desktop.name}")
    return moved


def migrate_once() -> list[str]:
    """幂等迁移老 whisper-input 数据。返回搬运过的条目摘要。

    用一个隐藏文件 `~/.daobidao_migrated_from_whisper_input` 当 marker,
    避免每次启动都跑一遍。
    """
    marker = Path.home() / _MIGRATED_MARKER
    if marker.exists():
        return []
    moved = _migrate_macos() if IS_MACOS else _migrate_linux()
    with contextlib.suppress(OSError):
        marker.write_text("ok\n")
    return moved
