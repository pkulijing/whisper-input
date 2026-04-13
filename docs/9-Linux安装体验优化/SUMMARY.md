# Linux 安装体验优化 — 开发总结

## 开发项背景

`8-GitHub CI与版本管理` 落地后，回头审视 `debian/postinst` 发现当前 Linux 安装体验有严重设计问题：

1. **postinst 里 curl 装 uv 是反模式**：`sudo apt install` 触发无签名脚本执行，违反发行版 policy，对安全意识强的用户不友好。
2. **postinst 跑 `uv sync` + 预下载模型**：约 2.5GB 下载量塞进 apt 安装链，`apt install` 要卡 5-10 分钟，违反 deb 语境下"秒级完成"的预期。
3. **postinst 失败会让 apt 进入"已解压但配置失败"半装状态**，普通用户恢复困难。
4. **GUI 前端装包时 postinst 的 stdout 不可见**，用户以为卡死，实际在后台下 torch。
5. 首次启动也没有统一的进度反馈——用户只能盯着 notify-send 和终端。

同时 macOS 分支在 `7-macOS分发优化` 已经搭好了一套**单窗口三阶段**的首启引导（stage A 装依赖 / stage B 下模型 / stage C 加载模型到内存，全程一个 tkinter 窗口一条滚动日志），Linux 这边一直没对齐。

本轮目标合并成一件事：**把 apt install 变成秒级文件分发动作 + 把所有长时间操作搬到首启一个统一的 tkinter 窗口里展示进度，对齐 macOS 体验**。

## 实现方案

### 关键设计

1. **所有重量级动作全部移出 postinst**，postinst 只保留加 input 组 / 刷图标缓存 / 刷 desktop-database / 友好提示。`apt install` 现在是纯文件复制，秒级完成，彻底避免半装状态。

2. **uv 由"postinst 自动装"变为"硬前置要求"**。postinst 不碰 uv、不硬 fail；trampoline 里 `command -v uv` 硬检查，缺失则 `notify-send -u critical` + exit。这是主动 trade-off：Linux 用户大概率是开发者，预装 uv 不是负担，换来的是安装链路干净、错误反馈对 GUI 前端可见。

3. **setup_window 跑在 uv 管的 python-build-standalone 上，不吃 apt 的 `python3-tk`**。项目的一贯原则是 python 由 uv 管、不依赖发行版绑定的固定版本。`debian/python_dist.txt` 里锁死 `PYTHON_VERSION=3.12.13`（与 `macos/python_dist.txt` 对齐），trampoline 用 `uv python install "$PYTHON_VERSION"` + `uv python find` 定位 python，兜底靠 install_only 变体自带的 tkinter。

4. **对齐 macOS 的三阶段单窗口结构**，完全照抄 `macos/setup_window.py` 的 SetupWindow 类 / UI queue / pty worker / stage 状态机。差异只在路径常量（XDG 目录、`/opt/whisper-input`）和 uv 定位方式（系统 PATH 而非 bundled）。三个 stage 的文案、进度条逻辑、错误屏、退出时 main.py 的 `start_new_session` detach——都和 mac 一致。

5. **Linux 独有的 stage 0**：因为 tkinter 窗口本身需要 python 先存在才能打开，python-build-standalone 的下载**不能**塞进窗口三阶段里，只能作为 "stage 0" 发生在 trampoline shell 阶段。反馈形式是一条 `notify-send`"正在准备 Python 运行环境..."，下载 ~30MB，约 10-30 秒。

6. **Stage A 的 uv sync 显式传 `--python 3.12.13`**，强制用户 venv 和 setup_window 使用同一份 python-build-standalone，避免两者漂到不同的 3.12.x patch 版本导致 stdlib 行为差异。

7. **修复 debian/control 版本号硬编码 bug**：原先 `Version: 0.1.0` 是死的，build.sh 的 deb 文件名却用 `pyproject.toml` 的 `$VERSION`，导致安装后 `dpkg -l whisper-input` 显示的版本与文件名不一致、apt 的升级/回滚逻辑会误判。改为 `VERSION_PLACEHOLDER` 占位符 + build.sh 构建期 sed 替换，和 mac 的 `Info.plist` 手法对称。

