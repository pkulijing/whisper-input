# Daobidao — Backlog

未来开发项清单。**本文件是权威来源**，取代各轮 `docs/N-*/SUMMARY.md` 里 "后续 TODO" 段的跨轮追踪职责 —— 那些段落继续保留，但只记录当轮发现的新线索，发现的新想法要立刻同步到这里。

**工作流**：

- **开新轮**时从下面的条目里挑一个作为 `docs/N-*/PROMPT.md` 的起点
- **收尾一轮**时从本文件**删掉**已完成的条目（不是打勾，是整条删，避免腐烂）
- **发现新想法**时立刻加进来，哪怕只写一行占位，之后再补完整

条目没有固定优先级 —— 选哪个做下一个看当时的心情和痛点。每条都写成"未来自己或后续 agent 读完能接得住"的格式：**动机 / 目标状态 / 候选方向 / 风险 / scope**。

## 目录

- [识别能力](#识别能力)
  - [中英混杂 / 专业词汇的识别后处理](#中英混杂--专业词汇的识别后处理)
  - [流式识别长音频滑动窗口](#流式识别长音频滑动窗口)
  - [流式 preview 浮窗显示 pending](#流式-preview-浮窗显示-pending)
- [设置页体验](#设置页体验)
  - [STT 模型按需可视化下载 + 已下载状态感知](#stt-模型按需可视化下载--已下载状态感知)
- [应用生命周期](#应用生命周期)
  - [启动时检测并清理已有实例](#启动时检测并清理已有实例)
- [代码质量](#代码质量)
  - [测试套增强（v2）](#测试套增强v2)
  - [并发模型迁移到 asyncio](#并发模型迁移到-asyncio)
- [启动性能](#启动性能)
  - [ORT optimized_model 持久化](#ort-optimized_model-持久化)
- [已完成 / 不再追踪](#已完成--不再追踪)

---

## 识别能力

### 中英混杂 / 专业词汇的识别后处理

**动机**：26 轮换了 Qwen3-ASR 之后通用识别质量跃迁,但**中英混杂的专业名词 / 冷门技术术语 / 品牌人名** 还是会翻车。典型案例（对我们的技术工作者用户画像是日常痛点）：

- 冷门开源项目名(小写、非词典词,模型只能猜音)
- 非行业内的缩写(组织名、个人昵称、产品 codename)
- 公司内部黑话 / 内部专有名词

**希望达到**：用户能维护一个**个性化热词表**，识别阶段或后处理阶段用这个表去引导 / 纠正。硬约束：

1. **仍然本地运行** —— 不向云端发音频或文字
2. **速度基本不变** —— 松开热键后粘贴延迟 < 500ms overhead
3. **用户能自己增删词汇** —— 最好在 Web 设置页直接管理

**候选方向**（都没深入验证过，真做时要先做技术 spike）：

- **Qwen3-ASR 原生 context / hot words 支持**:Qwen3-ASR 的 chat template 里天然有一段 system prompt 可以塞进"请特别注意识别以下词汇:xxx"这种提示。这是 LLM 式 ASR 相对传统 ASR 的结构性优势。**如果 prompt 引导对 ONNX int8 量化版仍然有效,这是最干净的路**。需要做技术 spike:造几组带冷门词汇的样本,比较空 prompt vs "请注意识别以下词汇:..." prompt 的识别率
- **文本后处理层基于拼音 / 编辑距离的纠错**:对 CJK + 英文混杂场景不好做,拼音匹配对英文词效果差
- **用户字典 → post-processing regex 替换**:最简单版本,让用户自己写 `"苦不乐他死" → "kubernetes"` 这种规则。代价是要手动加每个词,好处是透明可控
- **小型本地 LLM 兜底二次校正**:识别完交给 sub-GB 本地 LLM 做"校正这段话的专业术语",问题是延迟堆起来

**风险**：

- Qwen3-ASR ONNX 量化版的 system prompt 引导效果需要实测验证,int8 量化可能对 prompt 敏感度有折扣
- 用户维护词汇表的 UX 设计要想清楚（Web 设置页？还是编辑 txt 文件？用户怎么知道哪些词该加）
- 热词列表过长时,prompt token 占比会挤压解码窗口(当前 `max_total_len=1200`)

**scope**：中。关键看 Qwen3-ASR prompt 引导的有效性。有效 → ~200 行 + 设置页加一个 textarea + 把词表拼进 system prompt;走后处理管道 → scope 翻倍。**先花半天做 spike 确定技术路径再开轮**。

---

### 流式识别长音频滑动窗口

**背景**:28 轮上线了流式识别(策略 E:prefix-cached re-prefill + rollback=10),
但硬墙 ~33-38s(KV cache `max_total_len=1200` + audio_features 随 chunk 累积)。
超过会抛 `StreamingKVOverflowError`,28s 时浮窗会提示"接近上限"让用户松手。

对"一次按住说话 > 60s"的用户场景(例如念一整页稿子),需要真正的滑动窗口:

- encoder 端:累积 audio_features 超过某阈值(比如 800 tokens)时,淘汰最早的一段
- decoder 端:committed text prefix 超过某阈值时,只 prefill 最后 N 个(最近的
  committed 上下文足以让模型继续生成),丢弃更早的 committed KV

**风险**:
- encoder 窗口淘汰会让 cross-attn 丢失早期音频信息,长依赖的识别(长句 / 前呼
  后应)可能掉点
- decoder prefix capping 可能让模型在"新段落开头"时失去连贯性(标点 / 时态)

**scope**:中大。~100-150 行 + 一套"60s 长音频"测试 fixture。**验证成本高**,
需要录制真人念稿 60s / 120s / 180s 的样本,用它衡量质量损失。

---

### 流式 preview 浮窗显示 pending

**背景**:28 轮状态机维护 `pending_tokens`(rollback 窗口内未 commit 的 token),
本轮为了最小可行实现,**未暴露到 UI**,pending 只在内存里。28 轮收尾跟用户
讨论过 — 当前流式节奏 ~2s 出一段比微信输入法慢明显,微信的"近实时"很大
程度靠"在光标旁画蓝线显示未确认文本"这个 input method composition state 在
撑场面。

**讨论过的三种渐进方向**(按工程量从低到高):

1. **屏幕角落浮窗 pending 显示**(本条主体,优先做):
   - `stream_step` 已经吐出 `StreamEvent.pending_text`,WhisperInput 调
     `overlay.set_pending_text(evt.pending_text)` 让录音浮窗实时刷
   - 浮窗组件(GTK+Cairo / AppKit)加一行灰色副文本
   - 用户看到"已 paste(committed)" + "浮窗里飘的灰色 pending",视觉延迟降到 <1s
   - **跨平台一致、无失败模式**

2. **光标旁浮窗**(可选锦上添花):
   - macOS 走 Accessibility API(`AXUIElementCopyAttributeValue` +
     `kAXSelectedTextRangeAttribute` + `kAXBoundsForRangeParameterizedAttribute`)
     拿目标 App 的光标屏幕坐标
   - 浮窗定位到光标右下方,视觉上贴近"原地织字"
   - **风险**:Electron / Web / Java App 的 a11y tree 经常退化(VS Code、Chrome、
     Slack 都不一定拿得到光标 bounds);Linux X11 的 AT-SPI 几乎不通用;
     ~30% App 需要 fallback 到屏幕角落
   - 做完顶天也是"贴近版微信输入法",仍然不是真 input method composition

3. **真·input method 集成**(独立大改造,不属于此条):
   - macOS IMK / Linux fcitx / ibus,注册成系统输入源
   - 真正的"蓝线 composition" + 松手 commit 原生协议
   - **整个产品形态变更**:不再是"按住热键 → 录音 → 粘贴"的全局工具,而是
     per-text-field 激活的输入法。跟当前极简交互冲突,要重新讨论形态

**改动面(只算路径 1)**:
- `stt/qwen3/_stream.py`:StreamEvent.pending_text 已经暴露,无需改引擎
- `backends/overlay_linux.py` / `overlay_macos.py`:浮窗组件加一行副文本区
- `__main__.py`:`_do_stream_step` 里调 `overlay.set_pending_text(...)`

**scope**:路径 1 ~80 行 + 三语 locale 各 1-2 条新字符串。路径 2 ~150 行 + AX
跨 App 兼容性兜底(失败时退化到路径 1)。先做路径 1,实测体感后看要不要冲
路径 2。

---

## 设置页体验

### STT 模型按需可视化下载 + 已下载状态感知

**动机**:26 轮上线了"识别模型"下拉(0.6B / 1.7B)+ 热切换,但只照顾了"两个模型都已下载"的稳定态。实际用户路径有两个坑:

1. **1.7B 第一次被点到时**,后台 `Qwen3ASRSTT("1.7B").load()` 会串行跑 `modelscope.snapshot_download`,在中国带宽下一次拉 ~2.4 GB,**保守估计 5-10 分钟**。设置页唯一反馈是"切换中..."的 toast —— 用户不知道"还要等多久"、"有没有在进,下"、"能不能取消",也可能误以为程序卡死把它杀掉,导致 cache 半成品,下次再 load 就爆
2. **用户想先下模型再用**:现在没有任何入口,只能硬切一次过去等。命令行 `daobidao --init` 只下默认的 0.6B,下不了 1.7B(`--init` 读 config 里的 `qwen3.variant`,默认 0.6B)

**希望达到**:

- 设置页"识别模型"区域对每个 variant 额外显示状态:**"已下载 / 未下载 / 下载中 X%"**
- 未下载的 variant 在下拉里 **disabled**,旁边带一个"下载此模型"按钮 —— 点了才开始下(而不是选中就自动下)
- 下载过程**可视化**:进度条 + 已下载 MB / 总 MB + 实时下行速度,可取消
- 下载完成后按钮消失,下拉里自动 enable
- 切换到**已下载**的 variant 时延迟只剩 session load(~4 秒),不再有网络等待

**候选方向**(两端分开想):

- **后端**:
  - `_downloader.py` 加 `check_variant_downloaded(variant) -> bool`:遍历 modelscope cache 目录,看那 3 个 onnx 文件是否都存在且 size 合理(manifest 里有精确字节数,可校验)
  - 新建 `DownloadManager` 管"进行中的下载任务",暴露 `start(variant)` / `cancel(variant)` / `status(variant) -> {state, downloaded_bytes, total_bytes, speed, error}`,内部走 daemon thread 跑 `snapshot_download` —— **关键问题**:`modelscope.snapshot_download` 是否原生吐进度回调? 没有的话就自己包装 HTTP 下载(`requests` + `stream=True` + `iter_content` + 手动算速度),skip modelscope 这层,直接命中 ModelScope HTTP URL。**先做一个 10 行 spike 验证可行性**
  - `settings_server.py` 加两条端点:`GET /api/stt/variants` 返回每个 variant 的下载状态 snapshot、`POST /api/stt/download` 触发下载、`POST /api/stt/download/cancel` 取消
- **前端**:
  - 页面加载时先查 `/api/stt/variants`,根据状态装饰下拉项(`disabled` + 文字加上"(未下载)")
  - 点"下载"按钮后进入轮询状态(复用 stt_switch_status 那套 500ms 轮询模式),渲染进度条
  - 完成后重新查 `/api/stt/variants`,enable 下拉项 + 隐藏按钮

**风险 / 注意点**:

- **取消下载的文件残留**:mid-download kill 后 modelscope cache 目录会有半成品 `.onnx.incomplete` / tmp 文件。要么 cancel 时显式 `shutil.rmtree(不完整目录)`,要么依赖 modelscope 自己下次下载时识别为损坏重新拉。先查 modelscope 行为再决定
- **并发控制**:同时允许两个 variant 一起下? 还是 serialize? 前者节省总时间但会抢带宽互相拖垮,后者用户体验更稳 —— 倾向 serialize,一次只下一个
- **1.7B 下载过程中应用崩了**:下次启动要能识别到 cache 里的半成品并提示用户"上次下载未完成,重新下?",不然永远 stuck
- **进度条数据来源**:如果绕开 modelscope 直接 HTTP 下载,断点续传、multi-part、镜像 failover 等功能就都自己写,是 scope 膨胀项。现实妥协:第一版**不做断点续传**,中断就从头来
- **`--init` 命令行也该支持选 variant**:`daobidao --init --variant 1.7B`,不阻塞这一轮但可以顺手做

**scope**:中。后端 ~150 行(含 DownloadManager + 两条端点 + spike 验证) + 前端 ~80 行(状态渲染 + 进度轮询 + 按钮态切换)+ 3 份 locale 各 6-8 条新字符串。**关键前置是 modelscope 是否暴露进度回调的 spike**,半小时内能验明;如果不暴露需要绕开 modelscope 自己 HTTP 下载,scope 再翻 50%。优先级**高** —— 这是 26 轮的直接遗留,用户视角看就是"买了个坏的下拉"。

---

## 应用生命周期

### 启动时检测并清理已有实例

**动机**：调试时遇到过上次没退出干净的僵尸进程，导致新启动的实例行为异常（热键被老进程抢走、端口被占、settings_server 起不来等）。用户手动 `ps | grep` 再 kill 太繁琐。

**希望达到**：启动流程里加一个前置步骤 —— 检测是否已有 daobidao 实例在跑，有就干掉老的，再继续启动新的。用户感知到的就是"双击启动 = 重启"。

**候选方向**：

- **端口探测**：`settings_server` 已经绑了一个独占端口，启动时先 `connect()` 探一下。占用 → 通过 `lsof -i :<port>` 或 `psutil.net_connections()` 拿 PID → SIGTERM → 等 1-2 秒 → SIGKILL 兜底。副作用最小，因为端口是本应用独占的
- **PID 文件**：`~/.cache/daobidao/daobidao.pid`，启动时读取 + `os.kill(pid, 0)` 探活 + 校对 `psutil.Process(pid).cmdline()` 防 PID 复用误杀
- **单实例锁**（`fcntl.flock`）：最干净的判定，但"拿不到锁后是 kill 老的还是退出"仍要自己决策，等于把问题推后
- **健康探测 + 强制 kill 组合**：PID 存活不等于状态正常（僵尸进程的典型症状就是"进程在但热键死了"）。更稳的做法是端口探测到后，HTTP ping 一下 settings_server 的 `/health`（得先加），3 秒无响应就当僵尸处理

**风险 / 注意点**：

- macOS 下经 `Daobidao.app` launcher 启动时，cmdline 和 `uv run daobidao` 不一样，识别逻辑要同时覆盖两种
- kill 老实例的时机要在 "绑端口 / 注册热键" 之前，否则自己会被自己的检测逻辑误伤
- 用户在两个 shell 里手动各起一个做对比调试的场景会被打断 —— 可以加一个 `--allow-multiple` flag 兜底

**scope**：中。~100 行，主要集中在 `__main__.py` 启动序列开头；外加 settings_server 可能要暴露一个 `/health` 端点。先选端口探测这条路做 MVP，够用再说。

---

## 代码质量

### 测试套增强（v2）

15 轮搭起了 pytest 框架(`tests/` 下纯逻辑层 + 带 mock 的边界层 + 端到端 STT 推理 + 默认开启的覆盖率报告 + codecov 上传 + README 徽章)。26 轮跟着把 `stt/qwen3/` 全家桶写成 100% 覆盖,总线从 51% → 61%(239 个用例)。但仍有几个明显能继续推进的方向。**先做不做都不影响项目正常运行**,列在这里是为了记住来路：

- **`__main__.main()` 编排路径**：主入口的 CLI 解析 / 托盘启动 / preload / 信号处理这一段约 230 行目前是 0% 覆盖(整体 51%→61% 的差距全在这里)。推到 70% 的主要抓手就是这一段。难点是它耦合了托盘 / 浏览器 / 信号,需要用 `capsys` + 大量 patch 写集成式测试
- **macOS CI runner 矩阵**：当前 `build.yml` 只跑 `ubuntu-24.04`。conftest 注入的 fake pynput / evdev 在真 darwin 上是否完全等价于真 pynput 还需要本地 macOS 跑一次确认。如果要彻底保险,加 `macos-latest` 进 matrix —— 代价是 macos runner 比 ubuntu 贵 10×
- **hotkey 测试升级**：当前测试直接调 `_on_hotkey_press` 等 internal 方法,所以 `hotkey_macos.py` / `hotkey_linux.py` 卡在 54% 覆盖率(`_listen_loop` / `start` / `stop` / `find_keyboard_devices` 都没测)。更接近真实路径的做法是通过 fake `Listener` / fake evdev 设备**注入合成键盘事件**,让 `_listen_loop` / pynput callback 自然驱动状态机。改造后能把覆盖率推到 80%+
- **STT 多语种 / 边角样本**：当前 `test_qwen3_asr.py` 只测一条中文(`tests/fixtures/zh.wav`)。可以录制 / 收集 en / ja / ko / yue 各一段短音频作 fixture,各加一个用例覆盖多语种解码路径 + verifyQwen3-ASR 声称的多语种能力在 ONNX int8 版本上是否掉点。也可以试一下噪声 / 长音频 / 多说话人这些边角场景

**scope**：每条都不大,小到一两个小时,大到半天。哪条优先看痛点 —— 如果某次 PR 因为没有 macOS CI 漏掉了一个 darwin-only 回归,就先做第二条;如果想把 coverage 徽章推过 70%,就先做第一条。

---

### 并发模型迁移到 asyncio

**动机**：当前全仓走 **threading + 阻塞 IO** 的路子 —— `settings_server` 用 stdlib `http.server`（每请求一线程），`recorder` 在录音线程里 blocking `sounddevice`，未来加的 updater / 版本检查都要手动包 `threading.Thread`。这种写法在 2026 年的 Python 里已经不是主流：

- 性能：GIL 下多线程的 IO 并发效率本身就不高，线程切换和锁开销白交；asyncio 在单线程事件循环里跑，IO 密集场景（HTTP / subprocess / 文件）吞吐和内存都更好
- 工程整洁度：`async def` + `await` 的调用图比 "Thread + Queue + Event + Lock" 容易读、容易测，不用再手动管 daemon / join / 超时
- 生态：`httpx` / `aiohttp` / `asyncio.subprocess` / `starlette` 等现代库全是 async first

**希望达到的状态**：

- HTTP 服务换成 `aiohttp` 或 `starlette + uvicorn`（后者更主流，但装机体积大一些）
- 录音 / STT / 子进程调用全部通过 `asyncio.to_thread()` 或原生 async API 接入
- `WhisperInput` 主控从 "threading 编排" 改成 "单一事件循环 + 少量线程边界"
- 所有未来新增的后台任务（update checker、健康探测等）默认写 `async def`

**阻力 / 注意点**：

- **GUI 层（pystray / Cocoa overlay / GTK overlay）强绑主线程 + 自己的 runloop**，asyncio 事件循环必须以"非抢占"方式共存（`asyncio.run` 放后台线程 / `qasync` / Cocoa runloop integration），这是整个迁移里最难的一块
- **pynput / evdev 的监听循环是 blocking 线程**，改成 async 要么等上游支持，要么用 `loop.run_in_executor`
- `onnxruntime` / `sounddevice` 天生同步，只能靠 `to_thread` 包装
- 24 轮新写的 `updater.py` 会和这个方向暂时逆行（还是 threading + `urllib`），迁移时一并改造
- 迁移改动面巨大，**不是一轮能做完的** —— 应该先做 POC（比如只把 settings_server 换成 aiohttp 试水），验证 GUI 共存方案可行，再全面推进

**scope**：大。估计需要先花半天做 GUI + asyncio 共存的技术 spike，spike 通过后至少 2-3 轮完成完整迁移。优先级：**非阻塞但方向明确** —— 现在 threading 写法还能跑，没到性能瓶颈，但继续往上堆功能迟早要还技术债。

---

## 启动性能

### ORT optimized_model 持久化

**动机**：第 27 轮压掉了 snapshot_download 这段（1.5–2.4s → 44ms），但 ONNX session 构造仍然 ~1.5s 没动。27 轮原 plan 想用 `ThreadPoolExecutor` 并行三个 session，实测只省 ~7%（远低于估的 30-50%），已回滚。根因是 ORT `InferenceSession.__init__` 内部两块 GIL 释放不彻底：protobuf 图反射里大量跨 C++/Python 边界调用、`CPUExecutionProvider` allocator 进程级 mutex，让并行线程大段时间在串行等。

**希望达到**：cache 命中冷启动 `qwen3_runner_ready.elapsed_ms` 从 ~1500ms 降到 500-800ms 级别，总冷启动压到 2s 以内。

**候选方向**（ROI 高到低）：

- **`SessionOptions.optimized_model_filepath` 落盘**（首选）：ORT 原生支持把 graph optimization 的产物（算子融合、常量折叠后）序列化到磁盘。第一次跑 1.5s，之后 `InferenceSession` 直接 load 优化图，跳过所有 optimization pass。社区报告省 30-60%。落盘位置 `~/.cache/daobidao/ort_cache/{variant}/{conv,encoder,decoder}.opt.onnx`，不能写进 modelscope cache（只读语义）
- **subprocess 预编译**：下载完模型后立刻起子进程跑一轮 session 构造把 opt 图 bake 出来，用户视角没有"首次慢、后续快"的不一致。代价是多 100-150 行流水线 + bake 中断兜底
- **multiprocessing 并行构造**：`ProcessPoolExecutor` 绕 GIL，理论 3× speedup。但 `InferenceSession` 不能跨进程 pickle 回主进程，整个架构要重写，scope 爆炸，不推荐

**风险 / 注意点**：

- ORT 版本升级后旧 opt 文件可能解析失败，需要兜底（catch + 删旧文件 + 重跑一遍正常 init）
- 落盘的 opt 文件大小和原 .onnx 差不多，多占一份磁盘（0.6B 约 +990 MB，1.7B 约 +2.4 GB）。需要评估用户磁盘压力，或把 opt cache 做成可清理
- 首次跑仍然 1.5s（"第一次为下一次服务"），要么用户第一次启动仍然慢、要么和"下载完预编译"方案组合

**scope**：中。~60 行 + 一份 cache 失效兜底 + 测试覆盖 "首次 opt 落盘 / 二次命中 opt / opt 损坏兜底" 三条路径。需要评估磁盘占用是否值得，可在启动性能仍是痛点时再启动。

---

## 已完成 / 不再追踪

这一段记录从早前 SUMMARY "后续 TODO" 里**刻意移除**的条目，避免未来自己或后续 agent 翻老 SUMMARY 发现"为什么这条没做"，误以为是遗漏：

- **首次模型下载进度 UI**（14 轮 SUMMARY 局限性 #3）—— 实测下载速度已经够快（ModelScope 国内 CDN 秒级），用户痛点不明显，不值得做
- **Linux 实机验证**（14 轮 SUMMARY 局限性 #4）—— 已在干净 Ubuntu 上手动验证通过
- **跨平台 Pythonic overlay 统一代码**（16 轮遗留）—— 视觉已在 16 轮对齐（微信输入法风格深蓝药丸），双份原生实现（GTK3+Cairo / AppKit）维持现状。Tkinter 与 pystray 主线程冲突、子进程方案引入退出清理复杂度，真要统一得换 Tauri 这类方案全面接管 UI 层，不是 overlay 一个模块的事，当前版本满意，不再追
