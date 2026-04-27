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
- [UI/UX 体验](#uiux-体验)
  - [跟随系统默认输入设备切换](#跟随系统默认输入设备切换)
  - [流式 preview 浮窗显示 pending](#流式-preview-浮窗显示-pending)
  - [模型管理加「删除」按钮](#模型管理加删除按钮)
  - [流式 worker 落后于音频时的 backpressure 提示](#流式-worker-落后于音频时的-backpressure-提示)
- [代码质量](#代码质量)
  - [1.7B 端到端测试在非 Linux x86 上不稳定](#17b-端到端测试在非-linux-x86-上不稳定)
  - [测试套增强（v2）](#测试套增强v2)
  - [并发模型迁移到 asyncio](#并发模型迁移到-asyncio)
- [性能](#性能)
  - [ORT optimized_model 持久化](#ort-optimized_model-持久化)
  - [1.7B 模型启用 GPU 推理后端（CUDA / CoreML）](#17b-模型启用-gpu-推理后端cuda--coreml)
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

## UI/UX 体验

### 跟随系统默认输入设备切换

**动机**：daobidao 启动后用户在系统设置里切换默认输入设备(如插上 USB 麦后切过去),**daobidao 不会跟着切** —— 实际录音和设置页的麦克风列表都还在用启动那一刻的设备。重启程序就能识别到新默认设备,确认是缓存陈旧问题。

**根因**(已查证,不是我们用错):

- PortAudio 在 `Pa_Initialize()` 那一刻 snapshot 系统默认设备,之后 `Pa_GetDefaultDevice` / `sd.default.device[0]` 永远返回那个缓存值,系统切默认时**不刷新**
- PortAudio 上游 [issue #615](https://github.com/PortAudio/portaudio/issues/615) collaborator RossBencina 亲口确认这是设计行为:*"Pa_GetDefaultDevice would only be updated after a refresh (or after calling Terminate/Initialize)"*
- HotPlug API(带 device-changed 回调)在 [HotPlug wiki](https://github.com/PortAudio/portaudio/wiki/HotPlug) 设计了多年,**至今未合进 main**
- python-sounddevice 维护者在 [issue #516](https://github.com/spatialaudio/python-sounddevice/issues/516) 明确推荐用 `sd._terminate(); sd._initialize()` 作为 workaround
- [bastibe/SoundCard](https://github.com/bastibe/SoundCard/blob/master/soundcard/coreaudio.py) 库直接绕开 PortAudio,在 macOS 上 ctypes 调 CoreAudio `AudioObjectGetPropertyData(kAudioHardwarePropertyDefaultInputDevice)` 拿实时默认设备,无缓存

**目标状态**:用户切了系统默认输入后,**打开 daobidao 设置页一次** → 设置页列表正确显示新设备 → 之后按热键录音自动用新设备。无需重启程序、无需每次录音都做检测。

**方案(极简版)**:

1. 设置页"麦克风检测"卡片**打开时**调一次 ctypes CoreAudio `AudioObjectGetPropertyData(kAudioHardwarePropertyDefaultInputDevice)` 拿实时默认 device ID,匹配 `sd.query_devices()` 列表里同名设备拿到 PortAudio 的 device index
2. 把这个 index 写进**进程级全局 cache**(比如 `WhisperInput.current_input_device` 或 config_manager 里的 runtime 字段)
3. `recorder.py` 的 `sd.InputStream(...)` 改成**显式传 `device=cache_index`**,不再依赖 PortAudio 自己挑默认
4. 设置页 `_handle_audio_devices` 的 `is_default` 判断也读这个 cache,跟实际录音保持一致

**显式不做**(用户明确反对):

- **每次按热键时刷新设备列表** —— 用户认为这是"每次录音前确保对"的过度防御性写法,担心一旦刷新逻辑出 bug 就**每次录音都挂**,影响范围不可控。倾向"打开设置页时刷一次"的范围更可控

**候选 v2(锦上添花,不属于本轮)**:

- macOS 注册 CoreAudio property listener(`AudioObjectAddPropertyListener` 监听 `kAudioHardwarePropertyDefaultInputDevice`),系统切默认时收回调自动刷新全局 cache → 用户不打开设置页也能跟着切。~50 行 ctypes,无新依赖
- Linux 走 PipeWire / PulseAudio 的 device-change 事件,更准但跟当前 pactl 主动查的风格冲突,不优先

**风险 / 注意点**:

- **Linux 路径未实测**:推测 PipeWire/PulseAudio 因为有"虚拟 default source"层,系统切默认会自动 reroute 同一个 device → daobidao 应该自然跟着走。但裸 ALSA 没有这层,可能跟 macOS 同病。本轮做完**必须在 Linux 接两个麦实测**:启动时选 A → 系统切到 B → 看实际录音和设置页是否跟着。如果 PipeWire 真无缝,就只在 macOS 路径加 ctypes 刷新逻辑;如果 Linux 也中,沿用 32 轮 pactl 路径加一条"查实时 default source"
- **Cache 失效场景**:如果用户在打开设置页之后又切了一次麦,cache 又陈旧了。但这是用户明确接受的代价(直觉上"切了麦再开一次设置页"是合理操作)
- **device index 漂移**:`sd.query_devices()` 返回的 index 在设备热插拔后可能变化(原 index 0 的麦拔了,新插的占了 index 0)。Cache 里存 index 不存 name 的话,可能指向错的设备。**改成存 name 更稳**,录音前用 name 反查当前 index
- **CoreAudio device ID → PortAudio index 的映射**:CoreAudio 给的是 `AudioObjectID`(uint32),PortAudio 给的是 host-api 内部 index,两者**不直接互通**。需要拿 CoreAudio 的 device ID 反查设备 name(再一次 `AudioObjectGetPropertyData` + `kAudioObjectPropertyName`),然后在 `sd.query_devices()` 列表里按 name 匹配。代码模式跟 SoundCard 一致

**scope**:小到中。

- macOS 路径:ctypes 调 CoreAudio 拿实时默认 + name 反查 + match PortAudio index ~50 行 + recorder/settings_server 改成显式传 device ~20 行 + 测试 ~50 行 → 半天到一天
- Linux 实测验证(可能不需要改代码):接两个 USB 麦或 USB + 内置实测,~半小时
- v2 的 CoreAudio listener 不在本轮 scope

**优先级**:中 —— 用户原话"没人天天切默认麦",真触发的频率低。但触发时影响**所有 macOS 用户**(不需要 Mac mini / Studio,普通 MacBook 插 USB 麦就能撞上),修复价值高。可以挂在那里等想动手时再开一轮,不阻塞主流程。

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

### 模型管理加「删除」按钮

**动机**:36 轮"模型管理"卡片做了下载流程,但**没有删除入口** —— 用户想腾磁盘空间(1.7B 占 2.4GB)、或者下了之后发现 1.7B 不好用想换回 0.6B,目前只能命令行 `rm -rf ~/.cache/modelscope/.../model_{variant}/`。从 UX 对称感看,"下载"按钮天然该有"删除"配对,缺这块卡片"感觉缺点东西"。

**目标状态**:
- 已下载的 variant 旁边加"删除"按钮(跟"下载"按钮位置对称,只显示其中一个)
- 点击 → 确认弹窗(避免误删)
- **当前 active variant 不允许删**(防止删了下次按热键挂);UI 上"删除"按钮直接置灰 + tooltip 解释,后端也拦截
- 删除完成 → cache 状态自动刷新 → 下拉里这条 variant 自动 disabled → 用户可以重新点"下载"重下

**候选方向**:
- **后端**:`DownloadManager` 加 `delete(variant) -> tuple[bool, str|None]` 方法,内部 `shutil.rmtree(cache_root / f"model_{variant}/")`,再调一遍 `is_variant_downloaded` 让 `ModelFileSystemCache` 索引顺便反映清理。`tokenizer/` 是两个 variant 共享的**绝对不动**。`settings_server.py` 加 `POST /api/models/delete`(body `{variant}`),逻辑跟 download/cancel 端点同一套
- **前端**:`refreshModelStatus` 渲染时,`downloaded && !active` → 显示删除按钮;active → 灰按钮 + tooltip "当前模型不能删除,请先切换到另一个 variant"
- **active 判定**:读 config.qwen3.variant 还是直接看 `WhisperInput.stt.variant`?后者更准但需要把 active variant 也透到 settings_server。前者简单,但 hot-switch 期间会有短暂错配 —— 倾向**后者**,加一个 callable getter 类似 `stt_switch_status_getter` 模式

**风险 / 注意点**:
- **误删 active**:必须前后端双拦截
- **正在下载时点删除**:边界 case 不该出现(downloading 时按钮逻辑应该让 download/cancel 显示,不显示 delete),但后端要兜住:`delete()` 时持锁检查 `_active_variant`,若等于该 variant 拒绝
- **删除时 modelscope 索引 stale**:`ModelFileSystemCache.get_file_by_path` 自带磁盘一致性兜底(`os.path.exists` 失败时清索引),所以 rmtree 后下次 `is_variant_downloaded` 会自动 False。**不用我们手动写 `.mcs`**
- **共享 tokenizer**:`shutil.rmtree(cache_root / f"model_{variant}/")` 作用域只在 model_{variant}/,不会误删 tokenizer/
- **3 语 i18n**:`model_delete_btn` / `model_delete_confirm_title` / `model_delete_confirm_body` / `model_delete_failed` / `model_delete_active_hint` 等约 5-6 条新 key

**scope**:小到中。后端 ~30 行 + 端点 ~25 行 + 前端 ~40 行(按钮 + 确认弹窗 + 联动状态)+ 三语 i18n 各 5-6 条 + 测试 ~80 行(`test_download_manager.py` 加 delete 用例 + `test_settings_server_models.py` 加端点用例)= **半天**。无前置 spike,逻辑跟 36 轮 download/cancel 对称,可直接照抄套路。

---

### 流式 worker 落后于音频时的 backpressure 提示

**动机**:35 轮加滑窗后 KV cache 硬墙不再是问题,但单线程 worker 的 `stream_step` 处理速度(~500-800ms / chunk on Apple M1, 0.6B)在用户极快语速 / UP 主播放等场景下可能跟不上音频流入(2s / chunk)。当前没有 backpressure 机制:

- `_event_queue` 持续增长(每 chunk 128KB chunk reference)
- `state.committed_tokens` 永久累加(prefill slice 截了,但 `tokenizer.decode` 全量算 → 几小时后 decode 比推理还慢)
- 用户视觉**无任何反馈**,只感知"字越出越慢" + "松手后还要等很久才出最后那段"
- 极端连讲几小时会被 OS OOM kill,但日常不会

**目标状态**:

- worker queue 长度超阈值 → 浮窗变色(复用 32 轮 error_state 红色药丸思路,可能换不同色比如黄色)+ 视觉传达"识别落后"
- 用户看到提示后可主动停顿让 worker 追上,或松手 finalize
- **不丢 chunk**(用户明确否决了"自动丢老 chunk"方案,可接受看到提示后自己放慢)
- queue 退回阈值以下 → 浮窗自动恢复正常态

**候选方向**:

- **浮窗加 backpressure 状态档**:目前已有 `idle / recording / processing / error / ready`,加 `backpressure` 第六档。macos / linux overlay 各加一组绘制逻辑(~30 行/平台)
- **双阈值去抖(Schmitt trigger)**:比如 queue ≥ 5 触发 backpressure 态,≤ 3 退出,避免抖动反复闪
- **阈值自适应**:固定 5 chunks 在 M1 / Intel 上等效不同延迟,可以做成 "阈值 = max(3, ceil(chunk 平均处理时长 / 2s) * 2)" 这种基于 EMA 的自调节,需要 spike
- **文案不依赖 i18n**:35 轮发现 `overlay.update(text)` 的 text 参数被无视(120×34 太窄渲染不下),所以纯靠**视觉(颜色 + 可能的图标)**传达,不加 i18n 字符串

**风险 / 注意点**:

- 浮窗状态机已有 error_state 抖动 bug 历史(32 轮修过),新增 backpressure 要复用同款 timeout / cancel 机制,避免重复造轮子
- 阈值定多少需要测:M1 vs Intel CPU 上 stream_step 速度差异大,固定 5 不一定通用。先静态阈值落地,等用户实测痛了再上自适应
- backpressure 跟 recording / processing 视觉区分要清晰(三种活动态了),可能用不同颜色饱和度
- 长 session(几小时)的 `committed_text` decode 退化是另一个相关问题,**不在本条 scope** —— 那条值得另开一条 backlog "增量 decode 优化"
- "正常用户不会触发"(语速 < 模型速度),所以这是 UX 兜底而非常规路径,优先级中等

**scope**:小到中

- macos + linux overlay 加新状态档:~60 行
- `_on_stream_chunk` 加 queue 长度检测 + state 切换调用 + 双阈值去抖:~30 行
- 测试覆盖(主要测阈值翻转 + 状态切换):~50 行
- 静态阈值版半天能落地;自适应阈值版多半天

---

## 代码质量

### 1.7B 端到端测试在非 Linux x86 上不稳定

**背景**:33 轮发现 `test_transcribe_zh_wav[0.6B/1.7B]` + `test_streaming_via_full_whisperinput_pipeline[0.6B/1.7B]` 在 GH Actions ubuntu-24.04 (x86_64 云 VM) 上**抽签翻车** —— 同一 commit rerun 一次过一次挂。归因是"长 prompt(~800 token)int8 量化推理在不同 runner SKU 上数值不稳定,greedy 第 1 个 token 偶发翻成 EOS,识别返空"。当时的 mitigation 是设 `DAOBIDAO_SKIP_E2E_STT=1` 让 CI 跳过这 4 条,**写在文档里的判断是"本地一直稳"**。

**35 轮新观察**:在作者的 Mac Studio (Apple Silicon, ARM) 上,`test_transcribe_zh_wav[1.7B]` **本地也确定性挂了**(0.6B 仍稳)。33 轮的"本地一直稳"假设破裂。重新核实数据点后:

| 平台 | 架构 | 1.7B 测试 |
|---|---|---|
| 作者 Linux 机 | x86_64 (Intel/AMD) | PASS(确定性) |
| 作者 Mac | ARM64 (Apple Silicon) | FAIL(确定性) |
| GH Actions ubuntu-24.04 | x86_64 (云 VM 抽 SKU) | FAIL(概率性) |

**真因猜测**:onnxruntime CPU EP 的 micro-kernel 跟 CPU/SIMD 强绑定 —— Linux x86_64 走 MKL/OpenBLAS + AVX2/AVX512;Mac ARM64 走 Accelerate framework / NEON;CI x86 是云 VM 不同 SKU。**同一 ONNX int8 模型在不同微架构上 dequant + matmul 的累计误差走不同数值路径**,长 prompt(1.7B 比 0.6B 更激进的量化 + 更长 audio_features)放大误差,greedy 第一 token 距离 EOS 决策边界很近时被翻 EOS。

**希望达到**:`uv run pytest`(无 skip env)在所有支持的开发架构(x86_64 Linux + ARM64 macOS,后续可能加 ARM64 Linux)都能稳过,无需 `DAOBIDAO_SKIP_E2E_STT` workaround。

**候选方向**(都没深入验证,真做时按需 spike):

- **改换关键词断言**:1.7B 测试目前断言 `"先帝" in text`,改成 "至少匹配下面 N 个关键词中的 K 个" 这种更宽松的形式,容忍 ASR 输出有少量字面差异。**问题**:核心 bug 是返**空字符串**(0 个关键词命中),不是字面不一致 —— 宽松匹配也救不了
- **迁移到 fp16 ONNX 1.7B 模型**:int8 量化是误差源头,fp16 应该数值更稳。代价:模型大小翻倍(~4.8 GB)、CPU 推理慢一倍。**最后才考虑**
- **Greedy 决策加 temperature / top-k 兜底**:第一个 token 翻 EOS 时不直接接受,看 top-2 / top-3 候选。改 inference loop ~30 行。**问题**:跟 ASR 自回归语义冲突,不优雅
- **官方上游 ONNX 重新 export**:可能 `zengshuishui/Qwen3-ASR-onnx` 这一份 int8 量化校准不充分,别的社区 export 或自己重 export 用更大 calibration set 可能稳一些。**重投入,涉及量化技术栈**
- **改回累积 transcribe 模式 + 强制不让模型早 EOS**:在 first MAX_NEW_PER_CHUNK token 内禁止 EOS。简单,但可能引入新 artifact

**风险 / 注意点**:

- 这个问题**不影响实际用户体验** —— 用户用流式模式(35 轮已验证 0.6B 端到端稳),离线模式 0.6B 也稳。1.7B 在 Mac/CI 上偶发返空是 **测试稳定性问题**,不是产品质量问题
- 跑 spike 验证猜测时,**注意区分"模型行为不稳"和"测试断言太严"** —— 用 ASR 模型对同一个 wav 跑 100 次看输出分布,先确认"翻 EOS"确实概率性发生
- 35 轮 `test_qwen3_stream_sliding_real.py` 也跟着 skip 了,因为它跟 1.7B 同病(长 prompt 0.6B 在 ARM 上是否稳还未验证 —— spike 跑成功的情况下应该稳,但要在 CI / 多机器多次跑才知道)

**scope**:中。spike 半天,确定真因后选方向。
- 选"宽松断言"路径 → ~50 行测试改动,不解决根因但降低噪音
- 选"fp16 模型"路径 → ~100 行 + 文件变化大 + 用户视角重新选 variant
- 选"决策兜底"路径 → ~30 行 inference 改动 + 大量 cross-arch 验证

**优先级**:中 —— 不影响产品,只影响"开发者打开 pytest 看到红"的体验。`DAOBIDAO_SKIP_E2E_STT` 已是有效兜底,完整修复可以排在用户向痛点之后。

---

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

## 性能

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

### 1.7B 模型启用 GPU 推理后端（CUDA / CoreML）

**动机**：30 轮修好了 1.7B 不可用之后,实测**纯 CPU 推理跑 1.7B 性能吃力**。在用户实测机(Intel 13700K,高端桌面 CPU)上松手到出字仍然有明显延迟,流式 chunk 处理也不再"近实时"。0.6B 在 CPU 上完全够用是 round 26 决定走 onnxruntime CPU-only 的依据,但 1.7B 是 ~2.4× 体量,对纯 CPU 路径已经超出舒适区。如果想让 1.7B 真正成为"想要更高准确率"用户的可用选项,GPU 推理是绕不开的方向。

**希望达到**：

- 检测到合适的 GPU 设备(NVIDIA + CUDA / Apple Silicon + CoreML)时,1.7B 自动走 GPU EP,流式每 chunk 处理时间降到跟 0.6B CPU 同档(< 500ms),离线 transcribe 跟 0.6B CPU 体感无差
- CPU-only 仍是稳定 fallback,无 GPU 用户和 GPU 不可用(驱动 / 版本不匹配)时无缝退化
- 设置页"识别模型"区域显示当前使用的 EP(`CPU` / `CUDA` / `CoreML`),用户可以肉眼确认 GPU 真生效了
- 0.6B 不强制开 GPU —— CPU 已经够,GPU 反而多一份系统依赖,默认走 CPU 即可(可在设置页强制覆盖)

**候选方向**:

- **`onnxruntime-gpu` (CUDA EP)**:ORT 原生支持,加 `CUDAExecutionProvider` 到 providers 列表即可。但 `onnxruntime-gpu` 是独立 wheel,跟 `onnxruntime` CPU 包**互斥**,意味着要嘛改 `pyproject.toml` 用 extras (`pip install daobidao[cuda]`),要嘛运行时检测后另开 venv,要嘛默认走 CPU 包再让用户手动 `uv tool install --reinstall daobidao --with onnxruntime-gpu` 覆盖。**首选 extras** —— 跟 PyPI 主流做法一致
- **`CoreMLExecutionProvider`(macOS Apple Silicon)**:onnxruntime 的 Apple 加速 EP,走 ANE / GPU。算子覆盖度相对 CUDA EP 弱,1.7B int8 量化模型的算子是否全支持需要 spike 验证,可能某些算子掉回 CPU 反而慢
- **`DirectMLExecutionProvider`(Windows + 任意 GPU)**:跨厂家(NVIDIA / AMD / Intel),作用面广。但 daobidao 当前不主打 Windows,优先级低
- **`ROCMExecutionProvider`(AMD GPU on Linux)**:作用面更窄,小众,不优先做

**风险 / 注意点**:

- **包体积膨胀**:onnxruntime-gpu(Linux x86_64) ~250 MB,加 CUDA runtime 系统依赖 ~1 GB+。0.6B 用户用不上,得通过 extras 让默认安装路径不变
- **CUDA 版本绑死**:onnxruntime-gpu 13.x 绑 CUDA 12,onnxruntime-gpu 1.x 绑 CUDA 11。用户机器 CUDA 版本不一致就会 fail。要在 ImportError / 创建 InferenceSession 时 catch,优雅退化到 CPU
- **算子掉 CPU 回退**:int8 量化模型的部分算子(QLinearConv / DynamicQuantizeLinear 等)在 CUDA EP 上可能没实现,onnxruntime 会自动 fallback 到 CPU EP,导致**部分图在 GPU 部分在 CPU,反而比纯 CPU 慢**。spike 阶段要看 `session.get_providers()` 实际生效的 provider,跑一段 audio 看每段耗时
- **Apple Silicon 量化模型**:CoreML EP 对 int8 量化的支持比 fp16 / fp32 弱很多,可能跑不起来或退化严重;实测可能要重新 export 一份 fp16 的 1.7B ONNX 给 CoreML 路径用,文件大小翻倍
- **多 EP 之间的输出一致性**:CPU vs CUDA vs CoreML 推理结果可能在低位数值上有差异,影响 greedy decode 选 token,极端情况下 transcript 不一样。需要新增 cross-EP 一致性测试或者接受"GPU 路径 transcript 可能跟 CPU 略有差异"
- **桌面用户的 GPU 占用感知**:用户在跑游戏 / 训练 / 视频时按热键说话会跟其它 GPU workload 抢资源,延迟可能反而比 CPU 不稳。**默认 0.6B 走 CPU、1.7B 才考虑 GPU** 是合理的妥协

**前置 spike**:

1. 用现有 1.7B ONNX,在用户的 13700K 机器上(如果有 NVIDIA GPU)挂 `CUDAExecutionProvider`,跑一遍 zh.wav 测 encoder + decoder 单步延迟。`session.get_providers()` 看实际生效是不是 `["CUDAExecutionProvider", "CPUExecutionProvider"]` 顺序
2. 在 Apple Silicon 机器上挂 `CoreMLExecutionProvider`,看是否报算子缺失,跑一遍单测看延迟
3. 比较 CPU vs GPU 各 EP 上 1.7B 流式每 chunk 的 ms,**如果不到 ≥ 3× speedup 就不值得做**(CUDA EP 的初始化开销 + 包体积膨胀 + 兼容性维护成本要 GPU 给出明显收益才划算)

**scope**:中大。

- spike + 决策:半天到一天
- 实现(若 spike 通过):pyproject.toml extras + `Qwen3ONNXRunner` 加 providers 参数 + `Qwen3ASRSTT.load()` 按 variant + 设备能力选 EP + 设置页显示当前 EP + 一致性测试 + extras 安装文档,~200-300 行
- 优先级**中** —— 1.7B 在 CPU 上仍然能跑(只是体感不顺),用户不点 1.7B 不影响主流程;36 轮已上线"可视化下载"让 1.7B 切换体验改善了一档,GPU 后端可以再观察一阵

---

## 已完成 / 不再追踪

这一段记录从早前 SUMMARY "后续 TODO" 里**刻意移除**的条目，避免未来自己或后续 agent 翻老 SUMMARY 发现"为什么这条没做"，误以为是遗漏：

- **首次模型下载进度 UI**（14 轮 SUMMARY 局限性 #3）—— 实测下载速度已经够快（ModelScope 国内 CDN 秒级），用户痛点不明显，不值得做
- **Linux 实机验证**（14 轮 SUMMARY 局限性 #4）—— 已在干净 Ubuntu 上手动验证通过
- **跨平台 Pythonic overlay 统一代码**（16 轮遗留）—— 视觉已在 16 轮对齐（微信输入法风格深蓝药丸），双份原生实现（GTK3+Cairo / AppKit）维持现状。Tkinter 与 pystray 主线程冲突、子进程方案引入退出清理复杂度，真要统一得换 Tauri 这类方案全面接管 UI 层，不是 overlay 一个模块的事，当前版本满意，不再追
- **录音时实时检测麦克风离线 - macOS 替代 query_devices**（32 轮遗留 A）—— 32 轮 macOS 仍走 `sd.query_devices`，MacBook（内置麦永远在）主流场景可靠；Mac mini / Studio / Pro 等无内置麦的桌面机用户拔 USB 麦后 CoreAudio 会留 `CADefaultDeviceAggregate-xxxx-x` 占位设备 → probe 通过 → 录到 0 字节 → 幻觉 token。触发面太窄（无内置麦桌面 Mac + 拔外接麦 + 立刻按热键），等真有 Mac mini / Studio 用户报问题再做
- **录音时实时检测麦克风离线 - 录音中途断开监控**（32 轮遗留 B）—— 32 轮的 callback 连续 5 次 `input_overflow` 升级 device_lost 在 PipeWire 上完全失效（拔麦后 PipeWire 给的是干净静音流，无任何 status flag）。触发面只有"按住热键说话过程中精准拔麦"那一句，下一次按热键时 32 轮的 pactl probe 会兜底，影响面就一句话，不值得为它再加一条 daemon 线程
