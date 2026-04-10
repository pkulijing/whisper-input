"""文字输入模块 (Linux) - 通过 xclip + xdotool 输入到当前焦点窗口。"""

import subprocess
import time


def type_text(text: str, method: str = "clipboard") -> None:
    """将文字输入到当前焦点窗口。

    Args:
        text: 要输入的文字
        method: "clipboard" (推荐，支持中文) 或 "xdotool" (仅ASCII)
    """
    if not text:
        return

    if method == "clipboard":
        _type_via_clipboard(text)
    else:
        _type_via_xdotool(text)


def _type_via_clipboard(text: str) -> None:
    """通过剪贴板粘贴文字（支持中文）。

    1. 保存当前剪贴板内容
    2. 将识别文字写入剪贴板
    3. 模拟 Ctrl+V 粘贴
    4. 恢复原剪贴板内容
    """
    # 保存原剪贴板
    try:
        original = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o"],
            capture_output=True,
            timeout=2,
        ).stdout
    except Exception:
        original = None

    # 写入新内容
    subprocess.run(
        ["xclip", "-selection", "clipboard"],
        input=text.encode("utf-8"),
        timeout=2,
    )

    # 短暂等待剪贴板同步
    time.sleep(0.05)

    # 模拟 Ctrl+V（--clearmodifiers 确保热键释放不干扰）
    subprocess.run(
        ["xdotool", "key", "--clearmodifiers", "ctrl+v"], timeout=2
    )

    # 恢复原剪贴板
    if original is not None:
        time.sleep(0.1)
        subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=original,
            timeout=2,
        )


def _type_via_xdotool(text: str) -> None:
    """通过 xdotool 直接输入（仅支持 ASCII）。"""
    subprocess.run(
        ["xdotool", "type", "--clearmodifiers", "--", text],
        timeout=5,
    )
