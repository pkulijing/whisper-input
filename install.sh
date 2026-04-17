#!/bin/sh
# Whisper Input - 一键安装脚本 / one-shot installer
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/pkulijing/whisper-input/master/install.sh | sh
#
# 做的事：装 uv / Python 3.12 / 系统依赖 / 跑 uv tool install --upgrade /
# 跑 whisper-input --init (下模型 + macOS .app bundle) / 询问是否立即启动。
# 纯 POSIX sh，读 /dev/tty 做交互，无 TTY 时走默认值。

set -eu

# 让本轮 shell 立刻能找到 uv / whisper-input（uv 和 uv tool 都装到这里）
export PATH="$HOME/.local/bin:$PATH"

# ---------- 全局状态 ----------
LANG_CHOICE="zh"               # 开头会让用户选
INPUT_GROUP_JUST_ADDED=0       # Linux: 步骤 5 改过 input 组就置 1
APT_UPDATED=0                  # Linux: 避免重复 apt-get update

# ---------- 颜色（仅在 stdout 是 TTY 时启用） ----------
if [ -t 1 ]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_GREEN="$(printf '\033[32m')"
    C_YELLOW="$(printf '\033[33m')"
    C_RED="$(printf '\033[31m')"
    C_CYAN="$(printf '\033[36m')"
else
    C_RESET=""; C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_CYAN=""
fi

# ---------- 输出 ----------
info() { printf '%s==>%s %s\n' "$C_CYAN" "$C_RESET" "$1"; }
ok()   { printf '%s✓%s %s\n'   "$C_GREEN" "$C_RESET" "$1"; }
warn() { printf '%s!%s %s\n'   "$C_YELLOW" "$C_RESET" "$1" >&2; }
die()  { printf '%s✗%s %s\n'   "$C_RED" "$C_RESET" "$1" >&2; exit 1; }

# ---------- 交互：读 /dev/tty 或走默认值 ----------
# Usage: prompt_yesno "question" default_y_or_n
# 返回 0 表示 yes，返回 1 表示 no
prompt_yesno() {
    _q="$1"; _default="$2"
    if [ ! -r /dev/tty ]; then
        if [ "$_default" = "y" ]; then
            printf '%s %s\n' "$_q" "$(msg_noninteractive_default_yes)"
            return 0
        else
            printf '%s %s\n' "$_q" "$(msg_noninteractive_default_no)"
            return 1
        fi
    fi
    if [ "$_default" = "y" ]; then
        _hint="[Y/n]"
    else
        _hint="[y/N]"
    fi
    printf '%s %s ' "$_q" "$_hint"
    _ans=""
    read -r _ans < /dev/tty || _ans=""
    [ -z "$_ans" ] && _ans="$_default"
    case "$_ans" in
        [Yy]|[Yy][Ee][Ss]) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------- 双语消息 ----------
