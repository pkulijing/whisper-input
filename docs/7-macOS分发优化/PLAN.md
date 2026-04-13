# macOS 分发优化 — 实施计划

## Context

跟进 `PROMPT.md` 中的需求。最终路线是 **bundle uv + bundle python-build-standalone**，不引入 PyApp/cargo。setup_window.py 单进程贯穿三阶段。

## 关键设计：单窗口贯穿三阶段

整个 GUI 进程只有一个，就是 shell trampoline 启动起来的 setup_window.py。它的 `tk.Tk()` 生命周期贯穿 stage A → B → C → 收尾，**绝不会**关一个窗口再开一个、也不会让 main.py 自己起独立窗口。

- Stage A（首启 / 缺依赖）：bundle 的 uv 跑 `uv sync`，pty 流式读输出，解析 `+ pkg` / `Downloading` / `Installed` 行更新进度条。
- Stage B（首启 / 缺模型）：subprocess 跑 modelscope `snapshot_download`，pty 解析 tqdm 百分比。
- Stage C（每次启动）：subprocess 跑 user venv 的 python 起 main.py，从 stdout 抓 `[sensevoice] 模型加载完成` 这行作为完成信号。
- 三阶段都用同一套 pty + 工作线程 + `root.after(...)` 回主线程刷新 UI 的模式，主线程从头到尾在跑 tkinter `mainloop`。

Stage C 完成时 setup_window 不 kill main.py：用 `start_new_session=True` 让 main 自成进程组，setup_window 隐藏窗口、跑 1s 收尾动画后 `sys.exit(0)`，main.py 继续在托盘里活着。

为什么上次说"C 没法和 A/B 共用窗口"是错的：tkinter mainloop 和 pystray NSApplication 在**两个进程里**互不冲突；TCC 责任归属由父进程链决定，main.py 作为 setup_window 的子进程，责任 bundle 仍向上追溯到 Whisper Input.app（详见下文 TCC 节）。

## .app 布局

```
Whisper Input.app/
└── Contents/
    ├── Info.plist                              # 不变
    ├── MacOS/
    │   └── whisper-input                       # shell trampoline，CFBundleExecutable
    └── Resources/
        ├── AppIcon.icns
        ├── uv                                   # bundle uv（沿用现状）
        ├── python/                              # bundle python-build-standalone arm64
        │   ├── bin/python3                      # 自带 Tk，不需要 TCL_LIBRARY hack
        │   ├── lib/...
        │   └── ...
        └── app/                                 # 真实源码树
            ├── main.py
            ├── stt_sensevoice.py
            ├── ... (所有 .py)
            ├── backends/
            ├── pyproject.toml                   # 给 uv sync 用
            ├── uv.lock
            ├── .python-version
            └── setup_window.py                  # 三阶段 GUI 主体
```

## 启动链路

```
Finder 双击 Whisper Input.app
  └─ Contents/MacOS/whisper-input  (shell trampoline)
       ├─ APP_DIR=$(...)
       ├─ export WHISPER_INPUT_APP_DIR / 一些 LANG
       └─ exec "$APP_DIR/python/bin/python3" "$APP_DIR/app/setup_window.py"
            └─ tkinter 窗口立即显示
                 ├─ Stage A（缺 user venv 时）：bundle uv 跑 uv sync → user venv
                 ├─ Stage B（缺模型时）：snapshot_download SenseVoiceSmall
                 └─ Stage C（每次）：Popen user venv 的 python main.py
                      └─ 抓到 "模型加载完成" → 隐藏窗口 → setup_window 退出
                           └─ main.py 继续在托盘里跑
```

## 两个 python 的分工

- **bundled python**（`Contents/Resources/python/`）：只用来跑 `setup_window.py`，stdlib + tkinter。这个 python **不装任何第三方包**，setup_window.py 也只用 stdlib。
- **user venv**（`~/Library/Application Support/Whisper Input/.venv/`）：装真正的重量级依赖（torch / funasr / pynput / sounddevice / pystray 等）。stage A 用 bundle 的 uv 对着 `Resources/app/pyproject.toml` 跑 `uv sync` 创建。stage C 起 main.py 时用这个 venv 的 python。

> 为什么要分两套：bundled python 在 .app 里、只读、不可写，不能往里装东西。user venv 在用户家目录，可读写，承载所有重量级依赖。setup_window.py 的角色就是"用 bundled python 拉起 GUI，再用 bundle 的 uv 把 user venv 装好"。

