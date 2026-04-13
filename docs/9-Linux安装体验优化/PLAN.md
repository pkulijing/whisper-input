# Linux 安装体验优化 — 实现计划

> **实施中调整 (2026-04-13)**：本 PLAN 原本规划把 setup_window.py / python_dist.txt 放在新建的 `linux/` 目录下，对称 `macos/`。实施时发现 `debian/` 本身就是 Linux 唯一的平台目录（存 control / postinst / trampoline 等），再开一个 `linux/` 是冗余。最终把 `setup_window.py` 和 `python_dist.txt` 直接放进 `debian/`，安装到 deb 的 `/opt/whisper-input/` 根下（和 main.py 同级）。下文所有 `linux/xxx` 路径请读作 `debian/xxx`。

## 目标回顾

两个诉求合并成一件事：

1. 把 `sudo apt install whisper-input.deb` 从 5-10 分钟的重操作降为秒级动作（PROMPT 原始诉求）。
2. **对齐 macOS 的首启体验**：在程序启动的时候弹出一个**统一的 tkinter 窗口**，按三阶段展示所有耗时动作——uv 依赖安装、SenseVoice 模型下载、模型加载到内存——同一窗口、同一进度条、同一片滚动日志，全部结束后窗口关闭、主程序进入托盘。

参考 `docs/7-macOS分发优化/PLAN.md` 的方案：**单进程贯穿三阶段**，stage C 结束前 setup_window 始终活着，main.py 由 setup_window 作为 subprocess 拉起。

## 现状快照

- `debian/postinst`：L14-39 curl 装 uv；L41-64 `uv sync`；L66-96 预下载模型；L98-123 图标/desktop/完成提示。前两大段要整个删掉。
- `debian/whisper-input.sh`（安装后是 `/usr/bin/whisper-input`）：当前直接 `exec uv run python main.py`，还有一段 venv 缺失时的 `uv sync` 兜底（L40-51）——这段兜底会被 setup_window 接管。
- `debian/control`：当前依赖列表没涉及 python 运行时以外的 GUI 栈。**不走 apt 的 `python3-tk`**（见决策 3）。
- `macos/setup_window.py`：成熟的三阶段实现，500 行 stdlib-only，**Linux 版本直接照搬结构**（SetupWindow class + UI 队列 + pty worker + stage 状态机），只改平台相关的路径常量和 uv/python 定位逻辑。
- `main.py` L430 `if not args.no_preload: wi.preload_model()`：**默认就是启动时预加载模型**，完全契合本轮"在 stage C 内看到模型加载完成"的需求，**不需要改 main.py**。
- `stt_sensevoice.py` 打印的 `[sensevoice] 模型加载完成` / `[main] 预加载 SenseVoice 模型` marker，mac 版已在用，Linux 版复用同两个 marker。
- `CLAUDE.md` 里写着 `SenseVoiceSTT: ..., lazy model loading, ...`。这里的"lazy"描述的是 `_model = None` → 首次调用才加载的**内部行为**，而 `main.py` 默认会在启动时调用 `preload_model()`。本轮要明确表述"默认启动时预加载"，让后续看文档的人不会误以为可以靠 lazy 来绕过 stage C——顺手改一行。
- 当前分支 `feat/linux-install-ux` 工作树干净，可以直接开工。

## 关键设计决策

### 决策 1：三阶段单窗口（主要设计）

完全复刻 mac PLAN 的 stage 状态机，Linux 只改平台相关部分：

