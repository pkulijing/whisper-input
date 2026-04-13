"""macOS 录音状态浮窗 - emoji 麦克风 + 动态波纹。"""

from math import pi

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

# 浮窗尺寸
_W, _H = 120, 120
# 波纹消退速度
_DECAY = 0.85
# RMS 归一化系数
_RMS_SCALE = 3000.0


class _OverlayView(NSView):
    """自定义视图：圆角背景 + emoji 麦克风 + 波纹。"""

    level = 0.0  # 0.0 ~ 1.0 归一化音量

    def drawRect_(self, rect):  # noqa: N802
        w = rect.size.width
        h = rect.size.height
        cx, cy = w / 2, h / 2

        # 1. 圆角半透明背景
        bg = NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0, 0, 0, 0.75
        )
        bg.setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, 20, 20
        ).fill()

        # 2. emoji 麦克风图标（居中）
        emoji = NSString.stringWithString_("\U0001f399")
        font = NSFont.systemFontOfSize_(48)
        para = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(1)  # NSTextAlignmentCenter
        attrs = {
            "NSFont": font,
            "NSParagraphStyle": para,
        }
        emoji_size = emoji.sizeWithAttributes_(attrs)
        emoji_rect = NSMakeRect(
            cx - emoji_size.width / 2,
            cy - emoji_size.height / 2,
            emoji_size.width,
            emoji_size.height,
        )
        emoji.drawInRect_withAttributes_(emoji_rect, attrs)

        # 3. 波纹（仅有声音时显示）
        level = self.level
        if level > 0.02:
            for i in range(3):
                ring_r = 38 + i * 10
                alpha = max(0, level * (1.0 - i * 0.3))
                if alpha < 0.05:
                    continue
                ring_color = (
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        1, 1, 1, alpha * 0.6
                    )
                )
                ring_color.setStroke()
                ring = NSBezierPath.bezierPath()
                # 左右两侧弧形波纹
                for start, end in [(pi * 0.6, pi * 1.4),
                                   (-pi * 0.4, pi * 0.4)]:
                    ring.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                        (cx, cy),
                        ring_r,
                        start * 180 / pi,
                        end * 180 / pi,
                    )
                ring.setLineWidth_(2.5)
                ring.stroke()


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
    """macOS 录音浮窗。"""

    def __init__(self):
        self._window = None
        self._view = None
        self._pending_runners = []
        self._level = 0.0

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
        self._view.level = 0.0
        self._view.setNeedsDisplay_(True)
        self._window.orderFront_(None)

    def update(self, text: str) -> None:
        self._perform_on_main(self._do_fade_out)

    def _do_fade_out(self):
        if self._view:
            self._view.level = 0.0
            self._view.setNeedsDisplay_(True)

    def hide(self) -> None:
        self._perform_on_main(self._do_hide)

    def _do_hide(self):
        if self._window:
            self._window.orderOut_(None)
        self._pending_runners.clear()

    def set_level(self, rms: float) -> None:
        """接收实时音量，更新波纹。"""
        normalized = min(1.0, rms / _RMS_SCALE)
        self._level = max(normalized, self._level * _DECAY)
        self._perform_on_main(self._do_update_level)

    def _do_update_level(self):
        if self._view:
            self._view.level = self._level
            self._view.setNeedsDisplay_(True)

    def _perform_on_main(self, block):
        runner = _MainThreadRunner.alloc().initWithBlock_(block)
        self._pending_runners.append(runner)
        runner.performSelectorOnMainThread_withObject_waitUntilDone_(
            "run:", None, False
        )
