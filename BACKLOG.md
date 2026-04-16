# Whisper Input — Backlog

未来开发项清单。**本文件是权威来源**，取代各轮 `docs/N-*/SUMMARY.md` 里 "后续 TODO" 段的跨轮追踪职责 —— 那些段落继续保留，但只记录当轮发现的新线索，发现的新想法要立刻同步到这里。

**工作流**：

- **开新轮**时从下面的条目里挑一个作为 `docs/N-*/PROMPT.md` 的起点
- **收尾一轮**时从本文件**删掉**已完成的条目（不是打勾，是整条删，避免腐烂）
- **发现新想法**时立刻加进来，哪怕只写一行占位，之后再补完整

条目没有固定优先级 —— 选哪个做下一个看当时的心情和痛点。每条都写成"未来自己或后续 agent 读完能接得住"的格式：**动机 / 目标状态 / 候选方向 / 风险 / scope**。

---

## 分发 & 安装体验

### 一键安装脚本（`curl | bash` 风格）

**动机**：14 轮走了 PyPI 标准路线，`uv tool install whisper-input` 对技术用户很自然，但这条命令本身要求用户先会装 `uv`（或 `pipx`）。对"会用 terminal 但没碰过 Python 生态"的用户门槛还是太高。

理想状态像 [uv](https://docs.astral.sh/uv/getting-started/installation/) / [oh-my-zsh](https://ohmyz.sh/) / [starship](https://starship.rs/) / [rustup](https://rustup.rs/) 那种 —— **一条命令装完所有东西**：

```bash
curl -LsSf https://whisper-input.example/install.sh | sh
```

脚本负责的事：

1. 检测平台 (macOS / Linux) 和架构 (arm64 / x86_64)
2. 检测并装好 Python 3.12（如果没有，通过 uv 的 `python install` 子命令）
3. 装 `uv`（如果没有）
4. 装系统依赖
   - macOS: `brew install portaudio`
   - Linux: `apt install xdotool xclip pulseaudio-utils libportaudio2 libgirepository-2.0-dev libcairo2-dev gir1.2-gtk-3.0`（或对应 dnf / pacman）
5. Linux 引导 `usermod -aG input $USER`（交互确认）
6. 跑 `uv tool install whisper-input`
7. 打印"装完了，跑 `whisper-input` 启动"

**脚本 hosting**：

- **方案 A**：GitHub Raw (`raw.githubusercontent.com/pkulijing/whisper-input/master/install.sh`) —— 零成本、最简单，curl 命令略长
- **方案 B**：GitHub Pages + 自定义短链 —— 需要配置 CNAME，但用户可以 `curl -LsSf get.whisper-input.dev | sh` 这种更酷的 URL
- **方案 C**：Release asset，tag 时 upload `install.sh`，curl 指向 `releases/latest/download/install.sh` —— 可以把 install 脚本和应用版本绑定

**非目标**：**不要再走 DEB / DMG / AppImage**。14 轮已经决定放弃 native bundle 路线。一键脚本是"懒人的 DMG"，不是 DMG 的替代 —— 它本质上仍然跑 `uv tool install`，只是帮用户把前置条件准备好了。

**风险**：

- bash 脚本在各种发行版 / macOS 版本上的兼容性不好搞 —— 测试矩阵大
- 装系统依赖要 sudo，脚本里调 sudo 会让安全意识强的用户警觉（行业惯例，但要在 README 里解释清楚）
- 检测用户已经装过 uv / python 的各种 corner case

**scope**：中。脚本本身 150-300 行 bash，但**测试矩阵（Ubuntu 24.04 / Debian 13 / macOS 12/13/14）才是大头**。

---

### macOS 权限问题的优雅解决（探索型）

**问题**：PyPI 路线下，`uv tool install` / `pipx install` 出来的 `whisper-input` 实际进程是 `~/.local/share/uv/tools/whisper-input/bin/python`，macOS 系统授权对话框弹出的是这个 Python 二进制，而不是 "Whisper Input"。

**具体痛点**：

1. 对话框文案是 "python wants to access ..."，非技术用户迷惑（"我没装 python 啊？"）
2. 图标是默认 Python 图标，没有品牌感
3. `Info.plist` 里我们写的 `NSMicrophoneUsageDescription` 中文文案不生效（PyPI 装不经过 bundle）
4. `uv tool upgrade` 如果换了 python 小版本，授权可能失效，用户看不出为什么

**目标状态**：用户装完后首次运行时：

- 授权对话框清楚标识是 "Whisper Input" 在请求权限
- 对话框文案是我们写的中文描述
- 图标是我们的 logo
- 授权一次后，无论是 `uv tool upgrade` 还是重装，都不需要重新授权

**这条刻意不定方案**。可能的方向有很多（thin `.app` wrapper 像 Karabiner 那样、Homebrew Cask 走 cask 路径、Apple Developer ID + 签名公证流程、甚至重新考虑某种形式的轻量 bundle），每条路都有自己的代价和限制。真正做这条时应该先做一轮完整的探索：调研 Karabiner / Rectangle / AltTab 这些工具是怎么解决同一问题的，对比各自的 UX / 开发成本 / 维护成本，再定方向。

**scope**：大。和 14 轮决定的"PyPI only"路线最可能产生张力，启动前应先明确这条能不能兼容 PyPI 路径。可能需要 Apple Developer 账号（$99/年）+ 公证流程。

---

## Bug

### 后处理误追加情感 emoji（😔）

**现象**：识别正常语音后，文本末尾经常多出一个 😔（不开心/郁闷），实际说话内容和情绪完全无关。

**原因**：SenseVoice 输出的 meta 标签里包含情感标签（`<|SAD|>` / `<|HAPPY|>` / `<|NEUTRAL|>` 等），`stt/_postprocess.py` 的 `format_str_v2` 会把出现次数最多的情感标签渲染成 emoji 追加到文本末尾（第 111 行 `s = s + emo_dict[emo]`）。模型的情感检测不可靠，经常把正常语音误判为 `<|SAD|>`，导致每次都多出 😔。

**目标状态**：作为输入法工具，用户要的是干净的文本，不应该自动追加情感 emoji。

**候选方向**：

- **最简单：直接把 `emo_dict` 里所有情感都映射为空串** —— 和 `<|NEUTRAL|>` 一样处理，彻底不渲染情感 emoji。对一个输入工具来说这是最合理的默认行为
- **可选：加一个配置项控制是否渲染情感 emoji**，默认关闭

**scope**：小。改 `_postprocess.py` 里 `emo_dict` 的值即可，几行代码 + 更新测试。

---

## 识别能力

### 中英混杂 / 专业词汇的识别后处理

**动机**：SenseVoice 对通用中英文识别质量很好，但**中英混杂的专业名词 / 技术术语经常识别不出来**。对我们的用户画像（技术工作者）这是日常痛点。常见的翻车案例：

- `kubernetes` → "苦不乐他死" / "库伯尼茨"
- `tkinter` → "ticket"
- `onnxruntime` → "ONNX run time" / "ONNX 轮 time"
- `TypeScript` → "type 斯克瑞普特"
- 人名地名不在模型词表里的全部翻车

**希望达到**：用户能维护一个**个性化热词表**，识别阶段或后处理阶段用这个表去引导 / 纠正。硬约束：

1. **仍然本地运行** —— 不向云端发音频或文字
2. **速度基本不变** —— 松开热键后粘贴延迟 < 500ms overhead
3. **用户能自己增删词汇** —— 最好在 Web 设置页直接管理

**候选方向**（都没深入验证过，真做时要先做技术 spike）：

- **SenseVoice 原生 hot words 支持**：FunASR 文档里提到过 context biasing，要查原模型是否接受 hot words 参数 + 我们的 ONNX 量化版是否保留了这个输入。**如果支持，这是最干净的路**，只需要在 `transcribe()` 调用时多传一个参数
- **文本后处理层基于拼音 / 编辑距离的纠错**：对 CJK + 英文混杂的场景可能不好做，拼音匹配对英文专业词效果差
- **小型本地 LLM 兜底**：识别完交给本地 LLM（Qwen-0.5B / Phi-3-mini 这种 sub-GB 模型）做 "校正这段话的专业术语"。问题是延迟可能不可接受
- **用户字典 → post-processing regex 替换**：最简单的版本，让用户自己写 `"苦不乐他死" → "kubernetes"` 这种规则。代价是用户要手动加每一个词，但好处是透明可控

**风险**：

- SenseVoice 不支持原生 hot words 的话，其他方案质量都要打折
- 用户维护词汇表的 UX 设计要想清楚（Web 设置页？还是编辑 txt 文件？用户怎么知道哪些词该加）
- 热词表会不会随时间膨胀，影响推理速度

**scope**：中到大。关键看 SenseVoice 对 hot words 的支持程度。支持 → ~300 行 + 设置页加一个 textarea；不支持且决定走后处理管道 → scope 翻倍。**先花半天做 spike 确定技术路径再开轮**。

---

### 实时语音识别（streaming）

**动机**：当前是"按住热键说话 → 松开后一次性识别 → 粘贴"的 **batch** 模式。对长句子有明显延迟 —— 说完 5 秒话要等 1 秒才出文字。

理想状态是 streaming：

- 说话的同时文字已经开始出现（或每 500ms 刷新一次）
- 松开热键时延迟接近零（最后一段已经识别完了）
- 更接近系统输入法的"语音输入"体验

**技术面**：

- **SenseVoice-Small 原生支持 streaming 模式**，FunASR 仓库有 streaming decoder 示例代码。但我们目前用的是 ONNX 量化版 + 自己 port 的 decoder，streaming decoder 是否也能走 ONNX 需要验证
- 改动面很大：
  - `recorder.py` 从 "一次性读完再转 WAV" 改成 "chunk-by-chunk 流式（16kHz、320ms 窗口）"
  - `stt/sense_voice.py` 切成 streaming decoder，可能要再 port 更多 funasr 代码
  - `input_method.py` —— **这里是最难的地方**。当前剪贴板粘贴是 atomic 操作，一次 paste 一段。streaming 要么改成"每识别完一个完整短语就 append 一段粘贴"，要么想办法"先占位 → 识别完后原地 update"。后者在不同应用里行为各异，几乎不可能做到通用
- 剪贴板语义下更合理的 streaming 是**"按短语 flush"** 而不是 "逐字 flush"

**风险**：

- streaming 和当前"clipboard paste"哲学冲突，UX 设计要重新想
- SenseVoice streaming 精度是否 < batch 模式未知
- 这是**整个应用的交互模式变更**，不是替换单个模块。规模大、耦合深

**scope**：大。应该是"想清楚要改交互模式再开轮"的类型，不是当做一个小优化来做。

---

## 应用生命周期

### 设置页面的更新检查 + 更新触发

**动机**：14 轮发到 PyPI 之后，用户怎么知道有新版本？目前完全没机制：

- 被动路径：用户自己定期跑 `uv tool upgrade whisper-input`
- 主动路径：应用自己定期（启动时 / 每天一次）查 PyPI，有新版就在设置页弹横幅，用户点"更新"按钮自动触发 upgrade

问题是**大部分用户不知道 `uv tool upgrade` 命令存在**，被动路径等于没更新。

**技术点**：

- **查版本**：`curl https://pypi.org/pypi/whisper-input/json` 拿 `info.version`，和本地 `whisper_input.__version__` 比。简单、无 token、无限频次（PyPI 允许）
- **触发 upgrade**：设置页"更新到 v0.x.y"按钮 → 后端起子进程跑 `uv tool upgrade whisper-input`（或 pipx 对应命令）→ 完成后提示"请重启 whisper-input 应用新版本"
- **区分 uv vs pipx**：看 `sys.prefix` 路径 —— `/.local/share/uv/tools/` 是 uv，`/.local/pipx/venvs/` 是 pipx
- **dev 模式隔离**：从 source 跑的 `uv run whisper-input`（`__version__ == "dev"`）不应该弹更新横幅

**风险**：

- 检查路径上加网络请求会增加启动延迟 —— 必须放到后台线程 + 带 2 秒超时 + 支持设置里关掉
- 更新触发期间 whisper-input 自己在跑，`uv tool upgrade` 覆盖 venv 里的文件，subprocess 体验怎样不确定（可能需要 "更新 → 自动重启应用" 的流程）
- 用户点按钮但网络断了，或者 PyPI 临时挂了，错误提示要友好

**scope**：中。~200 行代码 + 两处 Web UI 改动（横幅 + 按钮）。

---

### 日志系统（目前完全没有）

**动机**：程序到现在为止根本没有日志 —— 代码里一行 `logging` 都没用，所有输出都是 `print()` 直接打到 stdout/stderr。手动从终端跑的时候消息还能看见，但是：

- **macOS 自启（LaunchAgent）**：[autostart_macos.py:33-58](src/whisper_input/backends/autostart_macos.py#L33-L58) 生成的 plist 没有 `StandardOutPath` / `StandardErrorPath`，launchd 默认把两个流都丢到 `/dev/null`。**自启模式下日志直接消失，出问题没法追**
- **Linux 自启（XDG .desktop）**：[whisper-input.desktop](src/whisper_input/assets/whisper-input.desktop) 的 `Exec=whisper-input` 也没重定向，输出去向取决于桌面环境（GNOME 进 `journalctl --user`，其它 DE 行为各异，也可能直接丢了）

对一个长期跑在后台的工具来说这是个明显的洞 —— 用户来报 bug 时我们唯一能让他做的是"请从终端手动跑一次再复现"，完全没有"查一下过去 24h 的日志"这个选项。

**目标状态**：

- 全局 `logging` 配置，替换掉现在所有的 `print()` 调用，按 level 分流（INFO 正常输出、DEBUG 开发态才有、WARNING/ERROR 始终记录）
- 日志文件写到平台约定目录：
  - macOS: `~/Library/Logs/whisper-input/whisper-input.log`（Apple 推荐位置，Console.app 会自动扫这里）
  - Linux: `$XDG_STATE_HOME/whisper-input/whisper-input.log`，兜底 `~/.local/state/whisper-input/whisper-input.log`（XDG 规范下 state 目录就是放日志的地方）
- `RotatingFileHandler`：单文件 1 MB，保留 3 轮，避免长期运行撑爆磁盘
- **LaunchAgent plist 把 `StandardErrorPath` 也指向同一个文件**，这样 launchd 自己 spawn 失败 / Python 崩溃前 traceback 也能被捕获（logging 配置还没起来的阶段）
- 设置页加一个"打开日志目录"按钮，点了直接 `open`（macOS）/ `xdg-open`（Linux）弹文件管理器

**候选方向**：

- **stdlib `logging` + `RotatingFileHandler`**：零新依赖，完全够用，首选
- **`loguru`**：API 更友好，彩色输出开箱即用，但引入一个新依赖，且项目规模小没必要
- **`structlog`**：结构化日志对后续做可观测性有帮助，但现在没到那个阶段

**风险**：

- 替换所有 `print()` 是 cross-cutting 改动，要一次做完避免两套并存
- LaunchAgent 的 `StandardErrorPath` 路径在 plist 生成时是写死的，用户如果改了 `$HOME` 或者自定义了日志位置会错位 —— 得想清楚 config 和 plist 的同步策略
- Linux 用户可能更习惯 `journalctl --user` 查日志，双写（文件 + systemd journal）要不要做

**scope**：中。替换 `print()` + 建 logger 模块 ~半天；改 plist 模板 + 设置页按钮 ~小半天；测试文件轮转 + 各种自启路径下日志都能落盘 ~小半天。

---

## 代码质量

### 跨平台 Pythonic overlay（部分完成）

**已完成（第 16 轮）**：视觉统一为微信输入法风格的深蓝药丸 + 居中麦克风 emoji + 两侧随音量跳动的白色长条。两个平台观感一致，代码从 ~190 行各降到 ~130 行。

**未完成**：代码仍然是双份（`overlay_linux.py` 用 GTK3+Cairo，`overlay_macos.py` 用 AppKit）。经过第 16 轮讨论，Tkinter 在 macOS 上无法与 pystray 共享主线程（两个 GUI 框架都要占主线程），子进程方案可解但引入新的退出清理复杂度。结论：**维持双份原生实现，视觉已对齐，统一代码不是当前优先级**。

**如果后续要统一**：最可行的方向是 Tauri 或类似的跨平台桌面框架全面接管 UI 层（含 tray + overlay + 设置页），但这是整个项目架构升级，不是 overlay 一个模块的事。

---

### 测试套增强（v2）

15 轮搭起了 pytest 框架（`tests/` 下 75 个用例覆盖纯逻辑层 + 带 mock 的边界层 + 端到端 STT 推理 + 默认开启的覆盖率报告 + codecov 上传 + README 徽章，总线覆盖 ~51%），但有几个明显能继续推进的方向。**先做不做都不影响项目正常运行**，列在这里是为了记住来路：

- **macOS CI runner 矩阵**：当前 `build.yml` 只跑 `ubuntu-24.04`。conftest 注入的 fake pynput / evdev 在真 darwin 上是否完全等价于真 pynput 还需要本地 macOS 跑一次确认。如果要彻底保险，加 `macos-latest` 进 matrix —— 代价是 macos runner 比 ubuntu 贵 10×
- **hotkey 测试升级**：当前测试直接调 `_on_hotkey_press` 等 internal 方法,所以 `hotkey_macos.py` / `hotkey_linux.py` 卡在 54% 覆盖率(`_listen_loop` / `start` / `stop` / `find_keyboard_devices` 都没测)。更接近真实路径的做法是通过 fake `Listener` / fake evdev 设备**注入合成键盘事件**，让 `_listen_loop` / pynput callback 自然驱动状态机。改造后能把覆盖率推到 80%+
- **STT 多语种 / 边角样本**：v1 只测一条中文(`tests/fixtures/zh.wav`)。`iic/SenseVoiceSmall/example/` 里还有 `en.mp3` / `ja.mp3` / `ko.mp3` / `yue.mp3` 几个官方示例,可以同样转换成 wav fixture,各加一个用例覆盖更多语种 prompt id 路径。也可以试一下噪声 / 长音频 / 多说话人这些边角场景

**scope**：每条都不大,小到一两个小时,大到半天。哪条优先看痛点 —— 如果某次 PR 因为没有 macOS CI 漏掉了一个 darwin-only 回归，就先做第一条；如果某次重构动到 hotkey 状态机想要更扎实的覆盖，就先做第二条。

---

## 已完成 / 不再追踪

这一段记录从早前 SUMMARY "后续 TODO" 里**刻意移除**的条目，避免未来自己或后续 agent 翻老 SUMMARY 发现"为什么这条没做"，误以为是遗漏：

- **首次模型下载进度 UI**（14 轮 SUMMARY 局限性 #3）—— 实测下载速度已经够快（ModelScope 国内 CDN 秒级），用户痛点不明显，不值得做
- **Linux 实机验证**（14 轮 SUMMARY 局限性 #4）—— 已在干净 Ubuntu 上手动验证通过