## TCC 权限归属

- CFBundleExecutable 仍指 `Contents/MacOS/whisper-input`（shell trampoline）。
- shell exec 到 `Contents/Resources/python/bin/python3`，这个 binary 在 .app 内部，比上一版（在 user data 的 venv 里）更接近 Apple 期望的位置。
- main.py 由 setup_window.py 起的 subprocess 跑，python 二进制路径变成 user venv 的 python（在用户家目录），但**父进程链顶端是 .app 的 shell trampoline**，TCC 责任 bundle 应仍归 Whisper Input.app。

**这一点必须实测**。实施时的兜底顺序：

1. 先按最干净的方式做：python 放 `Contents/Resources/python/`，shell 直接 exec。
2. 出 .dmg → 拖 /Applications → 双击 → 触发麦克风弹窗 → 看弹窗写的归属。
3. 如果归属错乱（比如显示 "python3" 而不是 "Whisper Input"），按以下顺序回退：
   - 回退 1：把整棵 python 树搬到 `Contents/MacOS/python/`（Apple 约定的可执行文件位置）
   - 回退 2：在 user data 下创建一个 helper.app 包 user venv 的 python（重新启用上一版的 helper-app 套路，但只在 stage C 启动 main.py 时按需创建）

兜底方案不写进首版代码，等实测看到具体归属再补。

## 组件改动

### 1. 新增 `macos/python_dist.txt`

记录 python-build-standalone 的固定版本 + URL + sha256，方便 build.sh 读取，也方便后续手动升级。内容形如：

```
RELEASE=20250115
PYTHON_VERSION=3.12.8
URL=https://github.com/astral-sh/python-build-standalone/releases/download/20250115/cpython-3.12.8+20250115-aarch64-apple-darwin-install_only_stripped.tar.gz
SHA256=<填实际值>
```

实施时去 https://github.com/astral-sh/python-build-standalone/releases 找最新的 install_only_stripped 变体，把 sha256 抄进来。

### 2. 重写 `macos/whisper-input.sh`（shell trampoline）

精简到 ~10 行，不再做：helper.app 复制 python、ln dylib、Launch Services 注册、Info.plist 生成。

```bash
#!/bin/bash
set -e
LOG_FILE="$HOME/Library/Logs/WhisperInput.log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== $(date) ==="

APP_DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"
export WHISPER_INPUT_APP_DIR="$APP_DIR"

exec "$APP_DIR/python/bin/python3" "$APP_DIR/app/setup_window.py"
```

### 3. 重写 `macos/setup_window.py`（三阶段 GUI 主体）

完全 stdlib（含 tkinter），路径常量从 `WHISPER_INPUT_APP_DIR` 环境变量读。

整体结构：

- `class SetupWindow`：单 tk 窗口，stage 状态机 `STAGE_DEPS / STAGE_MODEL / STAGE_LOAD / DONE`。
- 复用上一版可参考的解析器（pty 流式读 + 行解析），但**重写**而不是 copy（旧版有 bug 和冗余）。
- 进入 `main()` 后立即 `tk.Tk()`、显示窗口、`after(500)` 进 stage 流程。
- 每个 stage 一个 worker 线程，pty 读子进程输出，主线程 `root.after` 刷 UI。
- Stage 转换：A done → B start，B done → C start，C done → 收尾 → `root.destroy()` → 进程退出。
- Stage A 启动前检查 user venv 是否存在 + pyproject.toml hash 是否变化（写一个 sentinel 文件 `.pyproject.hash`），不需要就跳过 stage A。
- Stage B 启动前检查 modelscope 缓存里是否已经有模型，不需要就跳过 stage B。
- Stage C 每次都跑（模型加载在内存里、不可跨进程复用）。
- Stage C 子进程必须 `start_new_session=True`，setup_window 退出时不 kill。
- 异常处理：任一 stage 失败 → 显示错误信息 + 一个"打开日志"按钮 + "重试"按钮，不自动关窗。

### 4. 重写 `build.sh` 的 `build_macos()`

新流程：

