"""macOS 自启动管理 - 使用 LaunchAgents plist。"""

import contextlib
import os
import subprocess
import sys

AUTOSTART_DIR = os.path.expanduser("~/Library/LaunchAgents")
AUTOSTART_LABEL = "com.whisper-input"
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, f"{AUTOSTART_LABEL}.plist")


def _program_arguments() -> list[str]:
    """返回 plist 中 ProgramArguments 使用的命令行。

    优先用 venv / uv tool / pipx 里的 whisper-input console script
    (sys.prefix/bin/whisper-input),找不到再退回到 `python -m whisper_input`。
    """
    venv_script = os.path.join(sys.prefix, "bin", "whisper-input")
    if os.path.isfile(venv_script):
        return [venv_script]
    return [sys.executable, "-m", "whisper_input"]


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _build_plist() -> str:
    args_xml = "\n".join(
        f"        <string>{_xml_escape(a)}</string>"
        for a in _program_arguments()
    )
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{AUTOSTART_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
"""


def _launchctl(*args: str) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["launchctl", *args],
            check=False,
            capture_output=True,
            timeout=10,
        )


def is_autostart_enabled() -> bool:
    """检查是否已启用开机自启动。"""
    return os.path.exists(AUTOSTART_FILE)


def set_autostart(enabled: bool) -> None:
    """设置开机自启动。

    语义是"下次登录时启动"，所以启用时只写 plist，不主动 bootstrap ——
    ~/Library/LaunchAgents 下的 plist 会在下次登录被 launchd 自动加载。
    主动 bootstrap 会因为 RunAtLoad=true 立刻拉起一个新实例，和当前
    正在运行的主程序冲突（端口 / TCC / 模型加载），所以必须避免。
    """
    if enabled:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
            f.write(_build_plist())
    else:
        # bootout 只影响 launchd 管理的实例（比如登录后启动的那个）；
        # 用户手动启动的进程不受影响，所以调用是安全的。
        _launchctl("bootout", f"gui/{os.getuid()}/{AUTOSTART_LABEL}")
        if os.path.exists(AUTOSTART_FILE):
            os.remove(AUTOSTART_FILE)