| 方面 | macOS | Linux |
|---|---|---|
| trampoline | `macos/whisper-input.sh`，exec 到 bundled python | `debian/whisper-input.sh`，exec 到 `uv python find 3.12` 返回的 python |
| GUI python | `Contents/Resources/python/bin/python3`（bundle 在 .app 里、构建期 curl 拉 python-build-standalone） | `uv python install <PYTHON_VERSION>` 让 uv 拉同一份 python-build-standalone 到用户 uv 缓存，**版本号在 `linux/python_dist.txt` 里锁死**，与 mac 对齐 |
| python 版本锁定 | `macos/python_dist.txt`（RELEASE + PYTHON_VERSION + URL + SHA256，build.sh 构建期用） | `linux/python_dist.txt`（PYTHON_VERSION，trampoline 运行期用；URL/SHA256 不用自己管，交给 uv 从 astral 拉） |
| uv 定位 | `Contents/Resources/uv`（bundled） | 用户 PATH 里的 `uv`（setup.sh 约定），`$HOME/.local/bin/uv` 或 `$HOME/.cargo/bin/uv` |
| user venv | `~/Library/Application Support/Whisper Input/.venv` | `~/.local/share/whisper-input/.venv`（当前已是此路径） |
| APP_SRC | `.app/Contents/Resources/app/` | `/opt/whisper-input` |
| 日志 | `~/Library/Logs/WhisperInput.log` | `${XDG_STATE_HOME:-~/.local/state}/whisper-input/whisper-input.log` |
| 三阶段 | A uv sync / B modelscope / C 启动 main.py | **完全一致** |
| Stage C marker | `[sensevoice] 模型加载完成` | **一致** |

Stage 切换逻辑、worker 线程 + pty、UI queue、错误屏、详细文案**全部照抄 mac 版**，这是已经走通的路径。

### 决策 2：uv 不在 postinst 里装、不作为 apt Depends、改为"前置要求 + setup_window 启动时硬检查"

`uv` 不在 apt 仓库，没法通过 `Depends:` 做硬依赖。选择：

- `postinst` **完全不碰 uv**（不 curl、不检测）。
- `debian/whisper-input.sh` trampoline 启动 setup_window 前先尝试把 `$HOME/.local/bin` / `$HOME/.cargo/bin` 加进 PATH。
- `setup_window.py` 的 **Stage A 启动前**先 `shutil.which("uv")`，找不到就进入错误屏（窗口里红字 + 日志区给出安装命令），不继续进 stage A，用户点关闭即可退出。相比 notify-send，窗口内错误屏是更好的体验——日志区能直接展示复制粘贴命令。
- `README.md` 明确把 uv 标为**必装前置**。

**为什么不让 postinst 硬 fail**：PROMPT 要求"秒级完成安装"，postinst 硬 fail 会让 apt 进入"已解压但配置失败"状态，恢复麻烦；而且 postinst 的 stderr 在 Discover / GNOME Software 里用户看不到，错误反馈质量远不如窗口内错误屏。PROMPT 里"明确报错退出"这句话，更准确的落点是 launcher/setup_window 而不是 postinst。

### 决策 3：setup_window 跑在 uv 管的 python-build-standalone 上，不吃 apt 的 python3-tk

**核心原则**：项目一律用 `uv` 管的 python，不依赖 apt 发行版带的 python 版本和绑定包。apt 的 `python3-tk` 和系统 `python3` 强绑定、版本固定、受发行版限制——和项目 `pyproject.toml` 里 `requires-python = ">=3.12"` 的约束可能对不上，而且违反"项目自己管 python"的原则。

取而代之：

- `debian/control` **不加** `python3-tk`，不需要 apt 的 python 扩展包。
- **新增 `linux/python_dist.txt`**，对称 `macos/python_dist.txt`，但只锁一行 `PYTHON_VERSION=3.12.13`（版本号和 mac 那份对齐，避免两端漂移）。URL/SHA256 不需要，因为 Linux 路径不自己下载解压、而是委托给 uv。
- trampoline 里 `source linux/python_dist.txt` 取 `PYTHON_VERSION`，然后 `uv python install "$PYTHON_VERSION"` 确保该确定版本已就绪（幂等，已装则秒过；未装则 uv 去 astral 的 python-build-standalone releases 拉，约 30MB）。
- 然后 `PYBIN="$(uv python find "$PYTHON_VERSION")"` 拿到这个**确定版本**的解释器路径，`exec "$PYBIN" setup_window.py`。
- python-build-standalone 的 install_only 变体**自带 tkinter**（mac PLAN 已验证；install_only 变体在 Linux/macOS 上都一样），所以 setup_window.py 的 `import tkinter` 直接可用。
- setup_window.py 严格 stdlib-only，不 `uv sync` 项目依赖也能跑。

