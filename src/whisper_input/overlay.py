"""录音浮窗调度器 - 按平台导入对应实现。"""

from whisper_input.backends import IS_MACOS

if IS_MACOS:
    from whisper_input.backends.overlay_macos import RecordingOverlay
else:
    from whisper_input.backends.overlay_linux import RecordingOverlay

__all__ = ["RecordingOverlay"]
