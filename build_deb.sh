#!/bin/bash
# 构建 Whisper Input DEB 安装包
set -e

VERSION="0.1.0"
PKG_NAME="whisper-input"
BUILD_DIR="build/deb/${PKG_NAME}_${VERSION}"

echo "正在构建 ${PKG_NAME} v${VERSION} ..."

# 清理旧构建
rm -rf "$BUILD_DIR"

# 创建目录结构
mkdir -p "$BUILD_DIR/DEBIAN"
mkdir -p "$BUILD_DIR/opt/whisper-input/assets"
mkdir -p "$BUILD_DIR/usr/bin"
mkdir -p "$BUILD_DIR/usr/share/applications"
mkdir -p "$BUILD_DIR/usr/share/icons/hicolor/256x256/apps"

# 复制应用源码
cp main.py hotkey.py input_method.py recorder.py stt_sensevoice.py \
   config_manager.py settings_server.py \
   config.yaml pyproject.toml uv.lock .python-version \
   "$BUILD_DIR/opt/whisper-input/"

# 复制平台后端
mkdir -p "$BUILD_DIR/opt/whisper-input/backends"
cp backends/__init__.py \
   backends/hotkey_linux.py backends/hotkey_macos.py \
   backends/input_linux.py backends/input_macos.py \
   backends/autostart_linux.py backends/autostart_macos.py \
   "$BUILD_DIR/opt/whisper-input/backends/"

# 复制图标资源
cp assets/whisper-input.png "$BUILD_DIR/opt/whisper-input/assets/"

# 复制 .desktop 文件和图标到系统目录
cp assets/whisper-input.desktop "$BUILD_DIR/usr/share/applications/"
cp assets/whisper-input.png "$BUILD_DIR/usr/share/icons/hicolor/256x256/apps/"

# 复制 DEBIAN 控制文件
cp debian/control "$BUILD_DIR/DEBIAN/"
cp debian/postinst "$BUILD_DIR/DEBIAN/"
cp debian/prerm "$BUILD_DIR/DEBIAN/"
cp debian/postrm "$BUILD_DIR/DEBIAN/"
chmod 755 "$BUILD_DIR/DEBIAN/postinst"
chmod 755 "$BUILD_DIR/DEBIAN/prerm"
chmod 755 "$BUILD_DIR/DEBIAN/postrm"

# 复制 launcher 脚本
cp debian/whisper-input.sh "$BUILD_DIR/usr/bin/whisper-input"
chmod 755 "$BUILD_DIR/usr/bin/whisper-input"

# 设置文件权限
find "$BUILD_DIR/opt/whisper-input" -type f -name "*.py" -exec chmod 644 {} \;
find "$BUILD_DIR/opt/whisper-input" -type f ! -name "*.py" -exec chmod 644 {} \;
chmod 644 "$BUILD_DIR/usr/share/applications/whisper-input.desktop"
chmod 644 "$BUILD_DIR/usr/share/icons/hicolor/256x256/apps/whisper-input.png"

# 构建 DEB 包
dpkg-deb --build "$BUILD_DIR"

echo ""
echo "构建完成: build/deb/${PKG_NAME}_${VERSION}.deb"
echo ""
echo "安装方法:"
echo "  sudo dpkg -i build/deb/${PKG_NAME}_${VERSION}.deb"
echo "  sudo apt-get -f install  # 安装缺失的依赖"
