#!/bin/bash
# Whisper Input - 贡献者开发环境配置脚本
#
# 面向 clone 本仓库的贡献者：装好系统依赖 + uv + `uv sync`，之后 `uv run whisper-input` 就能跑。
# 终端用户请用仓库根目录的 install.sh（curl | sh 一键装）。

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

OS="$(uname -s)"

echo "========================================="
echo "  Whisper Input 开发环境配置 ($OS)"
echo "========================================="
echo ""

install_uv() {
    if command -v uv &>/dev/null; then
        echo "  uv $(uv --version) ✓"
        return
    fi
    echo "  安装 uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck disable=SC1091
    [ -r "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv &>/dev/null; then
        echo "  错误: uv 安装后仍不可用，请手动 source ~/.local/bin/env 后重跑"
        exit 1
    fi
}

setup_macos() {
    # 1. portaudio (sounddevice 依赖)
    echo "[1/3] 检查 portaudio ..."
    if ! command -v brew &>/dev/null; then
        echo "  错误: 未安装 Homebrew，请先安装："
        echo '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        exit 1
    fi
    if brew list portaudio &>/dev/null; then
        echo "  portaudio 已安装 ✓"
    else
        echo "  安装 portaudio ..."
        brew install portaudio
    fi

    # 2. uv
    echo ""
    echo "[2/3] 检查 uv ..."
    install_uv

    # 3. Python 依赖
    echo ""
    echo "[3/3] 安装 Python 依赖 ..."
    uv sync
}

setup_linux() {
    # 1. 系统依赖（和 install.sh / README 清单保持一致）
    echo "[1/4] 检查 Linux 系统依赖 ..."
    MISSING=""
    for cmd in xdotool xclip paplay; do
        if ! command -v "$cmd" &>/dev/null; then
            MISSING="$MISSING $cmd"
        fi
    done
    if ! ldconfig -p 2>/dev/null | grep -q libportaudio; then
        MISSING="$MISSING libportaudio2"
    fi
    # GTK / AppIndicator 的 typelib / 头文件没有对应命令行工具，统一走 apt 装
    APT_PKGS="xdotool xclip pulseaudio-utils libportaudio2 \
libgirepository-2.0-dev libcairo2-dev gir1.2-gtk-3.0 \
gir1.2-ayatanaappindicator3-0.1"

    if [ -n "$MISSING" ] || ! dpkg -s gir1.2-ayatanaappindicator3-0.1 &>/dev/null; then
        if ! command -v apt-get &>/dev/null; then
            echo "  错误: 非 Debian/Ubuntu 发行版，请手动安装这些包："
            echo "    $APT_PKGS"
            exit 1
        fi
        echo "  安装缺失的 apt 软件包 ..."
        # shellcheck disable=SC2086
        sudo apt-get update && sudo apt-get install -y $APT_PKGS
    else
        echo "  系统依赖已满足 ✓"
    fi

    # 2. input 组
    echo ""
    echo "[2/4] 检查 input 组权限 ..."
    if id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
        echo "  用户已在 input 组 ✓"
    else
        echo "  将用户加入 input 组 (需要 sudo) ..."
        sudo usermod -aG input "$USER"
        echo "  ⚠️  需要注销并重新登录才能生效"
    fi

    # 3. uv
    echo ""
    echo "[3/4] 检查 uv ..."
    install_uv

    # 4. Python 依赖
    echo ""
    echo "[4/4] 安装 Python 依赖 ..."
    uv sync
}

case "$OS" in
    Darwin) setup_macos ;;
    Linux)  setup_linux ;;
    *)
        echo "错误: 不支持的操作系统 $OS（仅支持 macOS / Linux）"
        exit 1
        ;;
esac

echo ""
echo "========================================="
echo "  配置完成！"
echo "========================================="
echo ""
echo "运行："
echo "  uv run whisper-input"
echo ""

if [ "$OS" = "Darwin" ]; then
    cat <<'EOF'
⚠️  首次运行需要授予以下权限：

  1. 辅助功能权限（热键监听和文字输入）
     系统设置 > 隐私与安全性 > 辅助功能
     首次运行时系统会自动弹出授权对话框

  2. 麦克风权限（语音录制）
     首次录音时系统会自动弹出授权对话框

  3. 模型下载
     首次运行会自动下载 SenseVoice ONNX 模型（约 231 MB）
     下载源是达摩院官方 ModelScope 仓库，国内 CDN 直连，无需代理。
     一次成功后永久离线可用。

EOF
fi
