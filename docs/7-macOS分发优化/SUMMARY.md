# macOS 分发优化 — 开发总结

## 开发项背景

macOS 版本之前的体验问题：

1. **首启要求用户预装 uv**：尽管 .app 已经 bundle 了 uv 二进制，但启动链路是 `shell → uv run --python 3.12 setup_window.py`，uv 会在首启时去网上下 cpython。双击 .app 后卡住下载对非技术用户是反人类体验。
2. **三件耗时动作没有统一的进度反馈**：首启时的 `uv sync`（约 800MB 依赖）、SenseVoice 模型下载（约 900MB）、以及每次启动都要做的模型加载（几秒到几十秒）。上一版的 setup_window 只能展示前两件事的进度，模型加载阶段被排除在窗口之外，用户感知很差。
3. **权限归属错乱**：macOS TCC（Transparency, Consent, and Control）把运行中的 python 进程按文件名归属为 "python3"，不挂到 Whisper Input.app 上；辅助功能/麦克风/输入监控列表里只看到 "python3" 这一项、没有 "Whisper Input"，用户无法给它授权，陷入"不断被弹权限窗 → 无法授权"的死循环。

## 实现方案

### 关键设计

1. **Bundle 完整的 Python 运行时进 .app**：抛弃"让 uv 在首启时下 Python"这条路，改为在构建时用 `curl` 拉 python-build-standalone 的 install_only_stripped 变体（arm64，约 17MB 压缩、50MB 解压）并整棵复制进 .app。用户首启不联网、不等 Python。同时 python-build-standalone 的 install_only 变体自带 Tk，**彻底删除了上一版的 `TCL_LIBRARY` hack**。

2. **嵌套 .app 解 TCC 归属**：在主 .app 内部多塞一个 `Contents/Resources/Whisper Input.app/`（同名，路径不同），里面是 `Contents/MacOS/whisper-input`（python 二进制改名）+ 完整的 Info.plist（`CFBundleName=Whisper Input`、独立 bundle id `com.whisper-input.runtime`、`CFBundleIconFile` 指向 AppIcon）+ `Contents/lib` 符号链接回 `Resources/python/lib` 让 dyld 通过 `@executable_path/../lib` 找到 libpython。这样 macOS 把 python 进程识别为 "whisper-input" 并归属到内嵌的 "Whisper Input" bundle 上，dock 图标、cmd-tab 名称、TCC 列表都显示 "Whisper Input"。

3. **单窗口贯穿三阶段（A 装依赖 / B 下模型 / C 加载模型）**：一个 tkinter 进程、一个 `tk.Tk()` 生命周期、一个进度条、一片滚动 log，三个阶段的标题/描述/进度条模式随阶段切换。Stage A/B 通过 pty 流式读取子进程输出并实时更新 UI；Stage C 把 main.py 当 subprocess 起（`start_new_session=True` 让它自成进程组），用 `os.open(LOG_FILE, O_APPEND)` 把 stdout/stderr 直接写到日志文件，setup_window 在工作线程里 tail 日志文件直到看到 `[sensevoice] 模型加载完成` 这个锚点，触发窗口关闭。setup_window 退出后 main.py 继续在托盘里运行。

4. **所有 tk UI 调用走主线程 queue**：整个开发过程中最隐蔽的 bug。tk 9.0 在 worker 线程里调 `self.root.after(0, ...)` 会被静默吞掉，表现为"窗口空白、主程序却已经成功跑起来"。修法是让 worker 线程只往 `queue.Queue` 里 `put((fn, args, kwargs))`，主线程每 50ms 轮询一次并在本线程执行。

### 开发内容概括

- **新增 [macos/python_dist.txt](../../macos/python_dist.txt)**：锁定 python-build-standalone 的 release tag (20260408)、Python 版本 (3.12.13)、下载 URL 和 sha256。构建时 curl 按这份元信息下载 + 校验 + 缓存 + 解压。升级 Python 只需改这个文件。
- **重写 [macos/whisper-input.sh](../../macos/whisper-input.sh)**：从上一版 120 行的"复制 python、造 helper.app、链 dylib、注册 Launch Services"压缩成 25 行的 trampoline，只做三件事：建日志 tee、注入 `WHISPER_INPUT_APP_DIR` 环境变量、exec 嵌套 .app 里的 python 二进制跑 setup_window.py。
- **重写 [macos/setup_window.py](../../macos/setup_window.py)**（~550 行）：stdlib 全栈 GUI，单 tk 窗口、三阶段状态机、queue 化的线程安全 UI 更新、pty 流式读取与行解析、tail 日志锚点检测、用 pyproject.toml + uv.lock 的联合 sha256 sentinel 决定是否跳过 stage A。
- **改 [build.sh](../../build.sh) 的 `build_macos()` 流程**：从 4 步变 5 步，加 python-build-standalone 的下载/校验/缓存/解压步骤；组装阶段多出「构造嵌套 Whisper Input.app」一节，包含 Info.plist 模板、图标拷贝、lib symlink。
- **版本号 0.2.2 → 0.3.0**。

