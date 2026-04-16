"""Linux 录音状态浮窗 - 深蓝药丸 + 音量跳动条 (GTK3 + Cairo)。"""

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
        cr.set_source_rgb(*_PILL_RGB)
        cr.fill()

        # 麦克风 emoji 居中
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.select_font_face(
            "sans-serif", 0, 0  # NORMAL, NORMAL
        )
        cr.set_font_size(15)
        extents = cr.text_extents("\U0001f399")
        tx = cx - extents.width / 2 - extents.x_bearing
        ty = cy - extents.height / 2 - extents.y_bearing
        cr.move_to(tx, ty)
        cr.show_text("\U0001f399")

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
        if self._window:
            self._window.hide()
        return False

    def set_level(self, rms: float) -> None:
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
        GLib.idle_add(self._do_redraw)

    def _do_redraw(self):
        if self._drawing_area:
            self._drawing_area.queue_draw()
        return False