# 所有面向用户的字符串放这里，中英各一支。保持 POSIX sh 风格，不搞 i18n 框架。
msg_lang_prompt()              { printf 'Language / 语言:\n  [1] 中文 (default)\n  [2] English\nSelect / 选择: '; }
msg_header()                   { [ "$LANG_CHOICE" = en ] && echo "Whisper Input installer" || echo "Whisper Input 一键安装"; }
msg_unsupported_os()           { [ "$LANG_CHOICE" = en ] && echo "Unsupported OS: $1 (only macOS / Linux are supported)" || echo "不支持的操作系统: $1（仅支持 macOS / Linux）"; }
msg_unsupported_arch()         { [ "$LANG_CHOICE" = en ] && echo "Untested architecture: $1 — continuing anyway" || echo "未测试的架构: $1 —— 继续，但可能有风险"; }
msg_step_uv()                  { [ "$LANG_CHOICE" = en ] && echo "[1/6] Checking uv..." || echo "[1/6] 检查 uv..."; }
msg_uv_present()               { [ "$LANG_CHOICE" = en ] && echo "uv already installed ($(uv --version 2>/dev/null || echo 'version unknown'))" || echo "uv 已安装 ($(uv --version 2>/dev/null || echo '版本未知'))"; }
msg_uv_installing()            { [ "$LANG_CHOICE" = en ] && echo "Installing uv via astral.sh..." || echo "从 astral.sh 安装 uv..."; }
msg_uv_install_failed()        { [ "$LANG_CHOICE" = en ] && echo "uv installation failed — check your network and retry" || echo "uv 安装失败 —— 请检查网络后重试"; }
msg_step_python()              { [ "$LANG_CHOICE" = en ] && echo "[2/6] Checking Python 3.12..." || echo "[2/6] 检查 Python 3.12..."; }
msg_python_present()           { [ "$LANG_CHOICE" = en ] && echo "Python 3.12 available via uv" || echo "uv 已可用 Python 3.12"; }
msg_python_installing()        { [ "$LANG_CHOICE" = en ] && echo "Installing Python 3.12 via uv..." || echo "通过 uv 安装 Python 3.12..."; }
msg_step_sysdeps()             { [ "$LANG_CHOICE" = en ] && echo "[3/6] Checking system dependencies..." || echo "[3/6] 检查系统依赖..."; }
msg_brew_missing_prompt()      { [ "$LANG_CHOICE" = en ] && echo "Homebrew not found. Install it automatically now?" || echo "未检测到 Homebrew，是否现在代为安装？"; }
msg_brew_sudo_notice()         { [ "$LANG_CHOICE" = en ] && echo "  (Homebrew's own installer will prompt for your sudo password — that's normal.)" || echo "  （Homebrew 官方安装脚本会弹出 sudo 密码输入，这是正常行为）"; }
msg_brew_installing()          { [ "$LANG_CHOICE" = en ] && echo "Installing Homebrew..." || echo "安装 Homebrew..."; }
msg_brew_install_failed()      { [ "$LANG_CHOICE" = en ] && echo "Homebrew installation failed" || echo "Homebrew 安装失败"; }
msg_brew_declined() {
    if [ "$LANG_CHOICE" = en ]; then
        cat <<'EOF'
Please install Homebrew first, then re-run this installer:
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
EOF
    else
        cat <<'EOF'
请先手动安装 Homebrew，再重跑本脚本：
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
EOF
    fi
}
msg_portaudio_present()        { [ "$LANG_CHOICE" = en ] && echo "portaudio already installed" || echo "portaudio 已安装"; }
msg_portaudio_installing()     { [ "$LANG_CHOICE" = en ] && echo "Installing portaudio via Homebrew..." || echo "通过 Homebrew 安装 portaudio..."; }
msg_linux_apt_installing()     { [ "$LANG_CHOICE" = en ] && echo "Installing apt packages (may prompt for sudo password)..." || echo "安装 apt 软件包（可能需要 sudo 密码）..."; }
msg_linux_not_debian()         {
    if [ "$LANG_CHOICE" = en ]; then
        cat <<'EOF'
Non-Debian/Ubuntu distribution detected. Please install these packages manually
using your distribution's package manager, then re-run this installer:

  xdotool xclip pulseaudio-utils libportaudio2 libgirepository-2.0-dev
  libcairo2-dev gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
EOF
    else
        cat <<'EOF'
检测到非 Debian/Ubuntu 发行版。请用你的发行版包管理器手动安装以下依赖，
然后重跑本脚本：

  xdotool xclip pulseaudio-utils libportaudio2 libgirepository-2.0-dev
  libcairo2-dev gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
EOF
    fi
}
msg_step_inputgroup()          { [ "$LANG_CHOICE" = en ] && echo "[4/6] Checking Linux 'input' group..." || echo "[4/6] 检查 Linux 'input' 组权限..."; }
msg_inputgroup_present()       { [ "$LANG_CHOICE" = en ] && echo "User is already in 'input' group" || echo "用户已在 'input' 组"; }
msg_inputgroup_prompt()        { [ "$LANG_CHOICE" = en ] && echo "whisper-input needs /dev/input/* access. Add you to the 'input' group now? (requires sudo)" || echo "whisper-input 需要读取 /dev/input/* 的权限。是否将你加入 'input' 组？（需要 sudo）"; }
msg_inputgroup_added()         { [ "$LANG_CHOICE" = en ] && echo "Added to 'input' group. You MUST log out and log back in for it to take effect." || echo "已加入 'input' 组。**必须注销并重新登录后才会生效**。"; }
msg_inputgroup_skipped()       { [ "$LANG_CHOICE" = en ] && echo "Skipped. Remember to run 'sudo usermod -aG input \$USER' and re-login before using whisper-input." || echo "已跳过。使用 whisper-input 前别忘了手动执行 'sudo usermod -aG input \$USER' 并重新登录。"; }
msg_step_tool_install()        { [ "$LANG_CHOICE" = en ] && echo "[5/6] Installing whisper-input via uv tool..." || echo "[5/6] 通过 uv tool 安装 whisper-input..."; }
msg_step_init()                { [ "$LANG_CHOICE" = en ] && echo "[6/6] Running whisper-input --init (downloads ~231MB on first run)..." || echo "[6/6] 运行 whisper-input --init（首次约下载 231MB 模型）..."; }
msg_done_header()              { [ "$LANG_CHOICE" = en ] && echo "Installation complete!" || echo "安装完成！"; }
msg_launch_prompt()            { [ "$LANG_CHOICE" = en ] && echo "Launch whisper-input now?" || echo "是否立即启动 whisper-input？"; }
msg_launch_relogin_warning()   { [ "$LANG_CHOICE" = en ] && echo "You were just added to the 'input' group — log out and back in before launching, otherwise whisper-input cannot read /dev/input/*. Launch anyway?" || echo "你刚刚被加入 'input' 组 —— 启动前必须注销重新登录，否则 whisper-input 无法读取 /dev/input/*。仍然启动？"; }
msg_not_launched_hint()        { [ "$LANG_CHOICE" = en ] && echo "Run 'whisper-input' anytime to start." || echo "之后随时执行 'whisper-input' 即可启动。"; }
msg_noninteractive_default_yes() { [ "$LANG_CHOICE" = en ] && echo "(no tty; defaulting to yes)" || echo "（无交互终端，默认 yes）"; }
msg_noninteractive_default_no()  { [ "$LANG_CHOICE" = en ] && echo "(no tty; defaulting to no)" || echo "（无交互终端，默认 no）"; }

