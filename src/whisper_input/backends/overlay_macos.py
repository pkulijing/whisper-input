"""macOS 录音状态浮窗 - 深蓝药丸 + 音量跳动条。"""

import random

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFloatingWindowLevel,
    NSFont,
    NSMakeRect,
    NSMutableParagraphStyle,
    NSScreen,
    NSString,
    NSView,
    NSWindow,
)
from Foundation import NSObject

# 药丸尺寸
_W, _H = 120, 34
_PILL_R = 17

# 音量条参数
_BAR_COUNT = 3  # 每侧
_BAR_W = 3
_BAR_GAP = 5
_BAR_REST_H = 4
_BAR_MAX_H = 15
_BAR_OFFSET = 17  # 距中心的起始偏移

# 音量归一化
_DECAY = 0.85
_RMS_SCALE = 3000.0

# 深蓝 #1E3A8A
_PILL_COLOR = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    0.118, 0.227, 0.541, 1.0
)


class _OverlayView(NSView):
    """自定义视图：深蓝药丸 + 麦克风 + 跳动长条。"""

    bar_heights = [_BAR_REST_H] * (_BAR_COUNT * 2)

    def drawRect_(self, rect):  # noqa: N802
        w = rect.size.width
        h = rect.size.height
        cx, cy = w / 2, h / 2

        # 药丸背景
        _PILL_COLOR.setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, _PILL_R, _PILL_R
        ).fill()

        # 麦克风 emoji 居中
        emoji = NSString.stringWithString_("\U0001f399")
        font = NSFont.systemFontOfSize_(15)
        para = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(1)  # NSTextAlignmentCenter
        attrs = {
            "NSFont": font,
            "NSParagraphStyle": para,
            "NSColor": NSColor.colorWithCalibratedRed_green_blue_alpha_(
                1, 1, 1, 0.95
            ),
        }
        emoji_size = emoji.sizeWithAttributes_(attrs)
        emoji_rect = NSMakeRect(
            cx - emoji_size.width / 2,
            cy - emoji_size.height / 2,
            emoji_size.width,
            emoji_size.height,
        )
        emoji.drawInRect_withAttributes_(emoji_rect, attrs)

        # 跳动长条
        bar_color = (
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                1, 1, 1, 0.9
            )
        )
        bar_color.setFill()
        heights = self.bar_heights
        for i in range(_BAR_COUNT):
            bh = heights[i]
            x = cx - _BAR_OFFSET - i * (_BAR_W + _BAR_GAP) - _BAR_W
            y = cy - bh / 2
            NSBezierPath.fillRect_(NSMakeRect(x, y, _BAR_W, bh))
        for i in range(_BAR_COUNT):
            bh = heights[_BAR_COUNT + i]
            x = cx + _BAR_OFFSET + i * (_BAR_W + _BAR_GAP)
            y = cy - bh / 2
            NSBezierPath.fillRect_(NSMakeRect(x, y, _BAR_W, bh))


class _MainThreadRunner(NSObject):
    """通过 performSelectorOnMainThread 在主线程执行回调。"""

    def initWithBlock_(self, block):  # noqa: N802
        self = objc.super(_MainThreadRunner, self).init()
        if self is None:
            return None
        self._block = block
        return self

    def run_(self, _arg):
        try:
            self._block()
        except Exception as e:
            print(f"[overlay] 主线程回调异常: {e}")


class RecordingOverlay:
    """macOS 录音浮窗：深蓝药丸 + 麦克风 + 跳动长条。"""

    def __init__(self):
        self._window = None
        self._view = None
        self._pending_runners = []
        self._level = 0.0
        self._bar_heights = [_BAR_REST_H] * (_BAR_COUNT * 2)

    def _ensure_window(self):
        if self._window is not None:
            return

        screen = NSScreen.mainScreen().frame()
        x = (screen.size.width - _W) / 2
        y = screen.size.height * 0.3

        rect = NSMakeRect(x, y, _W, _H)
        self._window = (
            NSWindow.alloc()
            .initWithContentRect_styleMask_backing_defer_(
                rect, 0, NSBackingStoreBuffered, False,
            )
        )
        self._window.setLevel_(NSFloatingWindowLevel)
        self._window.setOpaque_(False)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setIgnoresMouseEvents_(True)
        self._window.setHasShadow_(True)

        self._view = _OverlayView.alloc().initWithFrame_(
            NSMakeRect(0, 0, _W, _H)
        )
        self._window.setContentView_(self._view)

    def show(self) -> None:
        self._perform_on_main(self._do_show)

    def _do_show(self):
        self._ensure_window()
        self._level = 0.0
        self._bar_heights = [_BAR_REST_H] * (_BAR_COUNT * 2)
        self._view.bar_heights = self._bar_heights
        self._view.setNeedsDisplay_(True)
        self._window.orderFront_(None)

    def update(self, text: str) -> None:
        self._perform_on_main(self._do_fade_out)

    def _do_fade_out(self):
        self._level = 0.0
        self._bar_heights = [_BAR_REST_H] * (_BAR_COUNT * 2)
        if self._view:
            self._view.bar_heights = self._bar_heights
            self._view.setNeedsDisplay_(True)

    def hide(self) -> None:
        self._perform_on_main(self._do_hide)

    def _do_hide(self):
        if self._window:
            self._window.orderOut_(None)
        self._pending_runners.clear()

    def set_level(self, rms: float) -> None:
        """接收实时音量，更新跳动长条。"""
        normalized = min(1.0, rms / _RMS_SCALE)
        self._level = max(normalized, self._level * _DECAY)
        level = self._level
        for i in range(_BAR_COUNT * 2):
            if level > 0.02:
                target = _BAR_REST_H + level * (
                    _BAR_MAX_H - _BAR_REST_H
                )
                jitter = random.uniform(0.5, 1.2)
                self._bar_heights[i] = max(
                    _BAR_REST_H,
                    min(_BAR_MAX_H, target * jitter),
                )
            else:
                self._bar_heights[i] = _BAR_REST_H
        self._perform_on_main(self._do_update_bars)

    def _do_update_bars(self):
        if self._view:
            self._view.bar_heights = self._bar_heights
            self._view.setNeedsDisplay_(True)

    def _perform_on_main(self, block):
        runner = _MainThreadRunner.alloc().initWithBlock_(block)
        self._pending_runners.append(runner)
        runner.performSelectorOnMainThread_withObject_waitUntilDone_(
            "run:", None, False
        )