**为什么锁精确版本而不是 `3.12`**：`uv python find 3.12` 返回"已缓存的 any 3.12.x"是不确定行为——uv 版本升级或用户别的项目装过不同的 3.12 patch 版本会导致 Linux 端跑的 python 和 mac 端漂移，本项目自己的供应链就失控了。锁 `3.12.13` 和 mac 锁死同一份 release，两端 python stdlib 行为完全一致，升级时一起升。

### 决策 3.1：python 下载是 "stage 0"，发生在 tkinter 窗口打开之前

关键约束：**tkinter 窗口本身就需要 python 先存在才能打开**。所以 python-build-standalone 的下载**不能**塞进 setup_window 的三阶段里——窗口还没开的时候无法给 GUI 反馈。

分层是这样：

```
trampoline（shell，无 GUI，只 notify-send）
  ├─ stage 0：uv python install <PYTHON_VERSION>（首启下载 ~30MB，已装则秒过）
  └─ exec <PYBIN> setup_window.py
       │
       └─ setup_window（tkinter 窗口）
            ├─ stage A：uv sync --python <PYTHON_VERSION>
            ├─ stage B：modelscope 下载 SenseVoice 模型
            └─ stage C：启动 main.py + 等 "模型加载完成" marker
```

- stage 0 的反馈形式：仅一条 `notify-send` "首次启动：正在准备 Python 运行环境（约 30MB）..."。30MB 在国内网络 10-30 秒，没有进度条但可接受；极端网络慢的情况本轮不兜底（zenity 弹条需要再引依赖，暂时不做）。
- stage A 的 `uv sync` 必须显式传 `--python "$PYTHON_VERSION"`，强制用户 venv 的 python 和 setup_window 跑的 python 是**同一份 python-build-standalone**，避免两者漂到不同的 3.12.x patch 版本。
- 冷启动视觉序列：用户双击图标 → notify "正在准备 Python" → 几十秒后 tkinter 窗口弹出 → stage A/B/C 走完 → 窗口消失 → 托盘图标出现。
- 热启动（stage 0 / A / B 都命中缓存）：用户双击图标 → tkinter 窗口秒弹 → 只走 stage C → 几秒后消失进托盘。

顺手把 `libnotify-bin` 加进 `Depends:`（提供 `notify-send`，是 OS 级小工具不是 python 绑定，和"不吃 apt python 栈"的原则不冲突），用于 trampoline 的起始通知和错误兜底。

### 决策 4：移除 launcher 里 "venv 缺失就 uv sync" 的兜底

那段兜底被 setup_window 的 Stage A 完全接管。trampoline 只做：设 LOG、设 PATH、设 GI_TYPELIB_PATH、检查 input 组、检查 tkinter 可导入、exec setup_window.py。整条链路简化成 mac 那样的 ~15 行 trampoline。

### 决策 5：不做"模型延迟到首次录音"

这是初版 PLAN 的想法，被用户明确否决。理由：既然有统一窗口，不如让用户在窗口里一次性看完"依赖 → 模型 → 加载"三件事，而不是双击完启动没反馈、按热键才开始下 500MB——后者体验更糟。

这一决策导致 `CLAUDE.md` 里"lazy model loading"的描述需要更新，顺手改一行（见 Step 6）。**注意**：实际代码 `stt_sensevoice.py` 的 `_model = None` + 首次调用才加载的行为不动，main.py 默认 `preload_model()` 的行为也不动——需要改的只是 CLAUDE.md 的一行文字描述。

## 文件布局

新增 `linux/setup_window.py`，对称于 `macos/setup_window.py`。原因：setup_window 是平台 GUI 前端，放 `linux/` 和 mac 的 `macos/` 对齐。该文件安装后随 `/opt/whisper-input/linux/setup_window.py` 一起复制（打包脚本要包含 `linux/`）。

```
whisper-input/
├── macos/setup_window.py       # 已存在
├── linux/setup_window.py       # 新增（本轮）
└── debian/
    ├── control                 # 加 python3-tk libnotify-bin
    ├── postinst                # 精简
    ├── whisper-input.sh        # 重写为 trampoline
    ├── prerm / postrm          # 不动
```

## 实施步骤

### Step 1 — 新增 `linux/setup_window.py`

直接以 `macos/setup_window.py` 为模板，做以下 Linux 化改动：

