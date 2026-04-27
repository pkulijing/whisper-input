"""Linux 录音状态浮窗 - 深蓝药丸 + 音量跳动条 (GTK3 + Cairo)。"""

import contextlib
import random
from math import pi

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

# 药丸尺寸
_W, _H = 120, 34
_PILL_R = 17
# 深蓝 #1E3A8A
_PILL_RGB = (0.118, 0.227, 0.541)
# 32 轮:错误态药丸红色 #DC2626(Tailwind red-600)
_ERROR_PILL_RGB = (0.863, 0.149, 0.149)
# 错误态浮窗自动 hide 时长(ms)
_ERROR_AUTO_HIDE_MS = 2500

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


class RecordingOverlay:
    """Linux 录音浮窗：深蓝药丸 + 麦克风 + 跳动长条。"""

    def __init__(self):
        self._window = None
        self._level = 0.0
        self._bar_heights = [_BAR_REST_H] * (_BAR_COUNT * 2)
        self._drawing_area = None
        # 32 轮:错误态(红色 + 麦克风斜线,跳动条不画);_error_hide_source_id
        # 是 GLib.timeout_add 返回的 source id,新一次 show() 时 source_remove
        # 防止 race(老错误态把刚显示的正常浮窗 hide 掉)。
        self._in_error_state = False
        self._error_hide_source_id: int | None = None

    def _ensure_window(self):
        if self._window is not None:
            return

        self._window = Gtk.Window(type=Gtk.WindowType.POPUP)
        self._window.set_decorated(False)
        self._window.set_keep_above(True)
        self._window.set_accept_focus(False)
        self._window.set_app_paintable(True)
        self._window.set_default_size(_W, _H)

        screen = self._window.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self._window.set_visual(visual)

        self._drawing_area = Gtk.DrawingArea()
        self._drawing_area.set_size_request(_W, _H)
        self._drawing_area.connect("draw", self._on_draw)
        self._window.add(self._drawing_area)
        self._drawing_area.show()

    def _on_draw(self, _widget, cr):
        w, h = _W, _H
        cx, cy = w / 2, h / 2
        r = _PILL_R

        # 清除
        cr.set_operator(0)  # CLEAR
        cr.paint()
        cr.set_operator(2)  # OVER

        # 药丸背景（圆角矩形，左右半圆帽）
        cr.new_sub_path()
        cr.arc(w - r, r, r, -pi / 2, 0)
        cr.arc(w - r, h - r, r, 0, pi / 2)
        cr.arc(r, h - r, r, pi / 2, pi)
        cr.arc(r, r, r, pi, 3 * pi / 2)
        cr.close_path()
        # 32 轮:错误态切红色背景
        if self._in_error_state:
            cr.set_source_rgb(*_ERROR_PILL_RGB)
        else:
            cr.set_source_rgb(*_PILL_RGB)
        cr.fill()

        # 矢量麦克风图标（不依赖 emoji 字体）
        cr.set_source_rgba(1, 1, 1, 0.95)

        # 话筒头 (rounded rect, w=7 h=11 r=3.5)
        cap_left = cx - 3.5
        cap_top = cy - 8
        cr.new_sub_path()
        cr.arc(cap_left + 7 - 3.5, cap_top + 3.5, 3.5, -pi / 2, 0)
        cr.arc(cap_left + 7 - 3.5, cap_top + 11 - 3.5, 3.5, 0, pi / 2)
        cr.arc(cap_left + 3.5, cap_top + 11 - 3.5, 3.5, pi / 2, pi)
        cr.arc(cap_left + 3.5, cap_top + 3.5, 3.5, pi, 3 * pi / 2)
        cr.close_path()
        cr.fill()

        # U 型托架（stroked polyline，圆角）
        cr.set_line_width(1.6)
        cr.set_line_cap(1)  # ROUND
        cr.set_line_join(1)  # ROUND
        cr.new_path()
        cr.move_to(cx - 5, cy + 1)
        cr.line_to(cx - 5, cy + 5)
        cr.line_to(cx + 5, cy + 5)
        cr.line_to(cx + 5, cy + 1)
        cr.stroke()

        # 连接杆
        cr.rectangle(cx - 0.8, cy + 5, 1.6, 3)
        cr.fill()

        # 底座
        cr.rectangle(cx - 3, cy + 8, 6, 1.6)
        cr.fill()

        if self._in_error_state:
            # 32 轮:错误态画白色对角斜线(左下 → 右上),压住麦克风图标
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.set_line_width(2.0)
            cr.set_line_cap(1)  # ROUND
            cr.new_path()
            cr.move_to(cx - 9, cy + 9)
            cr.line_to(cx + 9, cy - 9)
            cr.stroke()
            # 错误态不画跳动条
            return False

        # 跳动长条
        cr.set_source_rgba(1, 1, 1, 0.9)
        for i in range(_BAR_COUNT):
            bh = self._bar_heights[i]
            x = cx - _BAR_OFFSET - i * (_BAR_W + _BAR_GAP) - _BAR_W
            y = cy - bh / 2
            cr.rectangle(x, y, _BAR_W, bh)
            cr.fill()
        for i in range(_BAR_COUNT):
            bh = self._bar_heights[_BAR_COUNT + i]
            x = cx + _BAR_OFFSET + i * (_BAR_W + _BAR_GAP)
            y = cy - bh / 2
            cr.rectangle(x, y, _BAR_W, bh)
            cr.fill()

        return False

    def _center_window(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor()
        if monitor is None:
            monitor = display.get_monitor(0)
        geom = monitor.get_geometry()
        x = geom.x + (geom.width - _W) // 2
        y = geom.y + int(geom.height * 0.7)
        self._window.move(x, y)

    def show(self) -> None:
        GLib.idle_add(self._do_show)

    def _do_show(self):
        self._ensure_window()
        # 32 轮:新一轮正常 show 时取消老的错误态自动 hide(防 race)
        self._cancel_error_hide()
        self._in_error_state = False
        self._level = 0.0
        self._bar_heights = [_BAR_REST_H] * (_BAR_COUNT * 2)
        self._center_window()
        self._window.show()
        if self._drawing_area:
            self._drawing_area.queue_draw()
        return False

    def update(self, text: str) -> None:
        GLib.idle_add(self._do_fade_out)

    def _do_fade_out(self):
        self._level = 0.0
        self._bar_heights = [_BAR_REST_H] * (_BAR_COUNT * 2)
        if self._drawing_area:
            self._drawing_area.queue_draw()
        return False

    def hide(self) -> None:
        GLib.idle_add(self._do_hide)

    def _do_hide(self):
        self._cancel_error_hide()
        self._in_error_state = False
        if self._window:
            self._window.hide()
        return False

    def show_error(self, message: str) -> None:
        """32 轮:显示麦克风离线错误状态(红色药丸 + 麦克风斜线),2.5s 后自动 hide。

        ``message`` 当前不渲染到药丸内(120×34 太窄),只通过日志暴露给用户。
        """
        GLib.idle_add(self._do_show_error, message)

    def _do_show_error(self, _message: str):
        self._ensure_window()
        # 取消上一次错误态的 timeout(连续告警时不让旧 timeout 把新错误态关掉)
        self._cancel_error_hide()
        self._in_error_state = True
        self._level = 0.0
        self._bar_heights = [_BAR_REST_H] * (_BAR_COUNT * 2)
        self._center_window()
        self._window.show()
        if self._drawing_area:
            self._drawing_area.queue_draw()
        # 2.5s 后自动 hide;source_id 留着方便 source_remove
        self._error_hide_source_id = GLib.timeout_add(
            _ERROR_AUTO_HIDE_MS, self._auto_hide_error
        )
        return False

    def _auto_hide_error(self):
        self._error_hide_source_id = None
        # 若期间用户重新按了热键,_in_error_state 已被翻 False → 不 hide
        if self._in_error_state:
            self._in_error_state = False
            if self._window:
                self._window.hide()
        return False  # 一次性 timeout,不要重复

    def _cancel_error_hide(self):
        if self._error_hide_source_id is not None:
            with contextlib.suppress(Exception):
                GLib.source_remove(self._error_hide_source_id)
            self._error_hide_source_id = None

    def set_level(self, rms: float) -> None:
        # 32 轮:错误态期间忽略 RMS 推送,避免 race 把红色药丸刷成跳动条
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
        GLib.idle_add(self._do_redraw)

    def _do_redraw(self):
        if self._drawing_area:
            self._drawing_area.queue_draw()
        return False
