# 需求:Qwen3-ASR 真·流式识别

## 背景

第 26 轮完成了"SenseVoice → Qwen3-ASR"的模型替换,0.6B 和 1.7B 两款模型
都以**离线模式**运行——按住热键说话,松开后一次推理、一次 paste。

但我们真正想要的产品形态是"**边说边出字**"的流式识别。Qwen3-ASR 是标准
的 encoder-decoder 自回归 transformer 架构,天然具备流式识别的技术基础
(不像 SenseVoice 那种 CTC 非自回归架构做不了真流式)。本轮就要把这个
能力落地。

**前置知识**:流式 ASR 的基础概念、encoder-decoder vs CTC 架构差异、
chunked encoder + rollback decoder 的算法思路,详见第 26 轮的
`BACKGROUND.md`。本 PROMPT 假设读者已经看过那份文档。

## 本轮目标

### 核心目标

1. **实现真·输入流式识别**:用户按住热键说话时,每 ~2 秒就有新字开始出
   现在焦点窗口,不是松手后才一次性粘贴
2. **流式 + 离线两种模式共存**:用户在设置页可以选"流式"或"离线"
   (默认流式)。即使选流式,松手后的最后一段仍然要 flush 干净
3. **0.6B 和 1.7B 都支持流式**(同一套代码,跟第 26 轮的多 variant 策略
   一致)
4. **流式输出永远是追加、不擦改**——跟第 1 轮失败的 BackSpace 擦写路线
   划清界线

### 非目标(本轮明确不做)

- **hotwords / prompt biasing**:留后续轮次。流式状态机里**可以**预留
  `hotwords` 参数传到 prompt 构建,但本轮不暴露到 UI
- **长音频滑动窗口淘汰**(encoder 窗口 eviction + decoder prefix
  capping):本轮不做。单次按住热键 < 60 秒的场景不会触发。**如果**踩到
  KV cache overflow,临时抛 exception 提示用户分段说,不假装支持

## 硬约束

1. **流式识别必须是真·输入流**,不是"每次把累积 buffer 从头重跑 + token
   callback"的伪流式(Wasser1462 的 `streaming_qwen3_asr` 就是这种伪流
   式,不能用)
2. **真流式必须基于 chunked encoder + decoder rollback 机制实现**。
   encoder 每次只对新音频 chunk 做增量计算,decoder KV cache 跨 chunk
   复用,只 rollback 尾部 N 个 token
3. **不新增 runtime 依赖**。流式状态机用 numpy + onnxruntime(已有)
   实现
4. **测试覆盖率不能退化**。第 26 轮把整体覆盖率拉到 ≥ 70%,本轮收尾时
   仍然 ≥ 70%

## 主要设计方向(待 26 轮落地后再起草详细 PLAN)

**参数默认值**(可调):

- `chunk_size_sec = 2.0`
- `rollback_tokens = 5`
- `max_new_tokens_per_chunk = 32`

**状态机骨架**:

- 按下热键 → 初始化 `StreamState`(清空 buffer / KV cache / committed
  tokens)
- 录音器按 320ms 粒度往状态机回调新 chunk
- 累积到 2s 才触发一次流式步:conv + encoder 增量 → 扩展 audio_features
  → decoder 以 "committed + rollback" 为起点自回归 → 稳定前缀 commit →
  新增 committed_delta 追加 paste
- 松开热键 → 触发最后一次流式步(is_last=True),所有未 commit 的 token
  全部 flush

**BaseSTT 接口扩展**:

```python
class BaseSTT(ABC):
    supports_streaming: ClassVar[bool] = False

    def init_stream_state(self) -> Any: ...
    def stream_step(
        self, audio: np.ndarray, state: Any, is_last: bool
    ) -> StreamEvent: ...
```

`Qwen3ASRSTT.supports_streaming = True`;`transcribe()` 保留作为离线
fallback。

**录音器改造**:

`AudioRecorder` 增加 `start_streaming(on_chunk)` / `stop_streaming()`
API,走 sounddevice `InputStream(callback=...)` 的流式接口。

**设置页新增**:

"流式识别"开关(boolean toggle),独立于第 26 轮的 "识别模型" 下拉。

## 验收标准

### 功能正确性

- [ ] 按住热键说长段话(≥10 秒),说话过程中每 ~2 秒有新字开始出现
- [ ] 松开热键时最后一批 token 完成 flush,全部文本最终粘贴正确
- [ ] 流式模式下识别质量跟离线模式**基本一致**(不能有显著退化)
- [ ] 流式模式下 CPU 占用稳定,不是 O(N²) 爬升(连续说 30 秒不卡顿)
- [ ] 流式和离线模式切换无需重启应用
- [ ] 0.6B 和 1.7B 都能流式运行
- [ ] 流式输出永远是追加,不会擦除前面已粘贴的内容

### 代码质量

- [ ] `BaseSTT` 正确扩展,接口设计对未来引擎友好
- [ ] 流式状态机有充分的单元测试(mock ONNX session,只测状态机逻辑)
- [ ] 整体覆盖率保持 ≥ 70%
- [ ] 端到端 smoke test:跑一段长音频,流式和离线输出对齐(容忍尾部
      rollback 带来的微小差异)

## 局限性(预期本轮承认不解决)

1. **超长音频(>60 秒连续说话)不支持**:KV cache overflow 会抛异常
2. **流式标点可能跟离线有差异**:encoder-decoder 模型流式时每个 chunk
   的标点决策是基于部分上下文,跟离线看到全部音频再决策可能略有不同。
   **接受微小差异,不专门做后处理对齐**
3. **rollback 参数可能需要针对不同说话速度调**:默认值在大多数场景下
   应该够用,极端场景不专门优化

## 后续 TODO(同步 BACKLOG.md)

- **长音频滑动窗口**:支持 > 60s 连续说话(独立轮次)
- **流式 rollback 参数自适应**:根据说话节奏动态调整 `rollback_tokens`
- **流式 preview**:把尾部未稳定的 `pending_tokens` 以灰色浮窗显示
  给用户"正在识别..."的反馈(可选优化)

---

**注**:本 PROMPT 是骨架性质。详细的 PLAN.md 等到第 26 轮全部落地之后
再起草 —— 因为流式状态机的很多设计细节依赖第 26 轮的实际代码结构
(`_onnx_runner.py` 暴露的接口、`Qwen3ASRSTT` 的生命周期管理方式等)。
过早冻结 PLAN 会浪费工作量。
