"""macOS 自启动管理 - 使用 LaunchAgents plist。"""

import contextlib
import os
import subprocess
import sys

from daobidao.logger import LAUNCHD_LOG_FILENAME, get_log_dir

AUTOSTART_DIR = os.path.expanduser("~/Library/LaunchAgents")
AUTOSTART_LABEL = "com.daobidao"
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, f"{AUTOSTART_LABEL}.plist")


def _program_arguments() -> list[str]:
    """返回 plist 中 ProgramArguments 使用的命令行。

    优先级：
    1. .app bundle 已安装 → 直接调用 .app 内的可执行文件
       （用 open -a 的话，macOS 登录项会显示 "open" 而非 app 名称）
    2. venv console script → 直接调用
    3. python -m daobidao → 兜底
    """
    from daobidao.backends.app_bundle_macos import (
        get_app_bundle_path,
        is_app_bundle_installed,
    )

    if is_app_bundle_installed():
        exe = os.path.join(
            get_app_bundle_path(),
            "Contents",
            "MacOS",
            "daobidao",
        )
        return [exe]

    venv_script = os.path.join(sys.prefix, "bin", "daobidao")
    if os.path.isfile(venv_script):
        return [venv_script]
    return [sys.executable, "-m", "daobidao"]


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_plist() -> str:
    args_xml = "\n".join(
        f"        <string>{_xml_escape(a)}</string>"
        for a in _program_arguments()
    )
    # launchd 自己写的 stderr/stdout 用独立文件,不经 Python 的
    # RotatingFileHandler 轮转 —— 避免 launchd 持有旧 fd 把 stderr 写进已被
    # 重命名的孤儿文件,直到进程重启才切回来。app 内的结构化日志走 logger
    # 自己的 daobidao.log(可轮转)。
    launchd_log = _xml_escape(str(get_log_dir() / LAUNCHD_LOG_FILENAME))
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
    <key>StandardOutPath</key>
    <string>{launchd_log}</string>
    <key>StandardErrorPath</key>
    <string>{launchd_log}</string>
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
        # launchd 不会自己建日志目录,plist 里引用的 StandardErrorPath
        # 的父目录必须存在
        get_log_dir().mkdir(parents=True, exist_ok=True)
        with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
            f.write(_build_plist())
    else:
        # bootout 只影响 launchd 管理的实例（比如登录后启动的那个）；
        # 用户手动启动的进程不受影响，所以调用是安全的。
        _launchctl("bootout", f"gui/{os.getuid()}/{AUTOSTART_LABEL}")
        if os.path.exists(AUTOSTART_FILE):
            os.remove(AUTOSTART_FILE)