1. **路径常量**
   ```python
   APP_DIR = Path(os.environ.get("WHISPER_INPUT_APP_DIR", "/opt/whisper-input"))
   APP_SRC = APP_DIR                                           # Linux 上 APP_DIR 直接就是源码根
   USER_DATA_DIR = Path(
       os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share")
   ) / "whisper-input"
   USER_VENV = USER_DATA_DIR / ".venv"
   USER_VENV_PYTHON = USER_VENV / "bin" / "python"
   DEPS_SENTINEL = USER_DATA_DIR / ".deps_sha256"
   LOG_FILE = Path(
       os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state")
   ) / "whisper-input/whisper-input.log"
   ```

2. **uv 定位**：mac 版写死 `APP_DIR/uv`；Linux 版用 `shutil.which("uv")`，为 None 则进错误屏并给出官方安装命令。PATH 由 trampoline 注入。

3. **python 解释器**：Stage A 的 `uv sync` 不传 `--python`（mac 版传的是 bundled python，Linux 直接让 uv 自己挑系统 python3.12，和现状一致）。

4. **Stage C 启动 main.py**：传给 `subprocess.Popen` 的 python 是 `USER_VENV_PYTHON`，`cwd=APP_SRC`，`start_new_session=True`，stdout/stderr 重定向到 LOG_FILE——逻辑与 mac 完全相同。注意 **不加 `--no-preload`**，让 main.py 走默认预加载路径，这样 `[sensevoice] 模型加载完成` 会在 setup_window 还活着的时候被抓到。

5. **GI_TYPELIB_PATH 透传**：Stage C 的 env 里保留 `GI_TYPELIB_PATH`（由 trampoline 注入），main.py 自己也会兜底设，但 trampoline 先设更稳。

6. **input 组检查**：不放在 setup_window 里，在 trampoline 里做（见 Step 2）——因为这是 shell 能直接处理的东西。

7. **uv sync 参数**：mac 版用 `uv sync --python <bundled> --no-progress --color=never`；Linux 版用 `uv sync --python "$PYTHON_VERSION" --no-progress --color=never`——**必须显式传 `--python`**，保证用户 venv 用和 setup_window 同一份锁定版本（见决策 3.1）。`PYTHON_VERSION` 从 `linux/python_dist.txt` 读到，trampoline 通过环境变量 `WHISPER_INPUT_PYTHON_VERSION` 注入给 setup_window。其它参数（`UV_PROJECT_ENVIRONMENT` 指向 `USER_VENV`、`cwd=APP_SRC`）一致。

8. **错误屏对 uv 缺失的特殊文案**：stage A 启动前发现 uv 缺失 → 错误屏标题 "未找到 uv 包管理器"，日志区写三行安装指引（curl 一行命令 + pipx 一行命令 + 安装后注销重登 / 重开终端）。

9. **代码风格**：ruff 规则和 mac 版一致，line length 80，stdlib only。

### Step 2 — 重写 `debian/whisper-input.sh` 为 trampoline

精简到 ~30 行：设日志 / 设 PATH / 检查 input 组 / 确认 uv / 确认 uv 管的 python 3.12 / exec setup_window：

```bash
#!/bin/bash
set -e

# 日志（XDG state 目录）
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/whisper-input"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/whisper-input.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== $(date) ==="

# 路径 / 环境
export WHISPER_INPUT_APP_DIR="/opt/whisper-input"
export GI_TYPELIB_PATH="/usr/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

notify() {
    command -v notify-send >/dev/null && notify-send "Whisper Input" "$1" || true
}
notify_crit() {
    command -v notify-send >/dev/null && notify-send -u critical "Whisper Input" "$1" || true
}

# input 组硬检查
if ! groups 2>/dev/null | grep -qw input; then
    MSG="当前用户不在 input 组中，请执行 sudo usermod -aG input \$USER 后注销重登"
    echo "$MSG" >&2
    notify_crit "$MSG"
    exit 1
fi

# uv 硬检查
if ! command -v uv >/dev/null; then
    MSG="未找到 uv 包管理器。请先安装：curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "$MSG" >&2
    notify_crit "$MSG"
    exit 1
fi

# 锁定的 python-build-standalone 版本（和 mac 对齐）
# shellcheck disable=SC1091
. "$WHISPER_INPUT_APP_DIR/linux/python_dist.txt"   # 注入 PYTHON_VERSION
export WHISPER_INPUT_PYTHON_VERSION="$PYTHON_VERSION"

# stage 0：确认 uv 管的 python 已就绪（首启由 uv 从 astral 拉 ~30MB 的 install_only 变体，自带 tkinter）
if ! PYBIN="$(uv python find "$PYTHON_VERSION" 2>/dev/null)"; then
    notify "首次启动：正在准备 Python $PYTHON_VERSION 运行环境（约 30MB）..."
    echo "uv python install $PYTHON_VERSION ..."
    uv python install "$PYTHON_VERSION"
    PYBIN="$(uv python find "$PYTHON_VERSION")"
fi
echo "PYBIN=$PYBIN"

exec "$PYBIN" "$WHISPER_INPUT_APP_DIR/linux/setup_window.py"
```