# ---------- 语言选择 ----------
choose_language() {
    if [ ! -r /dev/tty ]; then
        LANG_CHOICE="zh"
        return
    fi
    printf '%s' "$(msg_lang_prompt)"
    _ans=""
    read -r _ans < /dev/tty || _ans=""
    case "$_ans" in
        2|en|EN|English|english) LANG_CHOICE="en" ;;
        *) LANG_CHOICE="zh" ;;
    esac
}

# ---------- 平台 ----------
detect_platform() {
    _os="$(uname -s 2>/dev/null || echo unknown)"
    _arch="$(uname -m 2>/dev/null || echo unknown)"
    case "$_os" in
        Darwin) PLATFORM="macos" ;;
        Linux)  PLATFORM="linux" ;;
        *) die "$(msg_unsupported_os "$_os")" ;;
    esac
    case "$_arch" in
        arm64|aarch64|x86_64) : ;;  # 支持的
        *) warn "$(msg_unsupported_arch "$_arch")" ;;
    esac
}

# ---------- 步骤 1: uv ----------
install_uv() {
    info "$(msg_step_uv)"
    if command -v uv >/dev/null 2>&1; then
        ok "$(msg_uv_present)"
        return
    fi
    info "$(msg_uv_installing)"
    if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
        die "$(msg_uv_install_failed)"
    fi
    # uv 安装器把二进制放到 $HOME/.local/bin，并生成 env 文件（新版）
    if [ -r "$HOME/.local/bin/env" ]; then
        # shellcheck source=/dev/null
        . "$HOME/.local/bin/env"
    fi
    if ! command -v uv >/dev/null 2>&1; then
        die "$(msg_uv_install_failed)"
    fi
    ok "$(uv --version 2>/dev/null || echo uv installed)"
}

# ---------- 步骤 2: Python 3.12 ----------
install_python() {
    info "$(msg_step_python)"
    if uv python find 3.12 >/dev/null 2>&1; then
        ok "$(msg_python_present)"
        return
    fi
    info "$(msg_python_installing)"
    uv python install 3.12
    ok "$(msg_python_present)"
}

