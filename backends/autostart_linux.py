"""Linux 自启动管理 - 使用 XDG autostart (.desktop 文件)。"""

import os
import shutil

# 自启动文件路径
AUTOSTART_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "autostart",
)
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "whisper-input.desktop")

# .desktop 文件来源（安装模式 → 开发模式）
DESKTOP_SOURCES = [
    "/usr/share/applications/whisper-input.desktop",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets",
        "whisper-input.desktop",
    ),
]


def is_autostart_enabled() -> bool:
    """检查是否已启用开机自启动。"""
    return os.path.exists(AUTOSTART_FILE)


def set_autostart(enabled: bool) -> None:
    """设置开机自启动。"""
    if enabled:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        for source in DESKTOP_SOURCES:
            if os.path.exists(source):
                shutil.copy2(source, AUTOSTART_FILE)
                return
        # 没有现成的 .desktop 文件，生成一个最小版本
        with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
            f.write(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Whisper Input\n"
                "Name[zh_CN]=语音输入\n"
                "Exec=/usr/bin/whisper-input\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
    elif os.path.exists(AUTOSTART_FILE):
        os.remove(AUTOSTART_FILE)