注意：
- `linux/python_dist.txt` 写成 `PYTHON_VERSION=3.12.13` 这种 shell-source 友好的 key=value 格式，可以 `.` 直接 source（和 `macos/python_dist.txt` 的格式一致，mac 的 `build.sh` 也是这样读的）。
- `uv python find` 找不到时返回非零退出码，所以 `if ! PYBIN=...` 分支就是"需要先 install"的路径。`set -e` 不会在 `if` 条件里终止脚本。

### Step 3 — 精简 `debian/postinst`

- 删除 L14-39（curl 装 uv）
- 删除 L41-96（uv sync + 模型预下载）
- 保留 L7-12（加 input 组）
- 保留 L98-106（icon / desktop-database）
- 完成提示文案升级为三行：
  1. 已加入 input 组，请注销重登
  2. **需先安装 uv**（给出 curl 一行安装命令）
  3. 首次启动会弹窗下载 torch + funasr + 模型（~2.5GB），需要联网和耐心

### Step 4 — 更新 `debian/control`

`Depends:` 追加：
- `libnotify-bin` — trampoline / setup_window 的 notify-send 通知

**不加** `python3-tk`：tkinter 由 uv 管的 python-build-standalone 提供（决策 3）。

顺便审视现有 `Depends:` 里的 `python3 (>= 3.12)`——保留，作为兜底（系统里最好有个 python3，虽然本项目运行链路完全走 uv 管的 python）。其它 `xdotool / xclip / pulseaudio-utils / libportaudio2 / gir1.2-appindicator3-0.1 / libgirepository-2.0-dev / gcc / libcairo2-dev / pkg-config` 都是运行时/编译时真依赖，保留不动。

### Step 5 — 更新 `build.sh` 的 Linux 分支

确保打包时把 `linux/setup_window.py` 复制到 deb 的 `/opt/whisper-input/linux/`。当前 `build.sh` 怎么选文件需要先读一眼，大概率是按目录/通配符来的，可能顺手带进去，也可能需要显式补一行。实施时打开 `build.sh` 确认后再动。

### Step 6 — 更新 `README.md` 和 `CLAUDE.md`

`README.md`：
- L22-26 "系统要求 > Linux" 把 uv 那行加粗并标注 "**必须预装**"，给出一行 curl 安装命令
- L78-84 "DEB 安装包" 段补一段：首次启动会弹出初始化窗口，依次完成依赖安装（~800MB）、模型下载（~900MB）、模型加载到内存，全程有进度显示，首次约 5-10 分钟
- `bash build_deb.sh` → `bash build.sh`（和实际文件名对齐）

`CLAUDE.md`：
- 找到 "lazy model loading" 一行，改为类似 "模型在启动时预加载（main.py 默认行为），可用 --no-preload 关闭" 的描述。

项目根 `CLAUDE.md` 归项目指令，本轮明确涉及它的语义所以可以动。全局 `~/.claude/CLAUDE.md` 不动。

### Step 7 — 本机构建与冒烟测试

当前机器是 Ubuntu 24.04，可直接验证。测试顺序：

