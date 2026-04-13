#!/bin/bash
# Whisper Input launcher for macOS .app bundle.
# 安装到 Whisper Input.app/Contents/MacOS/whisper-input。
# 只做三件事：建日志、设环境变量、exec 内置 python 跑 setup_window.py。
set -e

LOG_FILE="$HOME/Library/Logs/WhisperInput.log"
mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== $(date) === [trampoline]"

# Resources 目录：bundle 内的 python / uv / app 源码都在这下面
APP_DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
export WHISPER_INPUT_APP_DIR="$APP_DIR"

# Finder 启动时无 locale 环境变量，设置 UTF-8 避免编码问题
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

# 使用嵌套 .app 的 python 二进制（而不是 Resources/python/bin/python3）
# 目的：让 macOS TCC 把权限挂到 "Whisper Input" 而不是 "python3"
PYTHON="$APP_DIR/Whisper Input.app/Contents/MacOS/whisper-input"
SETUP="$APP_DIR/app/setup_window.py"

if [ ! -x "$PYTHON" ]; then
    osascript -e 'display dialog ".app 内置 Python 缺失，请重新安装 Whisper Input。" buttons {"OK"} default button 1 with title "Whisper Input" with icon stop'
    exit 1
fi

exec "$PYTHON" "$SETUP" "$@"
