# 第 28 轮 PLAN —— Qwen3-ASR 真·流式识别

## Context —— 为什么要做

- **现状痛点**:26 轮 Qwen3-ASR 上线后识别质量跃迁,但按住热键 → 松手 →
  粘贴是离线 batch 模式,10s 左右的中文语音在 Apple Silicon CPU 上松手后 ~2s
  才出字(encoder 对 pad_or_trim 到 30s 的 mel 一次前向 ~1s,decoder 30-40
  步贪心 ~200-400ms)。这跟"输入法"的体感差距很大。
- **技术前置条件已经齐备**:26 轮迁移 Qwen3-ASR 时刻意为流式留好了地基——
  `_onnx_runner.py` 用绝对位置 `cache_position` + 固定大小 KV cache buffer
  (`max_total_len=1200`,28 层,shape `(1, 1200, 8, 128)`),
  `decoder_step(input_ids, audio_features, caches, cur_len)` 把 KV 原位写回;
  而且 `audio_features` 是每次调用时传入的外部张量,**不在 decoder KV cache 里**
  (decoder 只缓存 self-attn 的 KV;cross-attn 的 K/V 每次从 audio_features
  重算),这让"逐 chunk 扩 audio_features 再继续生成"这个骨架在协议层本来就通。
- **架构面天然合适**:Qwen3-ASR 是 encoder-decoder 自回归结构,跟 SenseVoice
  的 encoder-only CTC 不同,天然支持 token 级流式生成(详见
  `docs/26-Qwen3-ASR替换SenseVoice/BACKGROUND.md` 的"流式 ASR 的两种策略"章节)。
- **预期产出**:按住热键说长段话时每 ~2s 有新字开始出现在焦点窗口,松手时最后
  一段 flush 干净,**追加式**粘贴(跟第 1 轮失败的 BackSpace 擦写路线彻底划
  清)。0.6B / 1.7B 一套代码都支持;流式 / 离线两种模式用户可切换(默认流
  式);60s 内的连续说话不要崩。

## 硬设计决策 —— 为什么需要先 spike

本轮绕不开的技术问题:decoder 的每个 token 都过 self-attn + cross-attn,
**self-attn KV cache 里的 hidden state 依赖当时的 cross-attn 输出(即当时的
audio_features)**。流式下 audio_features 会随 chunk 增长,已缓存的 self-attn
KV 跟最新 audio_features 不一致。关键放大效应:decoder 是 28 层堆叠,audio_pad
位置的 KV 如果在"零 audio_features"下 prefill,层层传递后是个"纯
zero-conditioned 深度特征",跟真 audio 对应的表示差得远;生成期新 text token
的 self-attn 回看这段 prompt KV 时 attention 分布会扭曲。救命稻草是 cross-attn
每次从 current audio_features 重算(text token 自己看得到真 audio),但 prompt
区污染仍可能导致 WER 退化 10-40%,实测才知道。

三条候选路径:

| 策略 | KV 复用 | 编码成本 | 质量风险 |
|------|--------|---------|---------|
| **A. 预分配满长度零 buffer,prompt 一次 prefill,后续只填 buffer 不重 prefill** | 全复用 | O(N) 总 | 高:prompt 区 audio_pad 的 self-attn KV 对零 features 算,可能系统性前缀偏置 |
| **B. 每 chunk 用真实长度重建 prompt,从头 prefill decoder** | 只复用生成文本的 KV | O(K²) 累计 prefill | 零 staleness |
| **E. Prefix-cached re-prefill(chat template 前 ~6 token KV 永久缓存,audio_pad + 后缀 + 已 commit 文本每 chunk 重 prefill)** | 固定前缀 + 每 chunk 批量重 prefill 中段 | batched prefill 比逐 token 快 ~10-20×,单 chunk 成本虽然随 K 线性增长,累计仍比 A + B 的坏 case 好 | 零 staleness |

(讨论过的"插入式增量 prompt"和"固定 pad 数 / 可变 audio_features"都因跟
ONNX 绝对位置寻址和 Qwen3-ASR 训练时 pad:audio_features 1:1 对齐不兼容而砍掉。)

**本轮选型路径**:
- 第 0 阶段 spike **同时实现 A 和 E**(B 作为朴素 baseline),对比
- 若策略 A 的 "prefix stability rate ≥ 92%" 且 "prompt-区 KV 相对离线 cosine ≥ 0.85",
  **选 A**(成本最优)
