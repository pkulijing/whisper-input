"""文字输入模块 - 运行时按平台选择后端实现。"""

from whisper_input.backends import IS_MACOS

if IS_MACOS:
    from whisper_input.backends.input_macos import type_text
else:
    from whisper_input.backends.input_linux import type_text

__all__ = ["type_text"]
