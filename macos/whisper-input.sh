#!/bin/bash
# Whisper Input launcher script for macOS .app bundle
# Installed to Whisper Input.app/Contents/MacOS/whisper-input

set -e

# 日志文件（双击启动时可查看）
LOG_FILE="$HOME/Library/Logs/WhisperInput.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== $(date) ==="

# 应用源码目录（.app/Contents/Resources/app/）
APP_DIR="$(dirname "$0")/../Resources/app"
APP_DIR="$(cd "$APP_DIR" && pwd)"

# per-user venv 和配置目录
DATA_DIR="$HOME/Library/Application Support/Whisper Input"
VENV_DIR="$DATA_DIR/.venv"

# 确保 PATH 包含常见的 uv 安装位置
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Finder 启动时无 locale 环境变量，设置 UTF-8 避免编码问题
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

# 检查 uv
if ! command -v uv &>/dev/null; then
    osascript -e 'display dialog "未找到 uv 包管理器。\n\n请先安装：\ncurl -LsSf https://astral.sh/uv/install.sh | sh\n\n然后重新启动应用。" buttons {"确定"} default button 1 with title "Whisper Input" with icon caution'
    exit 1
fi

# 检查 portaudio（sounddevice 依赖）—— 直接检查 dylib 文件
if [ ! -f /opt/homebrew/lib/libportaudio.dylib ] && [ ! -f /usr/local/lib/libportaudio.dylib ]; then
    osascript -e 'display dialog "未找到 portaudio（音频录制依赖）。\n\n请在终端中执行：\nbrew install portaudio\n\n然后重新启动应用。" buttons {"确定"} default button 1 with title "Whisper Input" with icon caution'
    exit 1
fi

cd "$APP_DIR"

# 设置 per-user venv 路径
export UV_PROJECT_ENVIRONMENT="$VENV_DIR"

# 首次启动：安装依赖
if [ ! -d "$VENV_DIR" ]; then
    # 通过通知告知用户正在安装
    osascript -e 'display notification "正在安装依赖，首次启动可能需要几分钟..." with title "Whisper Input"' 2>/dev/null || true

    uv sync --group macos

    osascript -e 'display notification "依赖安装完成！" with title "Whisper Input"' 2>/dev/null || true
fi

# 在用户数据目录创建最小 .app bundle 包裹 whisper-input 二进制
# macOS 按可执行文件所在的 .app bundle 追踪权限和显示图标
HELPER_APP="$DATA_DIR/Whisper Input.app"
WHISPER_BIN="$HELPER_APP/Contents/MacOS/whisper-input"
HELPER_LIB="$HELPER_APP/Contents/lib"
VENV_PYTHON="$VENV_DIR/bin/python"

# 获取主 .app 的图标路径（用于复制到 helper app）
MAIN_APP_ICON="$(dirname "$0")/../Resources/AppIcon.icns"

# 解析真实 python 路径和安装前缀
REAL_PYTHON=$("$VENV_PYTHON" -c "import os,sys; print(os.path.realpath(sys.executable))")
PYTHON_HOME=$("$VENV_PYTHON" -c "import sys; print(sys.base_prefix)")
REAL_LIB="$(cd "$(dirname "$REAL_PYTHON")/../lib" && pwd)"

if [ ! -f "$WHISPER_BIN" ] || [ "$VENV_PYTHON" -nt "$WHISPER_BIN" ]; then
    # 创建 helper .app 目录结构
    mkdir -p "$HELPER_APP/Contents/MacOS"
    mkdir -p "$HELPER_APP/Contents/Resources"
    mkdir -p "$HELPER_LIB"

    # 复制 python 二进制并重命名
    cp -L "$REAL_PYTHON" "$WHISPER_BIN"

    # 为 @executable_path/../lib/ 创建 dylib 软链接
    ln -sf "$REAL_LIB"/libpython*.dylib "$HELPER_LIB/"

    # 复制图标
    if [ -f "$MAIN_APP_ICON" ]; then
        cp "$MAIN_APP_ICON" "$HELPER_APP/Contents/Resources/AppIcon.icns"
    fi

    # 生成 Info.plist
    cat > "$HELPER_APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Whisper Input</string>
    <key>CFBundleIdentifier</key>
    <string>com.whisper-input.helper</string>
    <key>CFBundleExecutable</key>
    <string>whisper-input</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Whisper Input 需要麦克风权限来录制语音并进行语音识别</string>
</dict>
</plist>
PLIST

    # 向 Launch Services 注册，让 macOS 识别图标和应用名称
    LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"
    if [ -x "$LSREGISTER" ]; then
        "$LSREGISTER" -f "$HELPER_APP"
    fi
fi

# PYTHONHOME: 让复制的二进制找到 Python 标准库
# PYTHONPATH: 二进制不在 venv 目录内，需显式指定 site-packages
SITE_PACKAGES=$("$VENV_PYTHON" -c "import site; print(site.getsitepackages()[0])")
export PYTHONHOME="$PYTHON_HOME"
export PYTHONPATH="$SITE_PACKAGES"

exec "$WHISPER_BIN" main.py "$@"
