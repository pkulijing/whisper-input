#!/bin/bash
# macOS 开发模式启动脚本
# 创建最小 .app bundle 包裹 whisper-input 二进制，
# 使辅助功能/输入监控权限列表中显示名称和图标
set -e

VENV_PYTHON=".venv/bin/python"
HELPER_APP=".venv/Whisper Input.app"
WHISPER_BIN="$HELPER_APP/Contents/MacOS/whisper-input"
HELPER_LIB="$HELPER_APP/Contents/lib"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "错误: 未找到 .venv，请先运行 uv sync"
    exit 1
fi

# 解析真实 python 路径和安装前缀
REAL_PYTHON=$("$VENV_PYTHON" -c "import os,sys; print(os.path.realpath(sys.executable))")
PYTHON_HOME=$("$VENV_PYTHON" -c "import sys; print(sys.base_prefix)")
REAL_LIB="$(cd "$(dirname "$REAL_PYTHON")/../lib" && pwd)"

# python 更新时同步
if [ ! -f "$WHISPER_BIN" ] || [ "$VENV_PYTHON" -nt "$WHISPER_BIN" ]; then
    mkdir -p "$HELPER_APP/Contents/MacOS"
    mkdir -p "$HELPER_APP/Contents/Resources"
    mkdir -p "$HELPER_LIB"

    cp -L "$REAL_PYTHON" "$WHISPER_BIN"
    ln -sf "$REAL_LIB"/libpython*.dylib "$HELPER_LIB/"

    # 从 PNG 生成 .icns 图标
    if [ -f "assets/whisper-input.png" ]; then
        ICONSET_DIR="$HELPER_APP/Contents/Resources/AppIcon.iconset"
        mkdir -p "$ICONSET_DIR"
        for size in 16 32 128 256 512; do
            sips -z $size $size "assets/whisper-input.png" \
                --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
            double=$((size * 2))
            if [ $double -le 1024 ]; then
                sips -z $double $double "assets/whisper-input.png" \
                    --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
            fi
        done
        iconutil --convert icns "$ICONSET_DIR" \
            --output "$HELPER_APP/Contents/Resources/AppIcon.icns"
        rm -rf "$ICONSET_DIR"
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

    # 向 Launch Services 注册图标
    LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"
    if [ -x "$LSREGISTER" ]; then
        "$LSREGISTER" -f "$HELPER_APP"
    fi

    echo "[run] 已创建 $HELPER_APP"
fi

export PYTHONHOME="$PYTHON_HOME"
export PYTHONPATH=$("$VENV_PYTHON" -c "import site; print(site.getsitepackages()[0])")

exec "$WHISPER_BIN" main.py "$@"