### 开发内容概括

- 新增 `debian/setup_window.py`（~500 行 stdlib-only）：基于 `macos/setup_window.py` 改造，Linux 化路径常量 / uv 定位 / uv sync 参数。保留 SetupWindow 类、UI queue、pty+worker 模式、stage 状态机、错误屏、`start_new_session` detach 逻辑。
- 新增 `debian/python_dist.txt`：锁定 `PYTHON_VERSION=3.12.13`，与 mac 对齐。shell-source 友好格式。
- 重写 `debian/whisper-input.sh` trampoline（~60 行）：日志到 XDG state / 设 PATH / 检查 input 组 / 硬检查 uv / stage 0 准备 python-build-standalone / exec setup_window.py。
- 精简 `debian/postinst`：删掉 curl 装 uv（原 L14-39）、删掉 uv sync（原 L41-64）、删掉模型预下载（原 L66-96）。保留加 input 组、刷图标缓存、刷 desktop-database。完成提示文案升级为四点——input 组生效方式 / 需预装 uv / 首启四阶段流程 / 从应用菜单启动。
- 更新 `debian/control`：
  - `Version:` 改成 `VERSION_PLACEHOLDER` 占位符（配合 build.sh 修复版本号 bug）
  - `Depends:` 去掉 `python3 (>= 3.12)`（不再依赖系统 python），加 `libnotify-bin`（trampoline 的 notify-send）
- 更新 `build.sh` Linux 分支：
  - `cp debian/control` 改为 `sed "s/VERSION_PLACEHOLDER/${VERSION}/g" debian/control > ...`
  - 新增 `cp debian/setup_window.py debian/python_dist.txt` 到 `/opt/whisper-input/` 根下
- 更新 `README.md`：Linux 系统要求明确 uv 为必装前置 + 给出一行 curl 命令；"DEB 安装包"段描述首启四阶段流程；`bash build_deb.sh` → `bash build.sh` 修正文件名。
- 更新 `CLAUDE.md`：`stt_sensevoice.py` 的描述从 "lazy model loading" 改为 "startup preload via `preload_model()` (default; `--no-preload` to skip)"，对齐本轮"统一窗口里展示模型加载"的实际行为。

### 额外产物

- 顺手修了 `debian/control` 的 `Version: 0.1.0` 硬编码 bug——这不是 PROMPT 里要求的，但在本轮 review debian/control 时发现，用户判定为严重 bug 要求立刻修。
- `build.sh` 的 sed 替换手法对称了 mac 的 Info.plist 处理，两边版本注入逻辑统一。
- `linux/` 目录一度被创建又合并回 `debian/`——实施中段用户指出 `debian/` 已经是 Linux 唯一平台目录，新开 `linux/` 是冗余。最终结论记录在 [PLAN.md](PLAN.md) 顶部的调整说明。
- `.python-version` 从 `3.12` 锁死到 `3.12.13`——和 `debian/python_dist.txt` / `macos/python_dist.txt` 对齐，dev 环境和 deb / .app 共用同一份 python-build-standalone，避免 uv cache 因 python patch 版本漂移导致首装时重下 wheel。
- `pyproject.toml` 的 `requires-python` 从 `>=3.12` 收紧到 `>=3.12.13`——让 uv 在 resolve / sync 时强制使用锁定版本，和 `.python-version` / `python_dist.txt` 形成三点互锁。
- `pyproject.toml` 的 `version` 从 `0.3.0` bump 到 `0.3.1`——让 CI 自动把本轮 feat + 一系列 bug fix 一起发到 `v0.3.1` release。

### 冒烟测试中暴露并修复的遗留 bug（非本轮引入，但本轮顺带修掉）

装完 deb、首次跑 setup_window 的过程中陆续暴露了一些历史遗留 bug，都不是 round 9 改出来的，但**如果不修 round 9 的交付就是半残的**，因此一并修掉：

