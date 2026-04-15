"""热键监听模块 - 运行时按平台选择后端实现。"""

from whisper_input.backends import IS_MACOS

if IS_MACOS:
    from whisper_input.backends.hotkey_macos import HotkeyListener, SUPPORTED_KEYS  # noqa: I001
else:
    from whisper_input.backends.hotkey_linux import HotkeyListener, SUPPORTED_KEYS  # noqa: I001

__all__ = ["HotkeyListener", "SUPPORTED_KEYS"]  # noqa: RUF022
