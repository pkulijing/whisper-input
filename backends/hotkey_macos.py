"""热键监听模块 (macOS) - 使用 pynput 监听全局键盘事件。

需要在「系统设置 > 隐私与安全性 > 辅助功能」中授权终端或应用。
"""

import os
import subprocess
import threading
from collections.abc import Callable

from pynput.keyboard import Key, Listener


def check_macos_permissions() -> bool:
    """检查 macOS 权限，缺失时分步引导用户设置并重启。

    需要两个权限：
    - 辅助功能 (Accessibility)：AXIsProcessTrusted() 检测
    - 输入监控 (Input Monitoring)：CGPreflightListenEventAccess() 检测

    返回 True 表示权限已就绪，False 表示用户取消。
    """
    import sys

    try:
        from ApplicationServices import AXIsProcessTrusted
        from Quartz import (
            CGPreflightListenEventAccess,
            CGRequestListenEventAccess,
        )
    except ImportError:
        return True  # pyobjc 未安装，跳过

    accessibility_ok = AXIsProcessTrusted()
    input_monitoring_ok = CGPreflightListenEventAccess()

    # 主动请求输入监控权限，让系统把应用添加到列表中
    if not input_monitoring_ok:
        CGRequestListenEventAccess()

    if accessibility_ok and input_monitoring_ok:
        return True

    # 收集缺失的权限（输入监控优先）
    missing = []
    if not input_monitoring_ok:
        missing.append((
            "输入监控",
            "x-apple.systempreferences:"
            "com.apple.preference.security"
            "?Privacy_ListenEvent",
        ))
    if not accessibility_ok:
        missing.append((
            "辅助功能",
            "x-apple.systempreferences:"
            "com.apple.preference.security"
            "?Privacy_Accessibility",
        ))

    total = len(missing)
    for i, (name, settings_url) in enumerate(missing, 1):
        print(f"[权限] 未授予{name}权限，引导用户设置...")

        # 引导弹窗
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display dialog '
                f'"请在「{name}」中找到 Whisper Input 并启用。" '
                f'buttons {{"取消", "打开设置"}} '
                f'default button 2 '
                f'with title "Whisper Input 权限设置 ({i}/{total})" '
                f"with icon caution",
            ],
            capture_output=True,
        )

        subprocess.run(["open", settings_url])

        # 等待用户操作完毕
        btn = "下一步" if i < total else "已授权，重新启动"
        result = subprocess.run(
            [
                "osascript",
                "-e",
                f'display dialog '
                f'"在系统设置中启用后，点击「{btn}」继续。" '
                f'buttons {{"取消", "{btn}"}} '
                f'default button 2 '
                f'with title "Whisper Input 权限设置 ({i}/{total})" '
                f"with icon caution",
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            return False

    # 重启以应用权限
    print("[权限] 正在重启...")
    os.execv(sys.executable, [sys.executable, *sys.argv])

# 支持的热键映射
# 注意: pynput 中左侧修饰键不带 _l 后缀（Key.ctrl, Key.alt, Key.cmd）
SUPPORTED_KEYS = {
    "KEY_RIGHTCTRL": Key.ctrl_r,
    "KEY_LEFTCTRL": Key.ctrl,
    "KEY_RIGHTALT": Key.alt_r,  # 右 Option
    "KEY_LEFTALT": Key.alt,  # 左 Option
    "KEY_RIGHTMETA": Key.cmd_r,  # 右 Command
    "KEY_LEFTMETA": Key.cmd,  # 左 Command
    "KEY_CAPSLOCK": Key.caps_lock,
    "KEY_F1": Key.f1,
    "KEY_F2": Key.f2,
    "KEY_F5": Key.f5,
    "KEY_F12": Key.f12,
}

# 修饰键集合（需要延迟触发以避免组合键冲突）
_MODIFIER_KEYS = {
    Key.ctrl_r,
    Key.ctrl,
    Key.alt_r,
    Key.alt,
    Key.cmd_r,
    Key.cmd,
}

# 组合键延迟（秒）
COMBO_DELAY = 0.3


class HotkeyListener:
    """监听键盘热键的按下和释放事件。

    使用 pynput 全局键盘监听，需要辅助功能权限。

    对于修饰键（Ctrl/Alt/Command/Fn），使用延迟触发机制避免与组合键冲突：
    按下热键后等待 COMBO_DELAY 秒，期间如果有其他键按下则视为组合键，
    不触发录音。
    """

    def __init__(
        self,
        hotkey: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ):
        key_obj = SUPPORTED_KEYS.get(hotkey)
        if key_obj is None:
            raise ValueError(
                f"不支持的热键: {hotkey}，"
                f"支持的热键: {list(SUPPORTED_KEYS.keys())}"
            )

        self.key_obj = key_obj
        self.hotkey_name = hotkey
        self.on_press = on_press
        self.on_release = on_release
        self._listener: Listener | None = None

        # 热键状态
        self._pressed = False
        self._activated = False
        self._cancelled = False
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

        # 判断是否为修饰键
        self._is_modifier = key_obj in _MODIFIER_KEYS

    def start(self) -> None:
        """开始监听热键。"""
        if self._listener is not None:
            return

        print(f"[hotkey] 正在监听热键: {self.hotkey_name}")

        try:
            self._listener = Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
            )
            self._listener.start()
        except Exception as e:
            print(f"[hotkey] 启动键盘监听失败: {e}")
            print("[hotkey] 请在「系统设置 > 隐私与安全性 > 辅助功能」中")
            print("  授权当前终端或应用程序访问权限")

    def stop(self) -> None:
        """停止监听。"""
        if self._timer:
            self._timer.cancel()
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _key_matches(self, key) -> bool:
        """检查按键是否匹配目标热键。"""
        return key == self.key_obj

    def _on_key_press(self, key) -> None:
        """pynput 按键按下回调。"""
        if self._key_matches(key):
            if not self._pressed:
                self._on_hotkey_press()
        elif self._pressed and not self._activated:
            # 热键按住期间有其他键按下 → 组合键
            self._on_combo_detected()

    def _on_key_release(self, key) -> None:
        """pynput 按键释放回调。"""
        if self._key_matches(key) and self._pressed:
            self._on_hotkey_release()

    def _on_delayed_press(self) -> None:
        """延迟触发：确认不是组合键后激活录音。"""
        with self._lock:
            if not self._pressed or self._cancelled:
                return
            self._activated = True
        self.on_press()

    def _on_hotkey_press(self) -> None:
        """热键按下处理。"""
        with self._lock:
            self._pressed = True
            self._activated = False
            self._cancelled = False

        if self._is_modifier:
            self._timer = threading.Timer(
                COMBO_DELAY, self._on_delayed_press
            )
            self._timer.start()
        else:
            self._activated = True
            self.on_press()

    def _on_hotkey_release(self) -> None:
        """热键释放处理。"""
        with self._lock:
            self._pressed = False
            was_activated = self._activated
            self._activated = False

            if self._timer:
                self._timer.cancel()
                self._timer = None

        if was_activated:
            self.on_release()

    def _on_combo_detected(self) -> None:
        """检测到组合键，取消触发。"""
        with self._lock:
            self._cancelled = True
            if self._timer:
                self._timer.cancel()
                self._timer = None