1. **[stt_sensevoice.py](../../stt_sensevoice.py) — SenseVoice 加载报 `No module named 'model'`**
   - 根因：代码里传了 `trust_remote_code=True`，funasr 走 `import_module_from_path("./model.py")` 的路径，该函数内部 `sys.path.append("."); import_module("model")` 从**进程 cwd** 找 `model.py`，deb 装好后 cwd 是 `/opt/whisper-input` 根本没这文件
   - 正确做法：去掉 `trust_remote_code` 和 `remote_code`。funasr 包自己 `__init__.py` 里的 `import_submodules(__name__)` 递归导入时会触发 `funasr/models/sense_voice/model.py` 里的 `@tables.register("model_classes", "SenseVoiceSmall")` 装饰器，SenseVoiceSmall 类自动进全局 tables，AutoModel 按 config.yaml 里的 `model: SenseVoiceSmall` 字符串直接查表即可
   - 这个 bug 在历史上**反复出现过两次**：`c35ce78` 初版就有正确的 remote_code 值，`ca4b139` 在 ruff 清理时误删，后续又有人错加 `remote_code="./model.py"` 想修复但 cwd 路径还是错的。本次在代码里加了**三段防回归注释**说明为什么 `trust_remote_code` 这条路是错的，防止第三次重犯

2. **[main.py](../../main.py) — Linux 托盘"退出"菜单卡死**
   - 根因：pystray 的 quit 回调跑在 **daemon 线程**里，回调里 `sys.exit(0)` 只杀了当前线程不影响主线程；而主线程一直阻塞在 `signal.pause()` 里等信号，永远不会醒——用户必须 kill -9
   - 修法：引入 `threading.Event`，`shutdown()` 从任意线程 `set()`；主线程换成 `_shutdown_event.wait() + sys.exit(0)`，任意线程触发 shutdown 都能唤醒主线程正常退出
   - macOS 路径（tray_icon.run() 主线程阻塞 AppKit）行为不变

3. **[debian/setup_window.py](../../debian/setup_window.py) — HiDPI 屏字体模糊缩水**
   - 根因 1：`tk scaling` 默认按 X server 报告的 DPI 推算，4K 屏上 X server 经常给出离谱的物理尺寸（1806 mm = 1.8 米宽），tk 反推出 DPI 低于 72，结果**主动缩小** widget（本机实测 scale = 0.75）
   - 根因 2：字体写 `("Helvetica", ...)`——Linux 上 Helvetica 不存在，fontconfig fallback 链对中文小字号 hinting 效果差，字模糊
   - 修法：
     - 不信 `winfo_fpixels`，按屏幕像素分档（4K → 2.0 / 2K → 1.5 / 其他 → 1.0），并支持 `WHISPER_INPUT_UI_SCALE` / `GDK_SCALE` 环境变量覆盖
     - 无条件 `self.root.tk.call("tk", "scaling", scale)` 覆盖 tk 的默认推算，避免被 X 的垃圾 DPI 拖下水
     - 字体改用 `TkDefaultFont` / `TkFixedFont` 命名族（GNOME 下默认 Noto Sans，天生带中文 fallback）
     - Progressbar 的 `length`（像素单位）手动乘 scale（tk scaling 不管硬像素）

4. **[backends/input_linux.py](../../backends/input_linux.py) — 粘贴在终端、IDE 内嵌终端、插件输入框三种场景下互不兼容**
   - 现象矩阵：
     - `Ctrl+V` 在 VS Code editor / Claude Code 插件输入框 / 浏览器 / GEdit / 独立终端 bash 里：前三个 ✓，独立终端 ✗（bash 的 `^V` 是 quoted-insert）
     - `Ctrl+Shift+V` 在 VS Code editor (pasteAs) / VS Code 内嵌 terminal / 独立终端 ✓，Claude Code 插件输入框（webview 自定义 keymap）✗
   - 没有任何单一 Ctrl 快捷键能覆盖所有场景；双发又会在 VS Code editor 里双贴
   - **终局方案**：改用 **X11 PRIMARY + CLIPBOARD 双写 + Shift+Insert** 粘贴。原理：
     - 把文本**同时**写 X11 的 CLIPBOARD 和 PRIMARY 两套 selection
     - 发 `Shift+Insert` ——这是 X11 文本控件粘贴 PRIMARY 的标准快捷键，几乎所有 GTK/Qt/Chromium 控件都支持
     - VS Code Monaco 内部把 Shift+Insert 映射到 `editor.action.clipboardPasteAction`（读 CLIPBOARD），内嵌 xterm.js 粘贴 PRIMARY，Chromium 插件 webview 粘贴 PRIMARY，独立 gnome-terminal 粘贴 CLIPBOARD——两边都有同一份文本，全部命中
   - 一个快捷键打通所有目标，**不再需要 WM_CLASS 识别/双发/IME 管理**
   - 缺点：PRIMARY 会被覆盖且无法恢复（用户之前鼠标选的内容被丢）；密码框由于安全策略会拒绝 PRIMARY paste；某些重度自定义的富文本 Web 编辑器可能不吃 Shift+Insert（未来踩到再为对应 WM_CLASS 加 Ctrl+V fallback）