- 否则**选 E**(质量零损失,成本可控)
- B 只作为 spike 对照,不进正式代码路径

**PROMPT 里禁止的"伪流式"特指"每 chunk 从头重跑 encoder + 从头 decode"**,
跟 B / E 都不同 —— B / E 的 encoder 仍然只对新 chunk 增量,decoder 的 prompt
区虽然重 prefill 但是批量一次性的(不是逐 token),且生成阶段用到的 rollback
机制正常。SUMMARY.md 要把"为什么这不算伪流式"讲清楚避免误读。

---

## 实施阶段

### 第 0 阶段 —— Spike(一次性脚本,不入库)

**产出**:`scripts/spike_qwen3_streaming.py`(写完贴到 SUMMARY 附录后删)。

**实现三条路径**(其中 B 仅作对照,不进正式代码):

1. **离线 baseline**:直接跑 `Qwen3ASRSTT.transcribe()`
2. **Path A**:预分配 `audio_features_buffer = zeros((1, N_MAX=800, 1024))`,
   prompt 一次 prefill,后续 chunk 用 encoder 编码新音频(~23 audio token)填
   入 buffer 的下一个 slice,调 decoder_step 继续生成
3. **Path E**:prompt 分两段,`[chat prefix ~6 token]` + `[audio_pad * N_k +
   audio_end + im_end + assistant_start + committed_text]`。每 chunk 保留前缀
   KV,从后缀起重做 prefill(batched,传当前真实长度的 audio_features),然后
   继续生成
4. **Path B**(对照):每 chunk 从 cur_len=0 重做整个 prompt + committed 的 prefill

**Spike 测试的 5 个指标**(对 `tests/fixtures/zh.wav` 10.6s 的出师表样本):

| 指标 | 定义 | 接受阈值 |
|------|------|---------|
| **Final WER** | Path 最终 transcript 跟 baseline 的字级编辑距离 | ≤ 5% |
| **Prefix stability rate** | 一次 committed 的 token 在后续 chunk 不被事后改写的比例(衡量"提前 commit 错不错") | ≥ 92% |
| **Prompt-区 KV cosine** | A / E 的 prompt 区 self-attn KV 跟 baseline 的 prompt 区 KV 做 cosine similarity | ≥ 0.85(能提前识别 staleness 是否语义级污染) |
| **First-token latency per chunk** | 每次 stream_step 从接到 chunk 到 emit 第 1 个 token 的墙钟时间 | A 稳定不增长;E 可线性增长但 chunk 10 不超过 500ms |
| **Rollback 命中率** | pending 5/8/10 的 token 在下一 chunk 被改写的比例 | 20%–60%(超出意味着 rollback 窗口错位) |

**判定规则**:
- Path A 的 Prefix stability ≥ 92% AND KV cosine ≥ 0.85 → **选 A**
- 否则 **选 E**
- Path B 不进正式代码,仅用于"最坏情况下选 E 是否真的能 recover 质量"的交叉验证

**不测**:RTF、CPU 占用、长音频(> 15s)——这些在正式实现后补测。spike 只
解决"选 A 还是选 E"这个单点决策。

**Spike 决策**完成后 SUMMARY.md 的"关键设计"章节要记录实测数据和选型理由。

### 第 1 阶段 —— BaseSTT 接口扩展

**改动文件**:`src/whisper_input/stt/base.py`

```python
class BaseSTT(ABC):
    supports_streaming: ClassVar[bool] = False

    @abstractmethod
    def load(self) -> None: ...
    @abstractmethod
    def transcribe(self, wav_data: bytes) -> str: ...

    def init_stream_state(self) -> Any:
        raise NotImplementedError
    def stream_step(
        self, audio_chunk: np.ndarray, state: Any, is_last: bool
    ) -> StreamEvent:
        raise NotImplementedError


@dataclass
class StreamEvent:
    committed_delta: str     # 本步新提交的文本增量(用来 paste)
    pending_text: str        # 本步生成的尾部未稳定文本(可选用,本轮不展示)
    is_final: bool           # is_last=True 时为 True
```

state 刻意不声明成 dataclass —— 它是引擎私有,用 `Any` 留开。

