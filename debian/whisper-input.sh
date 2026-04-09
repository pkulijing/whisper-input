#!/bin/bash
# Whisper Input launcher script
# Installed to /usr/bin/whisper-input by DEB package

set -e

INSTALL_DIR="/opt/whisper-input"
VENV_DIR="${HOME}/.local/share/whisper-input/.venv"

# PyGObject (pip) 使用 girepository-2.0，需要指定系统 typelib 路径
export GI_TYPELIB_PATH="/usr/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"

# 检查用户是否在 input 组
if ! groups 2>/dev/null | grep -qw input; then
    echo "警告: 当前用户不在 input 组中，无法监听键盘事件。"
    echo "请执行: sudo usermod -aG input $USER"
    echo "然后注销并重新登录。"

    # 尝试用桌面通知提醒
    if command -v notify-send &>/dev/null; then
        notify-send -u critical "Whisper Input" \
            "当前用户不在 input 组中，请执行:\nsudo usermod -aG input $USER\n然后注销重新登录"
    fi
    exit 1
fi

# 检查 uv 是否可用（包括用户目录安装的 uv）
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv &>/dev/null; then
    echo "错误: 未找到 uv 包管理器。"
    echo "请安装: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

cd "$INSTALL_DIR"

# 设置 per-user venv 路径
export UV_PROJECT_ENVIRONMENT="$VENV_DIR"

# Fallback：如果 postinst 未能完成依赖安装，首次启动时补救
if [ ! -d "$VENV_DIR" ]; then
    if command -v notify-send &>/dev/null; then
        notify-send "Whisper Input" "正在安装依赖，请稍候..."
    fi

    uv sync

    if command -v notify-send &>/dev/null; then
        notify-send "Whisper Input" "依赖安装完成！"
    fi
fi

# 启动应用
exec uv run python main.py "$@"
