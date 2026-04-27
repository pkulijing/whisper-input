"""macOS 录音状态浮窗 - 深蓝药丸 + 音量跳动条。"""

import random

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFloatingWindowLevel,
    NSMakeRect,
    NSScreen,
    NSTimer,
    NSView,
    NSWindow,
)
from Foundation import NSObject

from daobidao.logger import get_logger

logger = get_logger(__name__)

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

# 错误态自动 hide 时长(s)
_ERROR_AUTO_HIDE_S = 2.5

# 深蓝 #1E3A8A
_PILL_COLOR = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    0.118, 0.227, 0.541, 1.0
)
# 32 轮:错误态红色 #DC2626(Tailwind red-600)
_ERROR_PILL_COLOR = NSColor.colorWithCalibratedRed_green_blue_alpha_(
    0.863, 0.149, 0.149, 1.0
)


class _OverlayView(NSView):
    """自定义视图：深蓝药丸 + 麦克风 + 跳动长条。"""

    bar_heights = [_BAR_REST_H] * (_BAR_COUNT * 2)
    in_error_state = False  # 32 轮:True 时画红色药丸 + 麦克风斜线

    def drawRect_(self, rect):  # noqa: N802
        w = rect.size.width
        h = rect.size.height
        cx, cy = w / 2, h / 2

        # 药丸背景
        if self.in_error_state:
            _ERROR_PILL_COLOR.setFill()
        else:
            _PILL_COLOR.setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, _PILL_R, _PILL_R
        ).fill()

        # 矢量麦克风图标（不依赖 emoji 字体；AppKit 默认 Y 轴朝上）
        white = NSColor.colorWithCalibratedRed_green_blue_alpha_(1, 1, 1, 0.95)
        white.setFill()
        white.setStroke()

        # 话筒头 (rounded rect, w=7 h=11 r=3.5)
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(cx - 3.5, cy - 3, 7, 11), 3.5, 3.5
        ).fill()

        # U 型托架（stroked polyline，圆角）
        bracket = NSBezierPath.bezierPath()
        bracket.setLineWidth_(1.6)
        bracket.setLineCapStyle_(1)  # NSLineCapStyleRound
        bracket.setLineJoinStyle_(1)  # NSLineJoinStyleRound
        bracket.moveToPoint_((cx - 5, cy - 1))
        bracket.lineToPoint_((cx - 5, cy - 5))
        bracket.lineToPoint_((cx + 5, cy - 5))
        bracket.lineToPoint_((cx + 5, cy - 1))
        bracket.stroke()

        # 连接杆
        NSBezierPath.fillRect_(NSMakeRect(cx - 0.8, cy - 8, 1.6, 3))

        # 底座
        NSBezierPath.fillRect_(NSMakeRect(cx - 3, cy - 9.6, 6, 1.6))

        if self.in_error_state:
            # 32 轮:错误态画白色对角斜线(AppKit Y 轴朝上,左下→右上 即 y 减小→y 增大)
            slash = NSBezierPath.bezierPath()
            slash.setLineWidth_(2.0)
            slash.setLineCapStyle_(1)  # round
            slash.moveToPoint_((cx - 9, cy - 9))
            slash.lineToPoint_((cx + 9, cy + 9))
            slash.stroke()
            return  # 错误态不画跳动条

        # 跳动长条
        bar_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(
            1, 1, 1, 0.9
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
        except Exception:
            logger.exception("main_thread_cb_failed")


class _ErrorHideTimerTarget(NSObject):
    """32 轮:NSTimer 的 target,fire 时调 overlay._auto_hide_error()。

    NSTimer 持有 target 强引用,target 这里再持有 overlay。timer fire 后
    本身被 invalidate(repeats=False),引用图断开,GC 回收。
    """

    def initWithOverlay_(self, overlay):  # noqa: N802
        self = objc.super(_ErrorHideTimerTarget, self).init()
        if self is None:
            return None
        self._overlay = overlay
        return self

    def fire_(self, _timer):
        try:
            self._overlay._auto_hide_error()
        except Exception:
            logger.exception("error_hide_timer_fire_failed")


class RecordingOverlay:
    """macOS 录音浮窗：深蓝药丸 + 麦克风 + 跳动长条。"""

    def __init__(self):
        self._window = None
        self._view = None
        self._pending_runners = []
        self._level = 0.0
        self._bar_heights = [_BAR_REST_H] * (_BAR_COUNT * 2)
        # 32 轮:错误态(红色 + 麦克风斜线,2.5s 自动 hide)
        self._in_error_state = False
        self._error_hide_timer = None  # NSTimer,新一次 show 时 invalidate

    def _ensure_window(self):
        if self._window is not None:
            return

        screen = NSScreen.mainScreen().frame()
        x = (screen.size.width - _W) / 2
        y = screen.size.height * 0.3

        rect = NSMakeRect(x, y, _W, _H)
        self._window = (
            NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect,
                0,
                NSBackingStoreBuffered,
                False,
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
        # 32 轮:取消老的错误态自动 hide(防 race),退回正常态
        self._cancel_error_hide()
        self._in_error_state = False
        if self._view:
            self._view.in_error_state = False
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
        self._cancel_error_hide()
        self._in_error_state = False
        if self._view:
            self._view.in_error_state = False
        if self._window:
            self._window.orderOut_(None)
        self._pending_runners.clear()

    def show_error(self, message: str) -> None:
        """32 轮:显示麦克风离线错误状态(红色药丸 + 麦克风斜线),2.5s 后自动 hide。

        ``message`` 当前不渲染到药丸内(120×34 太窄),仅日志可见。
        """
        self._perform_on_main(lambda: self._do_show_error(message))

    def _do_show_error(self, _message):
        self._ensure_window()
        self._cancel_error_hide()
        self._in_error_state = True
        if self._view:
            self._view.in_error_state = True
            self._view.setNeedsDisplay_(True)
        self._level = 0.0
        self._bar_heights = [_BAR_REST_H] * (_BAR_COUNT * 2)
        self._window.orderFront_(None)
        # NSTimer 在主 runloop 上 fire,scheduledTimer 自动入 default mode
        target = _ErrorHideTimerTarget.alloc().initWithOverlay_(self)
        scheduler = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_
        self._error_hide_timer = scheduler(
            _ERROR_AUTO_HIDE_S, target, "fire:", None, False
        )

    def _auto_hide_error(self):
        self._error_hide_timer = None
        # 若期间用户重新按了热键,_in_error_state 已被翻 False → 不 hide
        if self._in_error_state:
            self._in_error_state = False
            if self._view:
                self._view.in_error_state = False
            if self._window:
                self._window.orderOut_(None)

    def _cancel_error_hide(self):
        if self._error_hide_timer is not None:
            try:
                self._error_hide_timer.invalidate()
            except Exception:
                logger.exception("error_hide_timer_invalidate_failed")
            self._error_hide_timer = None

    def set_level(self, rms: float) -> None:
        """接收实时音量，更新跳动长条。"""
        # 32 轮:错误态期间忽略 RMS,避免 race 把红色药丸刷掉
        if self._in_error_state:
            return
        normalized = min(1.0, rms / _RMS_SCALE)
        self._level = max(normalized, self._level * _DECAY)
        level = self._level
        for i in range(_BAR_COUNT * 2):
            if level > 0.02:
                target = _BAR_REST_H + level * (_BAR_MAX_H - _BAR_REST_H)
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
