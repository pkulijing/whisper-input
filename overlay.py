"""录音浮窗调度器 - 按平台导入对应实现。"""

from backends import IS_MACOS

if IS_MACOS:
    from backends.overlay_macos import RecordingOverlay
else:
    from backends.overlay_linux import RecordingOverlay

__all__ = ["RecordingOverlay"]