1. 检查工具：`sips iconutil hdiutil curl shasum`（无 cargo）
2. 生成 .icns（不变）
3. **下载 python-build-standalone 到 build/macos/cache/**：
   - 读 `macos/python_dist.txt` 拿 URL + sha256
   - 用 curl 下到 cache（带 cache 命中检查，sha256 对就跳过下载）
   - tar -xf 到 `build/macos/cache/python-${VERSION}/`
4. 组装 .app bundle：
   - `Contents/Info.plist`：sed 注入 VERSION，不变
   - `Contents/MacOS/whisper-input`：copy `macos/whisper-input.sh`
   - `Contents/Resources/AppIcon.icns`
   - `Contents/Resources/uv`：copy `$(command -v uv)`
   - `Contents/Resources/python/`：cp -R 从 cache 解压目录复制（用 -R 保留 symlink 和权限）
   - `Contents/Resources/app/`：copy SOURCE_PY / SOURCE_BACKENDS / SOURCE_OTHER + setup_window.py + assets
5. 打 DMG（不变）

Linux 分支完全不动。

### 5. 删除上一版的实验性产物

- `macos/whisper-input.sh` 当前的内容（里面一坨 helper-app 创建逻辑）→ 重写为 trampoline
- `macos/setup_window.py` 当前的内容 → 重写

注：这两个文件是 untracked 的实验产物，不需要 `git rm`，直接 overwrite 即可。

## 关键文件

- 新增 `macos/python_dist.txt`
- 重写 `macos/whisper-input.sh`
- 重写 `macos/setup_window.py`
- 改 `build.sh`（仅 macOS 分支）
- 不动：`macos/Info.plist`、`main.py`、`stt_sensevoice.py`、所有 `backends/`、Linux 全套

## 复用的现有锚点

- `stt_sensevoice.py` 的 `[sensevoice] 模型加载完成` 输出作为 stage C 完成信号
- `main.py` 的 `[main] 预加载 SenseVoice 模型` 输出作为 stage C 进入"加载中"标签的信号
- `config_manager.py` 已经把 macOS 的用户数据目录定在 `~/Library/Application Support/Whisper Input/`，user venv 放它下面正好

## 风险与开放问题

1. **TCC 归属**（最大不确定项）：详见上文 TCC 节。实测后看是否需要回退方案。
2. **python-build-standalone 体积**：install_only_stripped arm64 大约 30-40MB（解压后 100MB+）。.dmg 会变大但可接受。
3. **arm64-only**：用户在 Apple Silicon 上构建并发 arm64-only DMG，跟 bundle uv 的现状一致。Intel Mac 不支持。
4. **uv sync 镜像源**：`pyproject.toml` 已配清华源，stage A 在用户机器上跑 uv sync 时也会用这个源，速度 OK。
5. **stage C 子进程 detach 后日志去向**：setup_window 退出后，main.py 的 stdout/stderr 没有 parent 接收。需要让 stage C 起 main.py 时显式重定向 stdout/stderr 到 `~/Library/Logs/WhisperInput.log`。

## Verification

按这个顺序做，每一步 OK 再走下一步：

1. **python-build-standalone 下载 + tkinter 起得来**：build.sh 跑完，手动 `Whisper Input.app/Contents/Resources/python/bin/python3 -c "import tkinter; tkinter.Tk()"` 看 GUI 弹出。
2. **setup_window 三阶段冷启动**：删掉 `~/Library/Application Support/Whisper Input/`，删掉 `~/.cache/modelscope`，重装 .dmg，双击 → 看到 stage A → B → C 全部走完，主程序进入托盘。
3. **TCC PoC**：触发麦克风访问 + 触发辅助功能授权（按热键），看系统弹窗的 app 名字是否是 "Whisper Input"，看 系统设置 → 隐私与安全性 列表里是否显示 "Whisper Input"。**这一步定性决定走不走兜底方案**。
4. **三阶段热启动**：再次双击，应只走 stage C，加载完后 ~1s 内窗口关闭。
5. **main.py 解耦**：stage C 完成后 `pgrep -f main.py` 应仍能找到 main 进程，托盘图标在，热键能触发录音。
6. **升级路径**：手动改 `pyproject.toml` 增减一个无关依赖、重 build .dmg、覆盖安装、双击 → stage A 应检测到 hash 变化重跑 sync，stage B/C 跳过。
7. **日志完整性**：`~/Library/Logs/WhisperInput.log` 应包含三阶段 + main 运行后的日志。

## 不在本次范围

- Linux 构建链路
- 代码签名 / 公证
- universal binary
- 自动更新
- Stage B 失败时的网络重试 / 镜像切换（先做最朴素的"失败显示错误"）