### 第 2 阶段 —— Qwen3ASRSTT 流式实现

**改动文件**:
- 新建 `src/whisper_input/stt/qwen3/_stream.py` —— 状态机纯逻辑
- `src/whisper_input/stt/qwen3/qwen3_asr.py` —— 接 init_stream_state / stream_step

**`_stream.py` 关键 API**:
```python
@dataclass
class Qwen3StreamState:
    audio_buffer: np.ndarray
    encoded_until_sample: int
    audio_features: np.ndarray     # shape (1, A, 1024)
    audio_features_len: int        # 当前有效长度(Path A 用)
    caches: list[np.ndarray]       # KV cache
    cur_len: int
    prompt_ids: list[int]
    committed_tokens: list[int]
    pending_tokens: list[int]
    committed_text: str            # 累积 decode 的文本(avoid byte-boundary bug)
    step_count: int
```

**核心算法**(A / E 的差异点并列):
```
1. 把 audio_chunk 追加到 state.audio_buffer
2. 若累积未编码部分 < chunk_size_sec 且 not is_last → 原地返回空 StreamEvent
3. 否则:
   a. 从 audio_buffer 切出"下一个未编码的 chunk" (2.0s 或剩余)
   b. mel = log_mel_spectrogram(chunk) —— chunk 长度 < 30s,不要 pad_or_trim 到 30s
   c. new_audio_features = runner.encode_audio(mel)  # shape (1, n_new, 1024)
   d. 更新 audio_features:
      (A) 拷贝到预分配 buffer 的下一个 slice,更新 audio_features_len
      (E) 把 new_audio_features 拼接到当前 audio_features 尾部
   e. Decoder rollback:
      - kept_prefix = committed_tokens + pending_tokens[:-rollback_N]
      - rolled_back = pending_tokens[-rollback_N:]  # 要重新生成
      (A) 重置 cur_len = len(prompt_ids) + len(kept_prefix);committed 的 KV 已在
          cache 里,直接复用
      (E) 重置 cur_len = len(chat_prefix_ids);以 [audio_pad * audio_features_len
          + chat_suffix_ids + kept_prefix] 为 input_ids 做一次 batched prefill,
          随后继续生成
   f. 自回归生成 up to max_new_tokens_per_chunk:
      - 每次喂 1 个 token,调 decoder_step,更新 cur_len
      - 遇 <|im_end|> 停(is_last 时生成完 stream 也结束)
      - cross-attn 输入永远是当前的 audio_features[:, :audio_features_len, :]
   g. 生成的 tokens 分成 committed_delta + new_pending
      (is_last=True 时全部进 committed_delta,pending = [])
   h. 更新 committed_text:维护 running tokenizer.decode(committed_tokens),
      diff 出 committed_delta_text 避免跨 token 字节边界 bug
   i. 返回 StreamEvent(committed_delta=..., pending_text=..., is_final=is_last)
```

**`qwen3_asr.py` 改动**:
- 类属性 `supports_streaming: ClassVar[bool] = True`
- `init_stream_state() -> Qwen3StreamState`:按 spike 选中的策略分配
- `stream_step(...)` 直接调 `_stream.py` 里的函数
- `transcribe()` 保留原逻辑作为离线 fallback
- KV overflow:`_onnx_runner.decoder_step` 抛 `RuntimeError` → `_stream.py` 外层
  捕获 → raise `StreamingKVOverflowError`

**Rollback 参数**(代码里 const,不暴露 UI):
- `CHUNK_SIZE_SEC = 2.0`
- `ROLLBACK_TOKENS = 10`(中文 + BPE 边界 + 模型 decision 延迟下,5 不够;10 只
  增 ~15% 成本)
- `MAX_NEW_TOKENS_PER_CHUNK = 32`

### 第 3 阶段 —— AudioRecorder 扩展

**改动文件**:`src/whisper_input/recorder.py`

新增并列方法,不改原 start/stop:
```python
def start_streaming(self, on_chunk: Callable[[np.ndarray], None]) -> None:
    """开始流式录音。每次 sd callback 触发时,把 indata(float32)回调给 on_chunk。"""

def stop_streaming(self) -> None:
    """停止流式录音。"""
```

