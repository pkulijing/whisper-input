#!/bin/bash
# 构建 Whisper Input 安装包 - 自动检测平台
set -e

PKG_NAME="whisper-input"
VERSION=$(grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')

# 公共源文件列表
SOURCE_PY=(
    main.py hotkey.py input_method.py recorder.py
    stt_sensevoice.py config_manager.py settings_server.py
    version.py overlay.py
)
SOURCE_BACKENDS=(
    backends/__init__.py
    backends/hotkey_linux.py backends/hotkey_macos.py
    backends/input_linux.py backends/input_macos.py
    backends/autostart_linux.py backends/autostart_macos.py
    backends/overlay_linux.py backends/overlay_macos.py
)
SOURCE_OTHER=(
    config.example.yaml pyproject.toml uv.lock .python-version
)

# ========================================
# macOS 构建
# ========================================
build_macos() {
    APP_NAME="Whisper Input"
    BUILD_DIR="build/macos"
    APP_BUNDLE="$BUILD_DIR/$APP_NAME.app"
    DMG_NAME="WhisperInput_${VERSION}.dmg"

    echo "正在构建 $APP_NAME v$VERSION (macOS) ..."

    for cmd in sips iconutil hdiutil; do
        if ! command -v $cmd &>/dev/null; then
            echo "错误: 未找到 $cmd，请在 macOS 上运行此脚本"
            exit 1
        fi
    done

    rm -rf "$BUILD_DIR"

    # 1. 生成 .icns 图标
    echo "[1/4] 生成应用图标 ..."
    ICONSET_DIR="$BUILD_DIR/AppIcon.iconset"
    mkdir -p "$ICONSET_DIR"

    SOURCE_PNG="assets/whisper-input.png"
    if [ ! -f "$SOURCE_PNG" ]; then
        echo "错误: 未找到图标源文件 $SOURCE_PNG"
        echo "请先运行: uv run python assets/generate_icon.py"
        exit 1
    fi

    for size in 16 32 128 256 512; do
        sips -z $size $size "$SOURCE_PNG" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
        double=$((size * 2))
        if [ $double -le 1024 ]; then
            sips -z $double $double "$SOURCE_PNG" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
        fi
    done

    iconutil --convert icns "$ICONSET_DIR" --output "$BUILD_DIR/AppIcon.icns"
    rm -rf "$ICONSET_DIR"

    # 2. 创建 .app bundle
    echo "[2/4] 创建 .app bundle ..."
    DEST="$APP_BUNDLE/Contents/Resources/app"
    mkdir -p "$APP_BUNDLE/Contents/MacOS"
    mkdir -p "$DEST/backends"
    mkdir -p "$DEST/assets"

    sed "s/VERSION_PLACEHOLDER/$VERSION/g" macos/Info.plist > "$APP_BUNDLE/Contents/Info.plist"
    cp macos/whisper-input.sh "$APP_BUNDLE/Contents/MacOS/whisper-input"
    chmod 755 "$APP_BUNDLE/Contents/MacOS/whisper-input"
    cp "$BUILD_DIR/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/"

    cp "${SOURCE_PY[@]}" "${SOURCE_OTHER[@]}" "$DEST/"
    cp "${SOURCE_BACKENDS[@]}" "$DEST/backends/"
    cp assets/whisper-input.png "$DEST/assets/"

    # 3. 创建 DMG
    echo "[3/4] 创建 DMG 安装包 ..."
    DMG_TEMP="$BUILD_DIR/dmg_temp"
    mkdir -p "$DMG_TEMP"
    cp -R "$APP_BUNDLE" "$DMG_TEMP/"
    ln -s /Applications "$DMG_TEMP/Applications"

    hdiutil create \
        -volname "$APP_NAME" \
        -srcfolder "$DMG_TEMP" \
        -ov -format UDZO \
        "$BUILD_DIR/$DMG_NAME" >/dev/null

    rm -rf "$DMG_TEMP"

    echo ""
    echo "[4/4] 构建完成!"
    echo ""
    echo "========================================="
    echo "  $APP_NAME v$VERSION (macOS)"
    echo "========================================="
    echo "  .app: $APP_BUNDLE"
    echo "  .dmg: $BUILD_DIR/$DMG_NAME"
    echo "========================================="
}

# ========================================
# Linux DEB 构建
# ========================================
build_linux() {
    BUILD_DIR="build/deb/${PKG_NAME}_${VERSION}"

    echo "正在构建 ${PKG_NAME} v${VERSION} (Linux DEB) ..."

    rm -rf "$BUILD_DIR"

    mkdir -p "$BUILD_DIR/DEBIAN"
    mkdir -p "$BUILD_DIR/opt/whisper-input/assets"
    mkdir -p "$BUILD_DIR/opt/whisper-input/backends"
    mkdir -p "$BUILD_DIR/usr/bin"
    mkdir -p "$BUILD_DIR/usr/share/applications"
    mkdir -p "$BUILD_DIR/usr/share/icons/hicolor/256x256/apps"

    cp "${SOURCE_PY[@]}" "${SOURCE_OTHER[@]}" "$BUILD_DIR/opt/whisper-input/"
    cp "${SOURCE_BACKENDS[@]}" "$BUILD_DIR/opt/whisper-input/backends/"
    cp assets/whisper-input.png "$BUILD_DIR/opt/whisper-input/assets/"
    cp assets/whisper-input.desktop "$BUILD_DIR/usr/share/applications/"
    cp assets/whisper-input.png "$BUILD_DIR/usr/share/icons/hicolor/256x256/apps/"

    cp debian/control "$BUILD_DIR/DEBIAN/"
    cp debian/postinst "$BUILD_DIR/DEBIAN/"
    cp debian/prerm "$BUILD_DIR/DEBIAN/"
    cp debian/postrm "$BUILD_DIR/DEBIAN/"
    chmod 755 "$BUILD_DIR/DEBIAN/postinst"
    chmod 755 "$BUILD_DIR/DEBIAN/prerm"
    chmod 755 "$BUILD_DIR/DEBIAN/postrm"

    cp debian/whisper-input.sh "$BUILD_DIR/usr/bin/whisper-input"
    chmod 755 "$BUILD_DIR/usr/bin/whisper-input"

    find "$BUILD_DIR/opt/whisper-input" -type f -exec chmod 644 {} \;
    chmod 644 "$BUILD_DIR/usr/share/applications/whisper-input.desktop"
    chmod 644 "$BUILD_DIR/usr/share/icons/hicolor/256x256/apps/whisper-input.png"

    dpkg-deb --build "$BUILD_DIR"

    echo ""
    echo "========================================="
    echo "  ${PKG_NAME} v${VERSION} (Linux DEB)"
    echo "========================================="
    echo "  .deb: build/deb/${PKG_NAME}_${VERSION}.deb"
    echo "========================================="
}

# ========================================
# 平台检测
# ========================================
case "$(uname)" in
    Darwin) build_macos ;;
    Linux)  build_linux ;;
    *)
        echo "错误: 不支持的平台 $(uname)"
        exit 1
        ;;
esac
