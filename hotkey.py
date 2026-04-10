"""热键监听模块 - 运行时按平台选择后端实现。"""

from backends import IS_MACOS

if IS_MACOS:
    from backends.hotkey_macos import HotkeyListener, SUPPORTED_KEYS  # noqa: I001
else:
    from backends.hotkey_linux import HotkeyListener, SUPPORTED_KEYS  # noqa: I001

__all__ = ["HotkeyListener", "SUPPORTED_KEYS"]  # noqa: RUF022
