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

## 代码质量

### 跨平台 Pythonic overlay

**动机**：当前 `overlay_linux.py` 用 GTK 3（拖 `libgirepository-2.0-dev` / `libcairo2-dev` / `gir1.2-gtk-3.0` 三个 apt 包 + 每次安装编译 pygobject / pycairo from source），`overlay_macos.py` 用 pyobjc + AppKit。两个平台两份代码，各几十行 + 各自的边角 bug 空间。

**理想状态**：一份代码 + 纯 Python 依赖（最好是 stdlib 的 `tkinter`），Linux 能砍掉三个 apt 包，两边代码合并成一个 `overlay.py`，删掉 `overlay_linux.py` / `overlay_macos.py` 和 `overlay.py` 的 dispatcher。

**候选技术**：

- **tkinter**：stdlib，几乎零新依赖。Canvas 支持画圆、`wm_attributes("-alpha", ...)` 支持透明、`-topmost` 支持置顶。对"录音时一个动画圆圈"的简单需求**大概率够用**。坑是 borderless + 透明 + 跟随 Retina / HiDPI 在三个平台上各有各的边角问题
- **PyQt6 / PySide6**：质感最好但太重（~60 MB wheel），违背 14 轮确立的"依赖干净"原则
- **dearpygui**：immediate-mode GUI、pythonic，但生态不主流，未来维护风险

**风险**：

- tkinter 画出来的半透明圆圈和原生 Cocoa / GTK 渲染质感有差距 —— 抗锯齿、阴影、渐变、动画帧率。尤其 **macOS 用户对这种细节敏感**
- 现有 overlay 用了平台特有的 always-on-top + click-through 能力，tkinter 是否都能对齐要验证

**正确做法**：先写一个 `overlay_tk.py` 原型，两边跑通后对比当前实现的观感 —— **质感能接受再合并**，不能接受就保留当前双实现，本条废止。不要做到一半强上。

**非目标**：不考虑"放弃 overlay，用托盘图标 pulse 代替" —— `config.overlay.enabled` 已经给用户提供了关闭选项，但很多人就是喜欢屏幕中央那个大圈的视觉反馈，overlay 本身是功能而不是装饰。

**scope**：中。原型 ~100 行，对比合并 ~半天，最后删老代码 + 清系统依赖清单 + 更新 CLAUDE.md 大概一天。

---

### 测试套 `tests/`

**动机**：从 0 轮到 14 轮项目始终没有自动化测试。每次重构靠"手动跑一遍看有没有炸"，14 轮这种大规模删代码尤其风险高 —— Phase 2 删 `stt/downloader.py` 时我靠 grep 确认没人 import，但如果有个 "曾经 import 过后来注释掉但 `.pyc` 还在" 的情况就会炸。有测试套就能自动挡住这种事。

**初版目标覆盖**（不追求完整，只覆盖低挂果）：

- **`config_manager.py`**：YAML 默认值合并、文件读写、修改持久化、`_find_project_root()` 的 dev / installed 模式切换
- **`stt/_postprocess.py`** 的 `rich_transcription_postprocess()`：纯字符串处理函数，已有 FunASR 官方的已知输入 / 输出对，最容易单测
- **`backends/autostart_macos.py` / `autostart_linux.py`**：`_build_plist()` / `_load_desktop_template()` 的输出应该是确定的字符串，assert 生成的文件内容
- **`stt/_tokenizer.py`** 可选：SentencePiece 处理纯函数，也能单测

**不覆盖的部分**（初版刻意跳过）：

- **STT 推理路径（`transcribe()`）**：需要真实模型文件 + numpy / onnxruntime / kaldi-native-fbank 运行。本地能跑，CI 上要考虑 ~231 MB 模型的 cache 策略
- **硬件 I/O**：麦克风、evdev、pynput 键盘事件
- **Web UI**：`settings_server.py` 的 HTTP handler 可以单测，但交互逻辑大多在 JS 里

**工具**：

- `pytest` + `pytest-mock`（标准选择）
- `[dependency-groups] dev` 里加进去
- `.github/workflows/build.yml` 的 lint job 改成 `lint + test`

**未定事项 / spike 要做的事**：

- CI runner 上 Linux 那边 evdev 需要 `/dev/input/` 权限 —— 跑 `autostart_linux.py` 测试时生成 `.desktop` 文件不需要 evdev，但如果未来想测 `hotkey_linux.py` 就要解决这个
- STT 推理测试是不是要放到一个单独的 optional test suite（`pytest -m slow`）

**scope**：中。初版 ~300 行测试代码 + `pyproject.toml` 加 dev dep + `build.yml` 加一步 `uv run pytest`。

---

## 已完成 / 不再追踪

这一段记录从早前 SUMMARY "后续 TODO" 里**刻意移除**的条目，避免未来自己或后续 agent 翻老 SUMMARY 发现"为什么这条没做"，误以为是遗漏：

- **首次模型下载进度 UI**（14 轮 SUMMARY 局限性 #3）—— 实测下载速度已经够快（ModelScope 国内 CDN 秒级），用户痛点不明显，不值得做
- **Linux 实机验证**（14 轮 SUMMARY 局限性 #4）—— 已在干净 Ubuntu 上手动验证通过
