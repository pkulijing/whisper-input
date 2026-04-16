"""系统托盘模块 - 运行时按平台选择后端实现。"""

from whisper_input.backends import IS_MACOS

if IS_MACOS:
    from whisper_input.backends.tray_macos import run_tray
else:
    from whisper_input.backends.tray_linux import run_tray

__all__ = ["run_tray"]
