# Whisper Input — Backlog

未来开发项清单。**本文件是权威来源**，取代各轮 `docs/N-*/SUMMARY.md` 里 "后续 TODO" 段的跨轮追踪职责 —— 那些段落继续保留，但只记录当轮发现的新线索，发现的新想法要立刻同步到这里。

**工作流**：

- **开新轮**时从下面的条目里挑一个作为 `docs/N-*/PROMPT.md` 的起点
- **收尾一轮**时从本文件**删掉**已完成的条目（不是打勾，是整条删，避免腐烂）
- **发现新想法**时立刻加进来，哪怕只写一行占位，之后再补完整

条目没有固定优先级 —— 选哪个做下一个看当时的心情和痛点。每条都写成"未来自己或后续 agent 读完能接得住"的格式：**动机 / 目标状态 / 候选方向 / 风险 / scope**。

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
- **跨平台 Pythonic overlay 统一代码**（16 轮遗留）—— 视觉已在 16 轮对齐（微信输入法风格深蓝药丸），双份原生实现（GTK3+Cairo / AppKit）维持现状。Tkinter 与 pystray 主线程冲突、子进程方案引入退出清理复杂度，真要统一得换 Tauri 这类方案全面接管 UI 层，不是 overlay 一个模块的事，当前版本满意，不再追
