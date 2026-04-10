"""macOS 自启动管理 - 使用 LaunchAgents plist。"""

import os
import sys

AUTOSTART_DIR = os.path.expanduser("~/Library/LaunchAgents")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "com.whisper-input.plist")


def is_autostart_enabled() -> bool:
    """检查是否已启用开机自启动。"""
    return os.path.exists(AUTOSTART_FILE)


def set_autostart(enabled: bool) -> None:
    """设置开机自启动。"""
    if enabled:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)

        # 使用当前 Python 解释器和 main.py 路径
        python_path = sys.executable
        main_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "main.py",
        )

        plist_content = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.whisper-input</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{main_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"""
        with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
            f.write(plist_content)
    elif os.path.exists(AUTOSTART_FILE):
        os.remove(AUTOSTART_FILE)
