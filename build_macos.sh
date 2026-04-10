#!/bin/bash
# 构建 Whisper Input macOS .app 和 DMG 安装包
set -e

VERSION="0.2.0"
APP_NAME="Whisper Input"
BUILD_DIR="build/macos"
APP_BUNDLE="$BUILD_DIR/$APP_NAME.app"
DMG_NAME="WhisperInput_${VERSION}.dmg"

echo "正在构建 $APP_NAME v$VERSION (macOS) ..."

# 检查必要工具
for cmd in sips iconutil hdiutil; do
    if ! command -v $cmd &>/dev/null; then
        echo "错误: 未找到 $cmd，请在 macOS 上运行此脚本"
        exit 1
    fi
done

# 清理旧构建
rm -rf "$BUILD_DIR"

# ========================================
# 1. 生成 .icns 图标
# ========================================
echo "[1/4] 生成应用图标 ..."

ICONSET_DIR="$BUILD_DIR/AppIcon.iconset"
mkdir -p "$ICONSET_DIR"

SOURCE_PNG="assets/whisper-input.png"
if [ ! -f "$SOURCE_PNG" ]; then
    echo "错误: 未找到图标源文件 $SOURCE_PNG"
    echo "请先运行: uv run python assets/generate_icon.py"
    exit 1
fi

# 生成各尺寸图标（macOS 要求的标准尺寸）
for size in 16 32 128 256 512; do
    sips -z $size $size "$SOURCE_PNG" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    if [ $double -le 1024 ]; then
        sips -z $double $double "$SOURCE_PNG" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
    fi
done

iconutil --convert icns "$ICONSET_DIR" --output "$BUILD_DIR/AppIcon.icns"
rm -rf "$ICONSET_DIR"
echo "  图标已生成: $BUILD_DIR/AppIcon.icns"

# ========================================
# 2. 创建 .app bundle 结构
# ========================================
echo "[2/4] 创建 .app bundle ..."

mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources/app/backends"

# 复制 Info.plist（替换版本号）
sed "s/VERSION_PLACEHOLDER/$VERSION/g" macos/Info.plist > "$APP_BUNDLE/Contents/Info.plist"

# 复制 launcher 脚本
cp macos/whisper-input.sh "$APP_BUNDLE/Contents/MacOS/whisper-input"
chmod 755 "$APP_BUNDLE/Contents/MacOS/whisper-input"

# 复制图标
cp "$BUILD_DIR/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/"

# 复制应用源码
DEST="$APP_BUNDLE/Contents/Resources/app"

cp main.py hotkey.py input_method.py recorder.py stt_sensevoice.py \
   config_manager.py settings_server.py \
   pyproject.toml uv.lock .python-version \
   "$DEST/"

# 复制 config.yaml（如果存在）
[ -f config.yaml ] && cp config.yaml "$DEST/"

# 复制 backends/
cp backends/__init__.py \
   backends/hotkey_linux.py backends/hotkey_macos.py \
   backends/input_linux.py backends/input_macos.py \
   backends/autostart_linux.py backends/autostart_macos.py \
   "$DEST/backends/"

# 复制图标资源
mkdir -p "$DEST/assets"
cp assets/whisper-input.png "$DEST/assets/"

echo "  .app bundle: $APP_BUNDLE"

# ========================================
# 3. 创建 DMG
# ========================================
echo "[3/4] 创建 DMG 安装包 ..."

DMG_TEMP="$BUILD_DIR/dmg_temp"
mkdir -p "$DMG_TEMP"

# 复制 .app 到临时目录
cp -R "$APP_BUNDLE" "$DMG_TEMP/"

# 创建 Applications 快捷方式
ln -s /Applications "$DMG_TEMP/Applications"

# 创建 DMG
hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$DMG_TEMP" \
    -ov \
    -format UDZO \
    "$BUILD_DIR/$DMG_NAME" \
    >/dev/null

rm -rf "$DMG_TEMP"
echo "  DMG: $BUILD_DIR/$DMG_NAME"

# ========================================
# 4. 完成
# ========================================
echo ""
echo "[4/4] 构建完成!"
echo ""
echo "========================================="
echo "  $APP_NAME v$VERSION (macOS)"
echo "========================================="
echo ""
echo "  .app: $APP_BUNDLE"
echo "  .dmg: $BUILD_DIR/$DMG_NAME"
echo ""
echo "安装方法:"
echo "  1. 双击 $DMG_NAME"
echo "  2. 将 $APP_NAME 拖入 Applications"
echo "  3. 首次运行需授予辅助功能和麦克风权限"
echo ""
echo "前置依赖（用户需自行安装）:"
echo "  brew install portaudio"
echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
echo "========================================="