- 复用现有 `_stream` / `_lock` / `_recording`,新增 `_on_chunk_cb` 字段
- Callback 先算 RMS 调 on_level(音量浮窗),再判断流式 / 累积分路
- Callback 内必须 lightweight,耗时工作扔给 WhisperInput 的 worker

### 第 4 阶段 —— WhisperInput 编排

**改动文件**:`src/whisper_input/__main__.py`

**流程**:
```
on_key_press (流式)              on_key_release (流式)
  stt.init_stream_state()        recorder.stop_streaming()
  self._stream_state = ...       enqueue 最后一次 stream_step(remaining, is_last=True)
  recorder.start_streaming(cb)   paste flush 的 committed_delta
                                 self._stream_state = None

on_chunk (在 sd callback 线程)
  append 到 self._chunk_accumulator(lock)
  if accumulator ≥ 2s * 16000:
      swap accumulator,enqueue _do_stream_step 到 worker
```

**worker 任务**:
```python
def _do_stream_step(audio_chunk, is_last):
    evt = self.stt.stream_step(audio_chunk, self._stream_state, is_last)
    if evt.committed_delta:
        type_text(evt.committed_delta)
    if evt.is_final:
        self._notify_status("ready")
```

**模式切换**:`on_config_changed` 里 `qwen3.streaming_mode` 只更新
`self.streaming_mode` 标志位,不中断正在进行的录音。`_do_key_press` /
`_do_key_release` 按当前标志位分路。

**KV overflow 友好处理**:stream_step 里抛 `StreamingKVOverflowError`:
1. worker 捕获 → `type_text(已 committed 的残余)`(若有)
2. 弹 toast(i18n `main.streaming_overflow`)
3. reset `self._stream_state = None`

**接近上限提示**:累计 audio ≥ `WARN_DURATION_SEC = 28` 时浮窗切 "已接近识别
上限"(i18n `main.streaming_near_limit`),不强制停,让用户决定。

### 第 5 阶段 —— 配置 + 设置页 + i18n

**改动文件**:
- `src/whisper_input/config_manager.py` — `DEFAULT_CONFIG["qwen3"]` 加
  `"streaming_mode": True`(默认开启)。`_deep_merge` 自动补默认值,无需 migration
- `src/whisper_input/assets/settings.html` — 照抄 `sound_enabled` 的 toggle 结构,
  `id="qwen3_streaming_mode"`,change 事件 `saveSetting('qwen3.streaming_mode',
  this.checked)`,立即生效不加入 RESTART_KEYS
- `src/whisper_input/assets/locales/{zh,en,fr}.json` — 各加 4 条:
  - `settings.streaming_mode`
  - `settings.streaming_mode_desc`
  - `main.streaming_overflow`
  - `main.streaming_near_limit`
- `__main__.py` `on_config_changed` 照 `sound.enabled` 加一个分支

### 第 6 阶段 —— 测试

**新增**:

1. `tests/test_qwen3_stream.py` — 状态机纯逻辑,**mock ONNX runner**(参照
   `test_qwen3_asr.py:115-173` 的 monkeypatch 风格),目标 `_stream.py` 100%
   覆盖(rollback / 空 chunk / is_last flush / KV overflow 分支)
2. `tests/test_qwen3_stream_smoke.py` — 流式 vs 离线 端到端对比,**真 ONNX**
   (`qwen3_0_6b_model_dir` fixture,没缓存就 skip),编辑距离 ≤ 5%
3. `tests/test_recorder_streaming.py` — 参照 `test_main_shutdown.py:20-47` 的
   `types.ModuleType("sounddevice")` 注入,手动触发 fake callback
4. `tests/test_main_streaming.py` — 参照 `test_main_stt_switch.py` 的 Event 驱
   动,fake STT + mock recorder.start_streaming + mock type_text,手动驱动
   "送 3 chunk,松手"的剧本

**扩展**:
- `test_config_manager.py`:默认 `qwen3.streaming_mode=True`
- `test_stt_factory.py`:`Qwen3ASRSTT.supports_streaming=True`
- `test_settings_server.py`:POST /api/config 带 qwen3.streaming_mode 的 roundtrip

**覆盖率**:整体 ≥ 70%;`_stream.py` 100%;`qwen3_asr.py` 流式新方法 ≥ 90%

### 第 7 阶段 —— 文档

