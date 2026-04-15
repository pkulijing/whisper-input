#!/bin/bash
# Whisper Input - macOS 环境配置脚本

set -e

echo "========================================="
echo "  Whisper Input - macOS 环境配置"
echo "========================================="
echo ""

# 检查 macOS
if [[ "$(uname)" != "Darwin" ]]; then
    echo "错误: 此脚本仅适用于 macOS"
    exit 1
fi

# 检查 Homebrew
if ! command -v brew &>/dev/null; then
    echo "错误: 未安装 Homebrew，请先安装："
    echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    exit 1
fi

# 安装 portaudio（sounddevice 依赖）
echo "[1/3] 检查 portaudio ..."
if [ ! -f /opt/homebrew/lib/libportaudio.dylib ] && [ ! -f /usr/local/lib/libportaudio.dylib ]; then
    echo "  安装 portaudio ..."
    brew install portaudio
else
    echo "  portaudio 已安装"
fi

# 检查 uv
echo "[2/3] 检查 uv ..."
if ! command -v uv &>/dev/null; then
    echo "  安装 uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "  请重新打开终端后再次运行此脚本"
    exit 1
else
    echo "  uv 已安装"
fi

# 安装依赖
echo "[3/3] 安装 Python 依赖 ..."
# 迁移到 onnxruntime + SenseVoice ONNX 后依赖很轻(~20MB),走清华源秒级完成。
uv sync

echo ""
echo "========================================="
echo "  安装完成！"
echo "========================================="
echo ""
echo "运行方式："
echo "  uv run python main.py"
echo ""
echo "⚠️  首次运行需要授予以下权限："
echo ""
echo "  1. 辅助功能权限（热键监听和文字输入）"
echo "     系统设置 > 隐私与安全性 > 辅助功能"
echo "     添加你使用的终端应用（如 Terminal.app 或 iTerm2）"
echo ""
echo "  2. 麦克风权限（语音录制）"
echo "     首次录音时系统会自动弹出授权对话框"
echo ""
echo "  3. 模型下载"
echo "     首次运行会自动下载 SenseVoice ONNX 模型（约 231MB，5 个文件）"
echo "     下载源是达摩院官方 ModelScope 仓库，国内 CDN 直连，无需代理。"
echo "     一次成功后永久离线可用。"
echo ""
