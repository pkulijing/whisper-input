"""热键监听模块 (macOS) - 使用 pynput 监听全局键盘事件。

需要在「系统设置 > 隐私与安全性 > 辅助功能」中授权终端或应用。
"""

import os
import sys
import threading
from collections.abc import Callable

from pynput.keyboard import Key, Listener

from daobidao.i18n import t
from daobidao.logger import get_logger

logger = get_logger(__name__)


def check_macos_permissions() -> bool:
    """检查 macOS 辅助功能权限，缺失时触发系统弹窗并等待授权。

    只需要 Accessibility。我们用 pynput 的 kCGSessionEventTap +
    kCGEventTapOptionListenOnly 组合，Accessibility 就够了，不需要
    Input Monitoring（后者是 kCGHIDEventTap 才需要的）。

    实现要点：
    - 轮询用 CFRunLoopRunInMode 而非 time.sleep，让 LaunchServices 启动
      事件能被处理（否则 open -a 会超时 -1712），TCC 弹窗也需要 runloop
      才能显示。
    - 授权到位后自动重启：event tap 创建时绑定权限状态，新权限必须
      在新进程里才能生效。
    """
    try:
        from ApplicationServices import (
            AXIsProcessTrusted,
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
        from CoreFoundation import (
            CFRunLoopRunInMode,
            kCFRunLoopDefaultMode,
        )
    except ImportError:
        return True  # pyobjc 未安装，跳过

    if AXIsProcessTrusted():
        return True

    logger.warning(
        "perm_need_grant",
        names=t("perm.accessibility"),
        message=t("perm.need_grant", names=t("perm.accessibility")),
    )
    AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    logger.info("perm_waiting_for_grant", message=t("perm.waiting_for_grant"))
    while not AXIsProcessTrusted():
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.5, False)

    logger.info(
        "perm_granted_restarting",
        message=t("perm.granted_restarting"),
    )
    from daobidao.backends.app_bundle_macos import (
        BUNDLE_ENV_KEY,
        restart_via_bundle,
    )

    if os.environ.get(BUNDLE_ENV_KEY):
        restart_via_bundle()
    else:
        os.execv(sys.executable, [sys.executable, *sys.argv])
    return True  # unreachable


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
                f"Unsupported hotkey: {hotkey}, "
                f"supported: {list(SUPPORTED_KEYS.keys())}"
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

        logger.info(
            "hotkey_listening",
            hotkey=self.hotkey_name,
            message=t("hotkey.listening", hotkey=self.hotkey_name),
        )

        try:
            self._listener = Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
            )
            self._listener.start()
        except Exception:
            logger.exception(
                "hotkey_listen_failed",
                hint=t("hotkey.accessibility_hint"),
                action=t("hotkey.grant_access"),
            )

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
            self._timer = threading.Timer(COMBO_DELAY, self._on_delayed_press)
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