# ---------- 步骤 3: 系统依赖 ----------
install_sysdeps_macos() {
    if ! command -v brew >/dev/null 2>&1; then
        if prompt_yesno "$(msg_brew_missing_prompt)" n; then
            info "$(msg_brew_installing)"
            printf '%s\n' "$(msg_brew_sudo_notice)"
            if ! /bin/bash -c "NONINTERACTIVE=1 \$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
                die "$(msg_brew_install_failed)"
            fi
            # 把 brew 加进当前 PATH（Apple Silicon 默认 /opt/homebrew，Intel 默认 /usr/local）
            if [ -x /opt/homebrew/bin/brew ]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [ -x /usr/local/bin/brew ]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
            if ! command -v brew >/dev/null 2>&1; then
                die "$(msg_brew_install_failed)"
            fi
        else
            msg_brew_declined
            exit 1
        fi
    fi
    if brew list portaudio >/dev/null 2>&1; then
        ok "$(msg_portaudio_present)"
    else
        info "$(msg_portaudio_installing)"
        brew install portaudio
    fi
}

install_sysdeps_linux() {
    if ! command -v apt-get >/dev/null 2>&1; then
        msg_linux_not_debian
        exit 1
    fi
    info "$(msg_linux_apt_installing)"
    if [ "$APT_UPDATED" -eq 0 ]; then
        sudo apt-get update
        APT_UPDATED=1
    fi
    sudo apt-get install -y \
        xdotool \
        xclip \
        pulseaudio-utils \
        libportaudio2 \
        libgirepository-2.0-dev \
        libcairo2-dev \
        gir1.2-gtk-3.0 \
        gir1.2-ayatanaappindicator3-0.1
}

install_sysdeps() {
    info "$(msg_step_sysdeps)"
    if [ "$PLATFORM" = "macos" ]; then
        install_sysdeps_macos
    else
        install_sysdeps_linux
    fi
}

# ---------- 步骤 4: Linux input 组 ----------
ensure_input_group() {
    [ "$PLATFORM" = "linux" ] || return 0
    info "$(msg_step_inputgroup)"
    if id -nG "$USER" 2>/dev/null | tr ' ' '\n' | grep -qx input; then
        ok "$(msg_inputgroup_present)"
        return
    fi
    if prompt_yesno "$(msg_inputgroup_prompt)" y; then
        sudo usermod -aG input "$USER"
        INPUT_GROUP_JUST_ADDED=1
        warn "$(msg_inputgroup_added)"
    else
        warn "$(msg_inputgroup_skipped)"
    fi
}

# ---------- 步骤 5: uv tool install ----------
install_whisper_input() {
    info "$(msg_step_tool_install)"
    # --upgrade 处理重复安装 / 升级；--compile-bytecode 跳过首次运行编译 .pyc
    uv tool install --upgrade --compile-bytecode whisper-input
}

# ---------- 步骤 6: --init ----------
run_init() {
    info "$(msg_step_init)"
    whisper-input --init
}

# ---------- 最后：问是否启动 ----------
maybe_launch() {
    printf '\n'
    ok "$(msg_done_header)"
    printf '\n'

    if [ "$INPUT_GROUP_JUST_ADDED" -eq 1 ]; then
        # 新加入 input 组的会话启动会权限失败，默认 N
        if prompt_yesno "$(msg_launch_relogin_warning)" n; then
            exec whisper-input
        else
            printf '%s\n' "$(msg_not_launched_hint)"
            return
        fi
    fi
    if prompt_yesno "$(msg_launch_prompt)" y; then
        exec whisper-input
    else
        printf '%s\n' "$(msg_not_launched_hint)"
    fi
}

# ---------- main ----------
main() {
    choose_language
    printf '%s%s%s\n\n' "$C_BOLD" "$(msg_header)" "$C_RESET"
    detect_platform
    install_uv
    install_python
    install_sysdeps
    ensure_input_group
    install_whisper_input
    run_init
    maybe_launch
}

main "$@"
