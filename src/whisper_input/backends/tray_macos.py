"""系统托盘模块 (macOS) - pystray + AppKit Retina 模板图。

macOS 菜单栏规范：用模板图（纯黑+透明）由系统自动反色，
仅 recording 状态叠加红点作为活跃指示（非模板图）。
源图画得足够大，配合 Retina setSize_ 才清晰。
"""

import io

import AppKit  # type: ignore[import-untyped]
import Foundation  # type: ignore[import-untyped]
import pystray
from PIL import Image, ImageDraw

from whisper_input.version import __version__

_ICON_SZ = 128

_STATUS_TIPS = {
    "loading": "Whisper Input - 加载中...",
    "ready": "Whisper Input - 就绪",
    "recording": "Whisper Input - 录音中",
    "processing": "Whisper Input - 识别中...",
}

_COLOR_BLACK = (0, 0, 0, 255)
_COLOR_RED = (244, 67, 54, 255)


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


def _is_template(status: str) -> bool:
    """recording 状态不能作为 template image（需要保留红色）。"""
    return status != "recording"


def _create_icon(status: str = "loading") -> Image.Image:
    img = Image.new("RGBA", (_ICON_SZ, _ICON_SZ), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = _COLOR_BLACK
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
        draw.ellipse([84, 4, 124, 44], fill=_COLOR_RED)
    return img


def run_tray(wi, settings_server, on_quit):
    """启动系统托盘图标（macOS，返回 icon 供主线程 .run()）。"""

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
        pystray.MenuItem("设置...", open_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )

    icon = pystray.Icon(
        "whisper-input",
        _create_icon("loading"),
        _STATUS_TIPS["loading"],
        menu,
    )
    icon._wi_template = True

    def _patched_assert_image():
        thickness = int(icon._status_bar.thickness())
        scale = 2  # Retina
        px = thickness * scale
        source = icon._icon.resize(
            (px, px), Image.Resampling.LANCZOS
        )
        buf = io.BytesIO()
        source.save(buf, "png")
        data = Foundation.NSData.dataWithBytes_length_(
            buf.getvalue(), len(buf.getvalue())
        )
        ns_image = AppKit.NSImage.alloc().initWithData_(data)
        ns_image.setSize_((thickness, thickness))
        ns_image.setTemplate_(
            bool(getattr(icon, "_wi_template", True))
        )
        icon._icon_image = ns_image
        icon._status_item.button().setImage_(ns_image)

    icon._assert_image = _patched_assert_image

    def on_status_change(status: str) -> None:
        icon._wi_template = _is_template(status)
        icon.icon = _create_icon(status)
        icon.title = _STATUS_TIPS.get(
            status, _STATUS_TIPS["ready"]
        )

    wi.set_status_callback(on_status_change)

    return icon
