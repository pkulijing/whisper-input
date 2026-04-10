"""热键监听模块 - 使用 evdev 监听键盘事件，支持区分左右修饰键。"""

import contextlib
import select
import threading
from collections.abc import Callable

import evdev
from evdev import ecodes

# 支持的热键映射
SUPPORTED_KEYS = {
    "KEY_RIGHTCTRL": ecodes.KEY_RIGHTCTRL,
    "KEY_LEFTCTRL": ecodes.KEY_LEFTCTRL,
    "KEY_RIGHTALT": ecodes.KEY_RIGHTALT,
    "KEY_LEFTALT": ecodes.KEY_LEFTALT,
    "KEY_RIGHTMETA": ecodes.KEY_RIGHTMETA,  # 右Win/Super键
    "KEY_LEFTMETA": ecodes.KEY_LEFTMETA,  # 左Win/Super键
    "KEY_CAPSLOCK": ecodes.KEY_CAPSLOCK,
    "KEY_F1": ecodes.KEY_F1,
    "KEY_F2": ecodes.KEY_F2,
    "KEY_F12": ecodes.KEY_F12,
}

# 组合键延迟（秒）：按下热键后等待此时间，期间无其他键按下才触发录音
COMBO_DELAY = 0.3


def find_keyboard_devices() -> list[evdev.InputDevice]:
    """查找所有键盘设备。"""
    keyboards = []
    for path in evdev.list_devices():
        try:
            device = evdev.InputDevice(path)
            caps = device.capabilities(verbose=False)
            # EV_KEY 事件类型 = 1
            if ecodes.EV_KEY in caps:
                key_caps = caps[ecodes.EV_KEY]
                # 检查是否有常见的键盘按键（字母键）
                if ecodes.KEY_A in key_caps and ecodes.KEY_Z in key_caps:
                    keyboards.append(device)
                    print(f"[hotkey] 发现键盘: {device.name} ({device.path})")
        except (PermissionError, OSError):
            continue
    return keyboards


class HotkeyListener:
    """监听键盘热键的按下和释放事件。

    使用 evdev 直接读取键盘设备，可以区分左右修饰键。
    需要 root 权限或将用户加入 input 组。

    对于修饰键（Ctrl/Alt/Meta），使用延迟触发机制避免与组合键冲突：
    按下热键后等待 COMBO_DELAY 秒，期间如果有其他键按下则视为组合键，
    不触发录音。
    """

    def __init__(
        self,
        hotkey: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ):
        key_code = SUPPORTED_KEYS.get(hotkey)
        if key_code is None:
            raise ValueError(
                f"不支持的热键: {hotkey}，"
                f"支持的热键: {list(SUPPORTED_KEYS.keys())}"
            )

        self.key_code = key_code
        self.hotkey_name = hotkey
        self.on_press = on_press
        self.on_release = on_release
        self._running = False
        self._thread: threading.Thread | None = None

        # 热键状态
        self._pressed = False
        # 是否已激活录音（延迟确认后）
        self._activated = False
        # 是否被组合键取消
        self._cancelled = False
        # 延迟触发定时器
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

        # 判断是否为修饰键（需要延迟触发）
        self._is_modifier = key_code in {
            ecodes.KEY_RIGHTCTRL,
            ecodes.KEY_LEFTCTRL,
            ecodes.KEY_RIGHTALT,
            ecodes.KEY_LEFTALT,
            ecodes.KEY_RIGHTMETA,
            ecodes.KEY_LEFTMETA,
        }

    def start(self) -> None:
        """开始监听热键（在后台线程中运行）。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止监听。"""
        self._running = False
        if self._timer:
            self._timer.cancel()
        if self._thread:
            self._thread.join(timeout=2)

    def _on_delayed_press(self) -> None:
        """延迟触发：确认不是组合键后激活录音。"""
        with self._lock:
            if not self._pressed or self._cancelled:
                return
            self._activated = True
        self.on_press()

    def _listen_loop(self) -> None:
        """监听循环。"""
        keyboards = find_keyboard_devices()
        if not keyboards:
            print("[hotkey] 错误: 未找到键盘设备。请确保:")
            print("  1. 以 root 运行，或")
            print("  2. 将用户加入 input 组: sudo usermod -aG input $USER")
            return

        print(f"[hotkey] 正在监听热键: {self.hotkey_name}")

        while self._running:
            # 使用 select 监听多个键盘设备
            r, _, _ = select.select(keyboards, [], [], 0.5)
            for device in r:
                try:
                    for event in device.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        self._handle_key_event(event)
                except (OSError, BlockingIOError):
                    continue

        # 清理
        for kb in keyboards:
            with contextlib.suppress(Exception):
                kb.close()

    def _handle_key_event(self, event) -> None:
        """处理单个按键事件。"""
        if event.code == self.key_code:
            if event.value == 1 and not self._pressed:
                # 热键按下
                self._on_hotkey_press()
            elif event.value == 0 and self._pressed:
                # 热键释放
                self._on_hotkey_release()
            # value == 2 是按键重复，忽略
        elif event.value == 1 and self._pressed and not self._activated:
            # 热键按住期间有其他键按下 → 组合键，取消触发
            self._on_combo_detected()

    def _on_hotkey_press(self) -> None:
        """热键按下处理。"""
        with self._lock:
            self._pressed = True
            self._activated = False
            self._cancelled = False

        if self._is_modifier:
            # 修饰键：延迟触发，等待确认不是组合键
            self._timer = threading.Timer(COMBO_DELAY, self._on_delayed_press)
            self._timer.start()
        else:
            # 非修饰键（F1/F2等）：立即触发
            self._activated = True
            self.on_press()

    def _on_hotkey_release(self) -> None:
        """热键释放处理。"""
        with self._lock:
            self._pressed = False
            was_activated = self._activated
            self._activated = False

            # 取消未触发的定时器
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
