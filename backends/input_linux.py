"""文字输入模块 (Linux) - 通过 xclip + xdotool 输入到当前焦点窗口。"""

import contextlib
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
    """通过 X11 selection + Shift+Insert 粘贴文字（支持中文）。

    核心思路：**同时写 CLIPBOARD 和 PRIMARY 两套 selection**，然后发
    Shift+Insert。

    为什么这样能覆盖所有场景：
    - Shift+Insert 是 X11 文本控件的"粘贴 PRIMARY"标准快捷键，几乎所有
      GTK/Qt/Chromium 文本控件都支持
    - VS Code 的 Monaco editor 把 Shift+Insert 内部映射到
      editor.action.clipboardPasteAction（粘贴 CLIPBOARD），所以 editor 也
      命中 ✓
    - VS Code 的 integrated terminal（xterm.js）继承 xterm 的行为，
      Shift+Insert 粘贴 PRIMARY ✓
    - VS Code 扩展的 webview 输入框（Chromium <input>）按 Chromium 默认
      行为处理 Shift+Insert → 粘贴 PRIMARY ✓
    - 独立终端（gnome-terminal）Shift+Insert 粘贴 CLIPBOARD ✓
    - 浏览器地址栏、Gedit、LibreOffice 等 Shift+Insert 粘贴 PRIMARY ✓
    - 两套 selection 都写了同一份文本，不管控件读哪个都成功

    相比 Ctrl+V / Ctrl+Shift+V 的好处：
    - 一个快捷键覆盖所有 WM_CLASS，不再需要窗口识别和双发
    - 不会触发 VS Code .md 的 Markdown Preview / JetBrains 的 clipboard 历史
    - 不会被 fcitx/ibus 拦截（Shift+Insert 是纯快捷键，不经 IME 路径）

    副作用：用户之前用鼠标选中的 PRIMARY 内容会被我们覆盖，且无法恢复
    （PRIMARY 是 "selection-only"，没有 history）。这是 X11 设计决定的。
    """
    # 保存原 CLIPBOARD（PRIMARY 不保存——见 docstring 副作用说明）
    try:
        original = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o"],
            capture_output=True,
            timeout=2,
        ).stdout
    except Exception:
        original = None

    payload = text.encode("utf-8")

    # 同时写 CLIPBOARD 和 PRIMARY，确保任何 Shift+Insert 绑定都能命中
    subprocess.run(
        ["xclip", "-selection", "clipboard"],
        input=payload,
        timeout=2,
    )
    subprocess.run(
        ["xclip", "-selection", "primary"],
        input=payload,
        timeout=2,
    )

    # 短暂等待 X server 同步 selection owner
    time.sleep(0.05)

    # Shift+Insert：一个快捷键打通所有目标控件
    subprocess.run(
        ["xdotool", "key", "--clearmodifiers", "shift+Insert"], timeout=2
    )

    # 恢复原 CLIPBOARD
    if original is not None:
        time.sleep(0.1)
        with contextlib.suppress(Exception):
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
