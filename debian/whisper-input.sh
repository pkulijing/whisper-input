#!/bin/bash
# Whisper Input launcher (Linux trampoline)
# 安装后位于 /usr/bin/whisper-input
#
# 职责：
#   1. 准备日志 / PATH / 环境变量
#   2. 检查 input 组
#   3. 检查 uv 是否可用
#   4. stage 0: 确认 uv 管的 python-build-standalone 已就绪（首启拉 ~30MB）
#   5. exec setup_window.py，由它展示三阶段 tkinter 窗口
set -e

# ---------- 日志 ----------
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/whisper-input"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/whisper-input.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== $(date) ==="

# ---------- 环境 ----------
export WHISPER_INPUT_APP_DIR="/opt/whisper-input"
# PyGObject 需要指定系统 typelib 路径（main.py 里也兜底了，这里先设更稳）
export GI_TYPELIB_PATH="/usr/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
# uv 的常见用户安装位置
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

notify() {
    command -v notify-send >/dev/null 2>&1 && \
        notify-send "Whisper Input" "$1" || true
}
notify_crit() {
    command -v notify-send >/dev/null 2>&1 && \
        notify-send -u critical "Whisper Input" "$1" || true
}

# ---------- input 组检查 ----------
if ! groups 2>/dev/null | grep -qw input; then
    MSG="当前用户不在 input 组中，请执行 sudo usermod -aG input \$USER 后注销重登"
    echo "$MSG" >&2
    notify_crit "$MSG"
    exit 1
fi

# ---------- uv 检查 ----------
if ! command -v uv >/dev/null 2>&1; then
    MSG="未找到 uv 包管理器。请先安装: curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "$MSG" >&2
    notify_crit "$MSG"
    exit 1
fi

# ---------- stage 0: 准备 python-build-standalone ----------
# shellcheck disable=SC1091
. "$WHISPER_INPUT_APP_DIR/python_dist.txt"   # 注入 PYTHON_VERSION
export WHISPER_INPUT_PYTHON_VERSION="$PYTHON_VERSION"

if ! PYBIN="$(uv python find "$PYTHON_VERSION" 2>/dev/null)"; then
    notify "首次启动：正在准备 Python $PYTHON_VERSION 运行环境（约 30MB）..."
    echo "uv python install $PYTHON_VERSION ..."
    uv python install "$PYTHON_VERSION"
    PYBIN="$(uv python find "$PYTHON_VERSION")"
fi
echo "PYBIN=$PYBIN"

# ---------- 启动 setup_window ----------
exec "$PYBIN" "$WHISPER_INPUT_APP_DIR/setup_window.py"
