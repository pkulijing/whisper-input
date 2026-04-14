#!/bin/bash
# Whisper Input 安装脚本（Linux 专用，macOS 请用 setup_macos.sh）

set -e

# 守卫：本脚本只适用于 Linux
if [[ "$(uname)" != "Linux" ]]; then
    echo "错误: 此脚本仅适用于 Linux，macOS 请使用 setup_macos.sh"
    exit 1
fi

echo "=== Whisper Input 安装 ==="
echo ""

# 检查系统依赖
echo "1. 检查系统依赖..."
MISSING=""
for cmd in xdotool xclip paplay; do
    if ! command -v $cmd &>/dev/null; then
        MISSING="$MISSING $cmd"
    fi
done
if ! ldconfig -p 2>/dev/null | grep -q libportaudio; then
    MISSING="$MISSING libportaudio2"
fi

if [ -n "$MISSING" ]; then
    echo "   安装缺少的系统依赖:$MISSING"
    sudo apt-get update
    sudo apt-get install -y xdotool xclip pulseaudio-utils libportaudio2
else
    echo "   系统依赖已满足 ✓"
fi

# 检查用户是否在 input 组（evdev 需要）
echo ""
echo "2. 检查 input 组权限..."
if groups | grep -qw input; then
    echo "   用户已在 input 组 ✓"
else
    echo "   将用户加入 input 组..."
    sudo usermod -aG input $USER
    newgrp input
fi

# 检查 uv
echo ""
echo "3. 检查 uv..."
if ! command -v uv &>/dev/null; then
    echo "   uv 未安装，请先安装: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
else
    echo "   uv $(uv --version) ✓"
fi

# 安装 Python 依赖
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo ""
echo "4. 使用 uv 安装 Python 依赖..."
cd "$SCRIPT_DIR"

# 检测 GPU 决定装 CUDA 版还是 CPU 版 torch
# CUDA wheel ~2GB（含 cuDNN/cuBLAS），CPU wheel ~200MB，没显卡时装 CPU 版省一个量级
# 可用 TORCH_VARIANT=cuda|cpu 手动覆盖自动检测
VARIANT="${TORCH_VARIANT:-}"
if [ -z "$VARIANT" ]; then
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
        VARIANT=cuda
        echo "   检测到 NVIDIA GPU → 安装 CUDA 版 torch (cu121)"
    else
        VARIANT=cpu
        echo "   未检测到 NVIDIA GPU → 安装 CPU 版 torch"
    fi
else
    echo "   使用环境变量指定的 torch 变体: $VARIANT"
fi

# 普通依赖走 pyproject.toml 里配置的清华镜像，torch wheel 从阿里云直链拉取
uv sync --extra "$VARIANT"

echo ""
echo "=== 安装完成 ==="
echo ""
echo "使用方法:"
echo "  cd $SCRIPT_DIR"
echo ""
echo "  # 运行（需要 sudo 或 input 组权限读取键盘设备）"
echo "  sudo $(which uv) run python main.py"
echo ""
echo "  # 或使用 input 组权限（重新登录后）"
echo "  uv run python main.py"
echo ""
echo "  # 指定热键"
echo "  uv run python main.py -k KEY_RIGHTALT"