1. **deb 构建通过**：`bash build.sh`，产物在 `build/deb/whisper-input_*.deb`
2. **deb 秒装**：`sudo apt install ./build/deb/whisper-input_*.deb`，观察 postinst 输出应无 curl / 无 uv sync / 无模型下载，< 5 秒完成
3. **冷启动三阶段**：
   - `rm -rf ~/.local/share/whisper-input`（清掉 venv 和 sentinel）
   - `rm -rf ~/.cache/modelscope/hub/iic/SenseVoiceSmall*`（清掉模型缓存）
   - 注销重登（input 组生效）
   - 应用菜单点 Whisper Input
   - **预期**：tkinter 窗口弹出 → Stage A 进度条跑 uv sync → Stage B 进度条跑模型下载 → Stage C 标签切到"加载模型" → 看到 "模型加载完成" → 窗口消失 → 托盘图标出现
4. **热启动**：再次点图标，Stage A/B 跳过，只走 Stage C，几秒内窗口关闭进托盘
5. **uv 缺失错误屏**：临时 `mv ~/.local/bin/uv ~/.local/bin/uv.bak`，点图标，验证 tkinter 窗口弹出并进错误屏，文案清晰、日志区写安装命令。测完恢复。
6. **input 组缺失**：临时拿一个不在 input 组的账号验证 trampoline 的硬失败路径（或直接跑 `setsid -f env -i HOME=/tmp/empty bash -c '...'` 模拟，可选）
7. **升级路径**：改一下 `pyproject.toml` 加减一个无关依赖、重 build 重装、点图标 → 预期 Stage A 检测到 hash 变化重跑 sync，Stage B/C 按缓存情况跳过 / 走过
8. **正常使用**：所有阶段走完后按热键录一句话，验证录音-识别-粘贴链路正常

### Step 8 — 写 SUMMARY.md

按全局 CLAUDE.md 要求的模板（背景 / 实现方案 / 局限性 / 后续 TODO）总结。局限性至少：

- uv 仍需用户预装（本轮主动 trade-off，非开发者需要先跟文档走一步 curl）
- tkinter 依赖系统 `python3-tk`，如果用户环境奇特删了 python3-tk，trampoline 会报错并退出，不自救
- deb 未签名
- 仅 apt 系（yum / pacman 不做）

## 本轮不做

- 离线 deb（不把 torch 打进包）
- GPG 签名
- yum / pacman 适配
- CI workflow 改动
- 全局 `~/.claude/CLAUDE.md` 的任何修改

## 风险与对策

| 风险 | 对策 |
|---|---|
| Linux 用户环境五花八门，tkinter 窗口字体 / DPI 在 HiDPI 屏奇怪 | mac 版代码里没有特殊 DPI 处理，先照抄，实测不行再补 `tk.call('tk', 'scaling', ...)` |
| `/opt/whisper-input` 是只读但 `uv sync` 要在 APP_SRC 里跑 | mac 版也是这个结构：cwd=APP_SRC 只是为了让 uv 找到 pyproject.toml 和 uv.lock，`UV_PROJECT_ENVIRONMENT` 指向用户目录，uv 不会往 APP_SRC 写东西。同理适用 Linux |
| setup_window 跑到一半用户关窗 | mac 版已有 `_on_close` 逻辑 terminate current_proc，照抄；Stage C 已经 `start_new_session` detach，关窗不会 kill main.py |
| `~/.cargo/bin/uv`（老安装路径）和 `~/.local/bin/uv`（新路径）同时存在 / 都不存在 | PATH 同时 prepend 两个；`shutil.which("uv")` 会按 PATH 顺序找到任意一个 |
| deb 里 `python3 (>= 3.12)` 在 Ubuntu 24.04 上默认是 3.12.x，没问题；但 setup_window 跑的是系统 python3，和用户 venv 的 python 版本可能不一致 | setup_window 只用 stdlib，不 import 任何第三方包，所以版本差异不影响 |

## 交付物

- 新增：`linux/setup_window.py`
- 重写：`debian/whisper-input.sh`（trampoline 化）
- 精简：`debian/postinst`
- 更新：`debian/control`（加 python3-tk libnotify-bin）
- 可能更新：`build.sh` Linux 分支（如果不自动带 linux/ 目录则补一行）
- 更新：`README.md`、`CLAUDE.md`（一行描述）
- 新增：`docs/9-Linux安装体验优化/SUMMARY.md`
- 本机 Ubuntu 24.04 上验证通过的 deb
