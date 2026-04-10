#!/usr/bin/env python3
"""生成 Whisper Input 应用图标 (256x256 PNG)。"""

from PIL import Image, ImageDraw


def generate_icon(size: int = 256) -> Image.Image:
    """生成应用图标：绿色圆形背景 + 白色麦克风。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 缩放因子（相对于 64px 原始尺寸）
    s = size / 64

    # 绿色圆形背景
    margin = int(4 * s)
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill="#4CAF50",
    )

    # 麦克风主体（圆角矩形近似为椭圆+矩形）
    mic_left = int(22 * s)
    mic_right = int(42 * s)
    mic_top = int(14 * s)
    mic_bottom = int(36 * s)
    mic_radius = int(10 * s)

    # 麦克风顶部圆角
    draw.rounded_rectangle(
        [mic_left, mic_top, mic_right, mic_bottom + mic_radius],
        radius=mic_radius,
        fill="white",
    )

    # 麦克风弧形支架
    arc_left = int(18 * s)
    arc_right = int(46 * s)
    arc_top = int(26 * s)
    arc_bottom = int(50 * s)
    draw.arc(
        [arc_left, arc_top, arc_right, arc_bottom],
        0,
        180,
        fill="white",
        width=int(3 * s),
    )

    # 麦克风底部支柱
    center_x = size // 2
    stem_top = int(50 * s)
    stem_bottom = int(56 * s)
    draw.line(
        [center_x, stem_top, center_x, stem_bottom],
        fill="white",
        width=int(3 * s),
    )

    # 底座横线
    base_half = int(6 * s)
    draw.line(
        [center_x - base_half, stem_bottom, center_x + base_half, stem_bottom],
        fill="white",
        width=int(2 * s),
    )

    return img


if __name__ == "__main__":
    import os

    output_path = os.path.join(os.path.dirname(__file__), "whisper-input.png")
    icon = generate_icon(1024)
    icon.save(output_path, "PNG")
    print(f"图标已保存: {output_path}")
