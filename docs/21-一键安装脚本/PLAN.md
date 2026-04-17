# 第 21 轮：一键安装脚本

## Context

Whisper Input 目前的 end-user 安装路径是 `uv tool install whisper-input`，对已经装了 `uv` 的技术用户够用，但 BACKLOG「分发 & 安装体验」条目指出这对"只会用 terminal、没碰过 Python 生态"的用户门槛仍然太高 —— 他们要先知道 uv 是什么、怎么装、再逐个查系统依赖（portaudio / xdotool / `input` 组…）。

这一轮交付 `curl | sh` 风格的一键脚本 `install.sh`，部署在 GitHub Raw（暂不考虑自定义域名），把「装 uv / 装 Python 3.12 / 装系统依赖 / Linux `input` 组 / `uv tool install --upgrade` / `whisper-input --init` / 询问是否立即启动」一次做完。

已经存在但不直接复用的参照物：
- `scripts/setup_macos.sh`、`scripts/setup_linux.sh` —— 仓库内脚本，面向 contributor（假定已 `git clone`），不走 curl 管道，不装 `uv tool`，不跑 `--init`，不询问启动
- `whisper-input --init` —— 19 轮加的子命令，macOS 下安装 `~/Applications/Whisper Input.app` bundle（TCC 权限归属需要），全平台下载 SenseVoice ONNX 模型（~231 MB）。同步执行，完成后 return。用于避免用户首次启动时阻塞在模型下载

预期用户命令：

```bash
curl -LsSf https://raw.githubusercontent.com/pkulijing/whisper-input/master/install.sh | sh
```

## 实现方案

### 脚本位置与托管

- 新增 `install.sh` 于**仓库根目录**
- 托管走 GitHub Raw（`raw.githubusercontent.com/pkulijing/whisper-input/master/install.sh`）。master 推到远程后就自动生效，零额外配置
- 不 hash-pin / 不做 release asset 绑定 —— 用户安装时始终拿 master 最新版，跟 uv 的做法对齐

### 脚本约定

- Shebang：`#!/bin/sh`，**纯 POSIX sh**（参考 uv / rustup / oh-my-zsh 的做法）。不依赖 bashism，`curl | sh` 在 macOS 默认 `/bin/sh` 下也能跑
- 开头 `set -eu`，任何一步失败立即退出
- **双语输出**：脚本开头交互让用户选语言（`zh` / `en`，默认 `zh`），把选择存到变量 `LANG_CHOICE`。所有面向用户的字符串通过一个 `msg()` 函数按 `LANG_CHOICE` 选对应语言。非交互场景（无 TTY）→ 默认走中文
  - 内部实现：每条消息一个简单的 shell 函数，如 `msg_installing_uv() { [ "$LANG_CHOICE" = en ] && echo "Installing uv..." || echo "安装 uv..."; }`。不搞 i18n 框架，保持 POSIX sh 风格
- Interactive prompts（语言选择、brew 代装确认、Linux input 组、最后是否启动）**读 `/dev/tty` 而非 stdin**，因为 `curl ... | sh` 的 stdin 是管道，不是终端。读失败（无 TTY，如 CI 或真被 pipe 了）→ 全部走默认值
- `PATH` 主动补丁：脚本开头 `export PATH="$HOME/.local/bin:$PATH"`，保证刚装完 `uv` / `whisper-input` 立刻能找到

### 脚本步骤（顺序）

0. **选语言**：开场 `Language / 语言: [1] 中文 (默认) / [2] English`，读 `/dev/tty` 一个字符，把 `LANG_CHOICE` 设好。后续所有输出按这个选
1. **守卫**：检测 `uname -s`（`Darwin` / `Linux`，其它报错退出）、`uname -m`（`arm64` / `aarch64` / `x86_64` 放行，其它警告但不 block）
2. **装 uv**：`command -v uv` 存在则跳过；否则 `curl -LsSf https://astral.sh/uv/install.sh | sh`。装完 `source "$HOME/.local/bin/env"` 或 re-export PATH
3. **装 Python 3.12**：`uv python list --only-installed --no-config 2>/dev/null | grep -q 3.12` 命中跳过；否则 `uv python install 3.12`
4. **系统依赖**：
   - **macOS**：
     - `command -v brew` 不存在 → 读 `/dev/tty` 问 `[y/N]` 是否代装 Homebrew，默认 N
       - y → 跑官方一行命令（带 `NONINTERACTIVE=1` 跳过 brew 自己的回车确认，但 brew 仍然会弹 sudo 密码 —— 这是 brew 必需的，无法绕过，脚本里打印一行说明让用户有心理准备）。装完再跑 `eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"` 把 brew 加进当前 PATH
       - N → 打印官方一行命令 + 提示装完重跑，退出码 1
     - `brew list portaudio >/dev/null 2>&1` 命中跳过，否则 `brew install portaudio`
   - **Linux**：
     - `command -v apt-get` 存在（Debian/Ubuntu）→ `sudo apt-get update && sudo apt-get install -y xdotool xclip pulseaudio-utils libportaudio2 libgirepository-2.0-dev libcairo2-dev gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1`（和 `README.zh-CN.md` 里的 Linux 装机清单保持一致，比 `setup_linux.sh` 更全）
     - 不是 apt-get → 打印 "检测到非 Debian/Ubuntu 发行版，请手动安装这些包: ..." + 提示用户装完后重跑脚本，退出码 1
