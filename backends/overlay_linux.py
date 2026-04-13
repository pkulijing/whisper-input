"""Linux 录音状态浮窗 - 矢量麦克风 + 动态波纹 (GTK3 + Cairo)。"""

from math import pi

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

# 浮窗尺寸
_W, _H = 120, 120
# 波纹消退速度
_DECAY = 0.85
# RMS 归一化系数
_RMS_SCALE = 3000.0


def _draw_mic(cr, cx, cy):
    """用 Cairo 路径绘制复古录音室麦克风 (🎙 风格)。"""
    # --- 麦克风头部（圆形网格） ---
    head_r = 16
    head_cy = cy - 10

    # 外圈
    cr.set_source_rgba(0.85, 0.85, 0.85, 1.0)
    cr.arc(cx, head_cy, head_r, 0, 2 * pi)
    cr.fill()

    # 内圈深色
    cr.set_source_rgba(0.3, 0.3, 0.3, 1.0)
    cr.arc(cx, head_cy, head_r - 3, 0, 2 * pi)
    cr.fill()

    # 网格点阵模拟录音室麦克风
    cr.set_source_rgba(0.7, 0.7, 0.7, 0.8)
    for row in range(-2, 3):
        for col in range(-2, 3):
            dx = col * 5
            dy = row * 5
            if dx * dx + dy * dy <= (head_r - 5) ** 2:
                cr.arc(cx + dx, head_cy + dy, 1.5, 0, 2 * pi)
                cr.fill()

    # 外圈高光描边
    cr.set_source_rgba(1.0, 1.0, 1.0, 0.3)
    cr.set_line_width(1.5)
    cr.arc(cx, head_cy, head_r, 0, 2 * pi)
    cr.stroke()

    # --- 支架（竖杆） ---
    bar_top = head_cy + head_r
    bar_bottom = bar_top + 14
    bar_w = 3

    cr.set_source_rgba(0.7, 0.7, 0.7, 1.0)
    cr.rectangle(cx - bar_w / 2, bar_top, bar_w, bar_bottom - bar_top)
    cr.fill()

    # --- 底座 ---
    base_y = bar_bottom
    cr.set_source_rgba(0.7, 0.7, 0.7, 1.0)
    cr.move_to(cx - 12, base_y)
    cr.line_to(cx + 12, base_y)
    cr.set_line_width(3)
    cr.set_line_cap(1)  # ROUND
    cr.stroke()


class RecordingOverlay:
    """Linux 录音浮窗：emoji 麦克风 + 动态波纹。"""

    def __init__(self):
        self._window = None
        self._level = 0.0
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
        r = 20

        # 1. 清除
        cr.set_operator(0)  # CLEAR
        cr.paint()
        cr.set_operator(2)  # OVER

        # 2. 圆角背景
        cr.new_sub_path()
        cr.arc(w - r, r, r, -pi / 2, 0)
        cr.arc(w - r, h - r, r, 0, pi / 2)
        cr.arc(r, h - r, r, pi / 2, pi)
        cr.arc(r, r, r, pi, 3 * pi / 2)
        cr.close_path()
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.fill()

        # 3. 矢量麦克风图标 (Cairo 路径绘制)
        _draw_mic(cr, cx, cy)

        # 4. 波纹
        level = self._level
        if level > 0.02:
            for i in range(3):
                ring_r = 38 + i * 10
                alpha = max(0, level * (1.0 - i * 0.3))
                if alpha < 0.05:
                    continue
                cr.set_source_rgba(1, 1, 1, alpha * 0.6)
                cr.set_line_width(2.5)
                # 左右两侧弧形波纹
                for start, sweep in [(pi * 0.6, pi * 0.8),
                                     (-pi * 0.4, pi * 0.8)]:
                    cr.new_sub_path()
                    cr.arc(cx, cy, ring_r, start, start + sweep)
                    cr.stroke()

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
        self._center_window()
        self._window.show()
        if self._drawing_area:
            self._drawing_area.queue_draw()
        return False

    def update(self, text: str) -> None:
        GLib.idle_add(self._do_fade_out)

    def _do_fade_out(self):
        self._level = 0.0
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
        GLib.idle_add(self._do_redraw)

    def _do_redraw(self):
        if self._drawing_area:
            self._drawing_area.queue_draw()
        return False