## 局限性

1. **uv 成为必装前置**，对非开发者不友好。文档有明确指引但不会帮用户装。这是主动 trade-off。
2. **stage 0 没有进度条**，只有一条起始 notify。30MB 在国内网络慢时用户体验不佳（看不到下载进度，以为卡死）。本轮不兜底，如需进度条要引 zenity 依赖或再开一个 shell-level GUI。
3. **tkinter 依赖 python-build-standalone 的 install_only 变体自带 Tk**。如果 astral 未来发的变体不再带 tk，本方案会静默 `import tkinter` 失败——需要升级时盯一眼。
4. **deb 未签名**，不走 apt secure 渠道。用户需手动 `sudo apt install ./xxx.deb`。
5. **仅 apt 系（Ubuntu/Debian）**，不支持 yum / pacman / zypper。
6. **NVIDIA 依赖库下载很慢**（实测反馈）：Stage A 的 `uv sync` 在用户侧跑，国内镜像源（tuna + sjtu）已配但 torch+CUDA 依赖 wheel 大、链路长。本轮无法优化，属于 uv sync 本身的网络问题。
7. **config.yaml 放在源码树里**：实施时注意到 `/opt/whisper-input/` 里可能混进开发机的 `config.yaml`（build.sh 的 `SOURCE_OTHER` 列表里没有它，但若本地有 `.python-version` 之类副作用文件也会被打进去）。本轮没影响但值得后续留意。

## 后续 TODO

1. **Linux 安装包 smoke test 自动化**：CI 里目前只测 `dpkg -i` 是否成功，没触发 trampoline → setup_window 全链路。可以加一个 xvfb + fake uv + fake main.py 的 headless 测试，让 CI 能发现 setup_window 的 regression。
2. **stage 0 的进度反馈**：如果 `uv python install` 在弱网环境下卡超过 30 秒，加一个兜底——比如 trampoline 先 `zenity --info` 弹一个可关闭的提示，或者在 while 循环里周期性 notify-send。前者要引 zenity 依赖。
3. **Wayland 支持**：当前 xdotool + xclip 链路只支持 X11，Wayland 上彻底不工作。需要新的 input backend（可能是 wtype / wl-clipboard / libei），本轮不涉及。
4. **模型下载镜像**：Stage B 用 modelscope 的 `snapshot_download`，国内从 modelscope 拉一般还行，但没兜底。失败后只显示错误屏、不自动重试或切镜像。
5. **uv sync 失败后的 venv 重建**：目前失败只写错误屏，下次启动会重跑 stage A。但如果 venv 被 stage A 半创建（uv 进程中途被 kill），`deps_up_to_date()` 可能误判已就绪。加一个显式的"半状态检测" sentinel 会更稳。
6. **`/opt/whisper-input/` 下的源码权限**：当前 build.sh 把源码文件 chmod 644，但没设 owner。根据 deb policy 应该是 root:root。如果 fakeroot 没介入，本机构建出来的 deb 是 jing:jing。CI 里跑通过可能是因为 GitHub Actions runner 是 root。需要验证 CI 产物的 owner 字段。
7. **冒烟测试尚未跑完**（本轮交付时 NVIDIA 依赖还在下载中）：完整的 stage A→B→C 链路、错误屏路径、升级路径（改 pyproject 重装）都没走完。装完后如发现 regression 再起追加轮。