5. **Linux `input` 组**：`id -nG "$USER" | grep -qw input` 命中跳过；否则读 `/dev/tty` 问 `[y/N]`，y → `sudo usermod -aG input "$USER"` 并打印"需要注销重新登录才生效"
6. **装 whisper-input**：`uv tool install --upgrade --compile-bytecode whisper-input`
   - `--upgrade` 满足需求原文：重复安装变升级
   - `--compile-bytecode` 与 README 推荐一致，避免首次运行时的 `.pyc` 编译延迟
7. **初始化**：`whisper-input --init`
   - macOS：安装 `.app` bundle + 下模型
   - Linux：下模型
8. **问是否启动**：读 `/dev/tty`，`[Y/n]`（默认 Y）
   - Y / yes / 空 → `exec whisper-input`（前台 exec；shell 退出时应用也跟着退出 —— 跟用户需求"同意就直接启动"一致）
   - n → 打印 "安装完成！之后随时 `whisper-input` 即可启动"
   - 无 TTY → 默认 n（CI 场景下不启动）

### 细节：TTY 读取

POSIX sh 下读 `/dev/tty`：

```sh
prompt_yes_no() {
    # $1 = 问题, $2 = 默认值 (y|n)
    if [ ! -t 0 ] && [ ! -r /dev/tty ]; then
        echo "$1 (非交互，默认 $2)"
        [ "$2" = "y" ]  # 返回码
        return
    fi
    printf '%s ' "$1"
    read -r answer < /dev/tty || answer=""
    case "${answer:-$2}" in
        [Yy]|[Yy][Ee][Ss]) return 0 ;;
        *) return 1 ;;
    esac
}
```

### Linux 装完组 → 立即启动的坑

用户刚被加入 `input` 组时，当前 shell 会话还没拿到新的组成员身份，直接 `exec whisper-input` 会 `PermissionError` on `/dev/input/event*`。处理方式：

- 脚本最后的"是否启动"交互里，**如果刚刚在步骤 5 改过 `input` 组**，记个 flag 并提示用户 "需注销重新登录后再启动"，y/N 默认改成 N。这样不会炸一个一脸懵逼的错误

### README 更新

- 在 `README.md` / `README.zh-CN.md` 的「Installation」段落**最顶部**加一个「一键安装（推荐）」子节：
  ```bash
  curl -LsSf https://raw.githubusercontent.com/pkulijing/whisper-input/master/install.sh | sh
  ```
- 把现有「macOS」/「Linux」子节改成"手动安装"副选项，保留原内容（给想细粒度控制的用户用）
- 英文 README 同步更新

### 不做

- 不支持 Windows
- 不做自定义域名 / 短链
- 不支持 dnf / pacman（检测到非 apt 就让用户手动装，附依赖清单）
- 不加 CI 测试脚本的完整运行（install.sh 在真干净机器上测 matrix 是一大工程，这一轮只做脚本本体 + 手动验证，BACKLOG 里"测试矩阵"那条保留到未来）

## 关键文件

- **新增** `install.sh`（仓库根目录）
- **修改** `README.md`、`README.zh-CN.md`：「Installation」段落重写
- **新增** `docs/21-一键安装脚本/SUMMARY.md`：收尾时写
- **更新** `BACKLOG.md`：删掉「一键安装脚本（`curl | bash` 风格）」条目
- 相关但**不修改**：`scripts/setup_macos.sh`、`scripts/setup_linux.sh`（contributor workflow，保留）、`src/whisper_input/__main__.py:239-260`（`--init` 已实现，直接调用）

## 验证

### macOS（Apple Silicon）

1. 用一台（或一个干净的 macOS 用户）**没装过 whisper-input** 的机器 —— 清掉 `~/Applications/Whisper Input.app` / `~/.local/share/uv/tools/whisper-input`
2. `curl -LsSf https://raw.githubusercontent.com/pkulijing/whisper-input/<branch>/install.sh | sh` —— 注意测试时用 feature 分支的 raw URL，合到 master 后才是 `master`
3. 确认：uv 装上、Python 3.12 装上、portaudio 装上、`uv tool install` 成功、`whisper-input --init` 跑完（看到 `.app` 建好 + 模型进度）
4. 最后 prompt 回车 → whisper-input 启动、托盘图标出现、浏览器打开设置页
5. **重跑一次** `install.sh`：应全程"已安装，跳过"；`uv tool install --upgrade` 变 no-op；`--init` 走一遍但模型已在缓存不重下
6. `uv tool install --upgrade` 想模拟升级：`uv tool uninstall whisper-input && uv tool install whisper-input==<旧版本>`，再跑 install.sh，确认升到最新

### Linux（Ubuntu 24.04 全新机器 / 干净 container）

1. 从未装过 whisper-input、未加 input 组、未装系统依赖
2. `curl ... | sh`
3. 确认：sudo apt-get 装依赖成功、`input` 组交互被触发并接受、`uv tool install` 成功、模型下载完
4. 最后 prompt：**因为刚刚改过 `input` 组，脚本应提示"需注销重新登录后再启动"，默认 N** —— 验证这个分支
5. 注销重新登录后手动 `whisper-input`，确认能跑

### 非交互（CI / 真被 pipe）

- `curl ... | sh < /dev/null`：应**走默认值**跑完全部自动步骤，但跳过 `input` 组交互（默认 N）、跳过最后启动（默认 N），不卡死

### 不支持的平台

- Fedora（dnf）或 Arch（pacman）：脚本应打印手动装依赖的提示 + 清单，退出码 1
- Windows（Git Bash）：`uname -s` 是 `MINGW*` / `MSYS*`，应在守卫处报错退出

### 语言切换验证

- 脚本开场选 `2`（English）→ 后续所有提示、错误信息、交互问题都是英文
- 脚本开场选 `1`（或回车）→ 中文
- 无 TTY → 默认中文，全程中文
