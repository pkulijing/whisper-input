"""Linux 自启动管理 - 使用 XDG autostart (.desktop 文件)。"""

import os
from importlib.resources import files

# 自启动文件路径
AUTOSTART_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "autostart",
)
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "whisper-input.desktop")


def _load_desktop_template() -> str:
    """从 package data 读 .desktop 模板。"""
    template = files("whisper_input.assets") / "whisper-input.desktop"
    return template.read_text(encoding="utf-8")


def is_autostart_enabled() -> bool:
    """检查是否已启用开机自启动。"""
    return os.path.exists(AUTOSTART_FILE)


def set_autostart(enabled: bool) -> None:
    """设置开机自启动。"""
    if enabled:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        content = _load_desktop_template()
        with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
            f.write(content)
    elif os.path.exists(AUTOSTART_FILE):
        os.remove(AUTOSTART_FILE)
