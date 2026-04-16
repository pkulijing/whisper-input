"""系统托盘模块 (Linux) - pystray + AppIndicator 后端。

Linux/Xorg 下 pystray 用 latin-1 编码 WM_NAME，状态文字只能用 ASCII。
图标按状态着色（品牌色），而非 macOS 的模板图方式。
"""

import threading

import pystray
from PIL import Image, ImageDraw

from whisper_input.i18n import t
from whisper_input.version import __version__

_ICON_SZ = 128

_STATUS_COLORS: dict[str, tuple[int, int, int, int]] = {
    "loading": (158, 158, 158, 255),
    "ready": (76, 175, 80, 255),
    "processing": (255, 152, 0, 255),
    "recording": (244, 67, 54, 255),
}


def _safe_tooltip(key: str) -> str:
    """获取 latin-1 安全的 tooltip 文本。

    Linux pystray 用 latin-1 编码 WM_NAME，中文会乱码。
    如果当前语言的文本无法 latin-1 编码，fallback 到英文。
    """
    text = t(key)
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        from whisper_input.i18n import get_all_locales

        return get_all_locales().get("en", {}).get(key, text)


def _draw_mic(
    draw: ImageDraw.ImageDraw,
    filled: bool,
    color: tuple[int, int, int, int],
) -> None:
    width = 12
    if filled:
        draw.rounded_rectangle(
            [40, 16, 88, 76], radius=24, fill=color
        )
    else:
        draw.rounded_rectangle(
            [40, 16, 88, 76],
            radius=24,
            outline=color,
            width=width,
        )
    draw.arc([20, 36, 108, 104], 0, 180, fill=color, width=width)
    draw.line([64, 96, 64, 116], fill=color, width=width)
    draw.line([40, 116, 88, 116], fill=color, width=width)


def _create_icon(status: str = "loading") -> Image.Image:
    img = Image.new("RGBA", (_ICON_SZ, _ICON_SZ), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = _STATUS_COLORS.get(status, _STATUS_COLORS["ready"])
    if status == "ready":
        _draw_mic(draw, filled=False, color=color)
    elif status == "processing":
        _draw_mic(draw, filled=True, color=color)
    elif status == "loading":
        _draw_mic(draw, filled=False, color=color)
        dot_color = (*color[:3], 160)
        for cx in (40, 64, 88):
            draw.ellipse([cx - 6, 112, cx + 6, 124], fill=dot_color)
    elif status == "recording":
        _draw_mic(draw, filled=True, color=color)
    return img


def run_tray(wi, settings_server, on_quit) -> None:
    """启动系统托盘图标（Linux，daemon 线程运行）。"""

    def open_settings(_icon, _item):
        if settings_server:
            settings_server.open_in_browser()

    def quit_app(icon, _item):
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem(
            f"Whisper Input v{__version__}",
            None,
            enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda _: t("tray.settings"), open_settings
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _: t("tray.quit"), quit_app),
    )

    icon = pystray.Icon(
        "whisper-input",
        _create_icon("loading"),
        _safe_tooltip("tray.loading"),
        menu,
    )

    def on_status_change(status: str) -> None:
        icon.icon = _create_icon(status)
        icon.title = _safe_tooltip(f"tray.{status}")

    wi.set_status_callback(on_status_change)

    threading.Thread(target=icon.run, daemon=True).start()