- `docs/28-Qwen3-ASR流式识别/SUMMARY.md`(结束后):实测 RTF / 延迟数据、
  spike 选型结论 + 证据、代码行数 + 覆盖率变化、局限性 / 后续 TODO 同步到
  `BACKLOG.md`(并从 BACKLOG 删掉"第 28 轮主线"条目)

---

## 关键文件清单

### 会改动的(已有)
| 文件 | 改动性质 |
|------|---------|
| `src/whisper_input/stt/base.py` | 加 supports_streaming / init_stream_state / stream_step / StreamEvent |
| `src/whisper_input/stt/qwen3/qwen3_asr.py` | 加流式入口,offline transcribe 保留 |
| `src/whisper_input/recorder.py` | 加 start_streaming / stop_streaming |
| `src/whisper_input/__main__.py` | WhisperInput 按 streaming_mode 分路 + chunk accumulator + KV overflow 处理 |
| `src/whisper_input/config_manager.py` | DEFAULT_CONFIG 加 `qwen3.streaming_mode` |
| `src/whisper_input/assets/settings.html` | 新增 toggle |
| `src/whisper_input/assets/locales/{zh,en,fr}.json` | 4 条新 i18n key × 3 语言 = 12 条字符串 |

### 新增的
| 文件 | 作用 |
|------|------|
| `src/whisper_input/stt/qwen3/_stream.py` | 流式状态机纯逻辑 |
| `tests/test_qwen3_stream.py` | 状态机单测 (mock ONNX) |
| `tests/test_qwen3_stream_smoke.py` | 流式 vs 离线端到端对比 (真 ONNX) |
| `tests/test_recorder_streaming.py` | start_streaming 单测 |
| `tests/test_main_streaming.py` | WhisperInput 流式编排集成测试 |
| `scripts/spike_qwen3_streaming.py` | spike 脚本,结束后删除 |
| `docs/28-Qwen3-ASR流式识别/SUMMARY.md` | 结束后补 |

### 不改动的
- `_onnx_runner.py` / `_feature.py` / `_prompt.py` / `_tokenizer.py` /
  `_postprocess.py` / `_downloader.py`

---

## 验收

**功能正确性**:
- [ ] 按住热键说 ≥10s 长段话,过程中每 ~2s 有新字出现(手动)
- [ ] 松手最后一批 token flush 正确(手动)
- [ ] 流式 vs 离线最终文本一致(test_qwen3_stream_smoke.py)
- [ ] CPU 占用不 O(N²)(手动)
- [ ] 流式 / 离线切换无需重启(手动)
- [ ] 0.6B / 1.7B 都能流式(手动)
- [ ] 追加不擦改(算法保证)

**代码质量**:
- [ ] BaseSTT 接口合理(review)
- [ ] `_stream.py` 100% 覆盖
- [ ] 整体 ≥ 70%
- [ ] 端到端 smoke 通过

---

## 本轮不做(PROMPT 非目标)

- hotwords / prompt biasing
- encoder 滑动窗口淘汰 / prefix capping
- 流式 preview(pending 灰色浮窗)
- rollback_tokens 自适应

## 局限性(承认不解决)

1. **超长音频 > ~33s (A) / ~38s (E) 不支持**,KV overflow 抛异常;28s 浮窗提示
2. **流式标点可能跟离线略有差异**,接受,不做对齐
3. **按住录音中改设置不生效**,要下一次按键才切换
4. **首 chunk 冷启动延迟 1–1.5s**,prefill 优化是 BACKLOG 事

## 后续 TODO(同步 BACKLOG.md)

- 长音频滑动窗口
- 流式 preview(pending 灰色浮窗)
- rollback 参数自适应

---

## 工作量估计

| 阶段 | 规模 | 含测试 |
|------|------|-------|
| 0. Spike | 半天 | - |
| 1. BaseSTT 接口 | ~30 行 | +20 行测试 |
| 2. Qwen3 流式 | ~200 行 | +150 行测试 |
| 3. Recorder | ~40 行 | +50 行测试 |
| 4. WhisperInput | ~100 行 | +100 行集成测试 |
| 5. 设置页 + i18n | ~30 行 + 12 i18n 字符串 | +10 行测试 |
| 6. 文档 | - | - |
| **合计** | **~400 行代码** | **~330 行测试** |

一周左右的工作量(含 spike + 手动验证)。