### 额外产物

- [PROMPT.md](PROMPT.md) — 需求文档
- [PLAN.md](PLAN.md) — 实施计划（最终 bundle-python 版本，不是中间讨论过的 PyApp 版本）

### 讨论过但被放弃的中间方案

- **PyApp（Rust 启动器）**：初版 PLAN 写的是用 PyApp。研究后发现 PyApp 的核心价值是"PyPI 项目 + 自更新 + 跨平台"，这三条对我们都不适用；它能帮我们管理 python-build-standalone 下载 URL 这一条，代价却是整套 Rust 工具链（cargo、约 500MB、首次编译 5 分钟）。最终裸 curl + shasum 够用了。
- **TCL_LIBRARY / TK_LIBRARY 环境变量 hack**：上一版为了让 uv 拉的 cpython 加载 tkinter 专门设的。新方案下 python-build-standalone 自带 Tk，不需要。
- **运行时在 user data 目录动态构造 helper.app**：上一版做法，复杂且脆弱。新方案下嵌套的 "Whisper Input.app" 是构建时产物，放在主 .app 的 Resources 里，随 .app 一起分发。

## 局限性

1. **单 arch (arm64)**：python-build-standalone、bundle 的 uv、编出的 .dmg 都是 Apple Silicon 专用。Intel Mac 不支持。要做 universal binary 需要拉两份 python-build-standalone 然后 lipo 合并，不在本次范围。
2. **未代码签名 / 未公证**：仍然是 ad-hoc 分发，第一次运行系统会弹 Gatekeeper 警告。要过 notarization 需要 Apple Developer 账号和一整套签名流程，本次没做。
3. **Stage A 的进度条是不定态转圈**：`uv sync --no-progress` 给的是"阶段性事件"（Resolved / Downloading / Built / Installed / + pkgname）而不是精确的 N/M 进度。窗口用这些事件切换 status 文案，但进度条不是确定态百分比。能接受。
4. **Stage B 的进度依赖 modelscope tqdm 输出格式**：解析的是 `Downloading [xxx]: 42%` 这样的行。如果 modelscope 换输出格式，正则会失效、进度变成"空转"但不影响功能。
5. **Stage A 检测"是否需要重跑"用 pyproject.toml + uv.lock 的联合 sha256**：简单可靠。但如果用户手动删了 user venv 里某个包，我们检测不到、不会重跑 stage A。需要手动删整个 `~/Library/Application Support/Whisper Input/.venv` 触发重装。
6. **TCC 归属的老化测试未覆盖**：本机验证了 dock 图标、cmd-tab 名称、TCC 列表显示 "Whisper Input"，但升级、重装、权限跨版本持久化等场景还没做完整回归。
7. **Stage C 的 main.py subprocess 日志管道**：main.py 的 stdout/stderr 被直接 redirect 到 `~/Library/Logs/WhisperInput.log`。setup_window 退出后 main.py 不会被 SIGPIPE，但如果日志文件在运行期间被手动删掉或 rotate，main.py 的写入会继续指向已解除链接的 inode，直到进程退出。对 Whisper Input 这种场景可忽略。

## 后续 TODO

1. **代码签名 + 公证 + DMG 美化**：做正式分发的必经路径。
2. **Universal binary**：给 Intel Mac 用户也能跑。
3. **Stage B 的失败 / 网络重试 / 镜像切换**：当前下载失败就报错停下，没有自动重试或切 HF mirror。
4. **setup_window 与 main.py 的 IPC 回收通道**：当前 setup_window 退出后 main.py 继续跑，两边没有任何联系。如果想做"用户点 setup_window 的取消按钮 → 连带杀 main.py"这种动作，需要加一条回收通道。
5. **考虑 Stage A 解析 uv 的 TTY 输出得到精确百分比**：当前 `--no-progress` 只能给阶段事件。解析 TTY 模式下的进度条能得到更精确的信息，但 trade-off 是跟 uv 输出格式耦合、脆。
6. **pystray + tkinter 在同进程共存的可能性**：目前 setup_window 和 main.py 是两个进程，多一层。如果 pystray 能在非主线程、或 tkinter 能主动把控制权交给 pystray 的 NSApplication，可以省一个进程。不紧急。
7. **升级路径的自动化回归**：手动验证过 stage A 的 pyproject hash 检测，但可以写一个脚本模拟"装 v0.3.0 → 换 v0.4.0 .dmg → 重启"的全流程。
