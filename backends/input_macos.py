"""文字输入模块 (macOS) - 通过 pbcopy/pbpaste + pynput 输入到当前焦点窗口。

需要在「系统设置 > 隐私与安全性 > 辅助功能」中授权终端或应用。
"""

import subprocess
import time

from pynput.keyboard import Controller, Key

_keyboard = Controller()


def type_text(text: str, method: str = "clipboard") -> None:
    """将文字输入到当前焦点窗口。

    Args:
        text: 要输入的文字
        method: macOS 上忽略此参数，始终使用剪贴板方式
    """
    if not text:
        return
    _type_via_clipboard(text)


def _type_via_clipboard(text: str) -> None:
    """通过剪贴板粘贴文字（支持中文）。

    1. 保存当前剪贴板内容
    2. 将识别文字写入剪贴板
    3. 模拟 Cmd+V 粘贴
    4. 恢复原剪贴板内容
    """
    # 保存原剪贴板
    try:
        original = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            timeout=2,
        ).stdout
    except Exception:
        original = None

    # 写入新内容
    subprocess.run(
        ["pbcopy"],
        input=text.encode("utf-8"),
        timeout=2,
    )

    # 短暂等待剪贴板同步
    time.sleep(0.05)

    # 模拟 Cmd+V 粘贴
    _keyboard.press(Key.cmd)
    _keyboard.press("v")
    _keyboard.release("v")
    _keyboard.release(Key.cmd)

    # 恢复原剪贴板
    if original is not None:
        time.sleep(0.1)
        subprocess.run(
            ["pbcopy"],
            input=original,
            timeout=2,
        )
