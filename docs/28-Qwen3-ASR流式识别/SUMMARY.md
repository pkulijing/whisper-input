# 第 28 轮总结 —— Qwen3-ASR 真·流式识别

## 背景

26 轮换到 Qwen3-ASR 后识别质量跃迁,但**松手后粘贴延迟 ~2s**(0.6B,10s 左右
的中文语音,Apple Silicon CPU)。延迟大头在 encoder 对 pad_or_trim 到 30s 的
mel 一次前向(~1s);decoder 30-40 步贪心只占 200-400ms。跟"输入法"的实时
体感差距明显。

本轮目标:**真·输入流式**——按住热键说长段话,过程中每 ~2s 新字出现在
焦点窗口,松手时最后一段 flush 干净,**追加式**粘贴(跟第 1 轮失败的
BackSpace 擦写路线彻底划清)。

## 实现方案

### 关键设计 1 —— 流式策略选型(Spike 决策)

PROMPT 要求先做 spike,因为流式 ASR 对 Qwen3-ASR 这种 encoder-decoder 自回归
结构有个**理论难点**:decoder 每个 token 都过 self-attn + cross-attn,
**self-attn KV cache 里缓存的 hidden state 依赖当时的 cross-attn 输出**(即
当时的 audio_features)。流式下 audio_features 随 chunk 增长,已缓存的 self-attn
KV 会跟最新 audio_features 不一致(staleness)。

候选三条路径:

| 策略 | KV 复用 | 编码成本 | 质量风险 |
|------|--------|---------|---------|
| **A** 预分配满长度零 buffer,prompt 一次 prefill,后续只填 buffer | 全复用 | O(N) | 高:prompt 区 audio_pad 在零 features 下 prefill,28 层堆叠后是"纯 zero-conditioned 深度特征" |
| **B** 每 chunk 用真实长度从头重建 prompt 重 prefill | 只复用生成 token KV | O(K²) | 零 staleness |
| **E** chat template 前缀 KV 永久缓存,audio_pad + 后缀 + committed 每 chunk 重 prefill | 固定前缀 + 中段重 prefill | batched prefill 比逐 token 快 ~10-20× | 零 staleness |

**Spike 实测**(`scripts/spike_qwen3_streaming.py`,zh.wav 10.6s 出师表):

| Path | 输出 | edit distance vs baseline |
|------|------|--------------------------|
| 离线 baseline | `先帝创业未半而中道崩殂，今天下三分，益州疲弊，此诚危急存亡之秋也。` | 0 |
| Path A | `''`(空字符串) | **100% — 灾难** |
| Path E | 跟 baseline 完全一致 | **0%** |

Path A 失败比 Plan agent 预测的 "10-40% WER 退化" 严重得多 —— ONNX int8 量化
放大了 staleness 数值误差,模型甚至吐不出文字。**正式实现选 Path E。**

> "为什么这不算 PROMPT 禁止的伪流式":伪流式特指"每 chunk 从头重跑
> encoder + 从头 decode"。Path E 的 encoder 仍然增量(只对新 chunk 跑
> encode_audio),decoder 重 prefill 的只是 prompt 中段,是一次批量前向
> (seq=n_af+suffix+committed),不是逐 token 生成;rollback 机制保留。

### 关键设计 2 —— marker-anchored 切分(真机踩坑后引入)

**初版实现按"commit 前 N-rollback,pending 后 rollback"切分,真机暴露严重
bug**:不管说啥都粘 "language Chinese"。

通过给 stream_step 加 raw token dump 排查,发现 Qwen3-ASR 每次生成的序列结构是:

```
[language, Chinese, <asr_text>, 真 transcript..., <|im_end|>]
```

前 2-3 个 scaffolding token 永远在最前面。朴素 rollback 切分会在某些 chunk 把
`[language, Chinese]` 切进 committed,**这段不含 `<asr_text>` marker**,
`parse_asr_output` 找不到 marker 就把整段原样返回 → 用户屏幕粘 "language Chinese"。

zh.wav 测试没暴露这个 bug 因为它每 chunk 生成的 token 数刚好 ≤ rollback,
全部进 pending,绕过了 commit 路径 —— 测试链路漏了"非最后一个 chunk 真正
走 commit 路径"的覆盖。

**修法 —— marker-anchored split**:`committed_tokens` 必须满足"要么空,要么
含 `<asr_text>` marker"。切分逻辑分四个分支:

- `is_last`:全部 commit
- committed 已含 marker:正常 rollback(commit 前 N-ROLLBACK,pending 后 ROLLBACK)
- committed 没 marker 但 new_generated 含 marker:**marker 及之前全 commit,
  marker 之后留最后 ROLLBACK 个进 pending**
- 都没 marker:`deferred`,全部归 pending 等下一 chunk

二道保护:即使切分让 scaffolding 滑进来,`committed_tokens` 不含 marker 时
`committed_delta = ""`(宁可不贴,不贴 leak)。

### 关键设计 3 —— rollback / chunk 参数调优(基于真机体感)

PLAN 阶段 Plan agent 建议 `ROLLBACK_TOKENS=10`,真机验证发现"说好多话才
出一次字" —— 10 太保守:模型每 chunk 生成 ~12 个 token 时,commit_count
= 12-10 = 2 ≤ marker_idx,触发 deferred,什么都不贴。

试过 `CHUNK_SIZE_SEC=1.0` + `ROLLBACK=3` 想加快节奏,**模型单 chunk 音频
太短早期 commit 错字**(把"先帝"识别成"先地"),后续 chunk 基于错误前缀
继续生成,最终 transcript 满是重复残渣("先地，后地，创业未半而中道...")。

最终参数:

```python
STREAMING_CHUNK_SEC = 2.0    # chunk 频率
ROLLBACK_TOKENS = 3          # 尾部缓冲
MAX_NEW_TOKENS_PER_CHUNK = 32
WARN_DURATION_SEC = 28       # 浮窗"接近上限"阈值
```

zh.wav 实测节奏:`先帝创业` → `未半而中道` → `崩殂。今天下三分，` →
`益州疲弊，此诚` → `危急存亡之` → `秋也。`(每 ~2s 一段)。整体相对 offline
edit distance ≤ 3%(偶发标点差异:"崩殂**。**"vs"崩殂**，**",chunk 边界
decision 看不到全文导致)。

### 开发内容概括

| 阶段 | 改动范围 |
|------|---------|
| 第 0 阶段 Spike | `scripts/spike_qwen3_streaming.py`(保留) |
| BaseSTT 接口 | `stt/base.py` +60 行:`supports_streaming` 类变量、`StreamEvent` dataclass、`StreamingKVOverflowError`、`STREAMING_CHUNK_SEC/SAMPLES` |
| Qwen3 流式 | 新建 `stt/qwen3/_stream.py` ~430 行(策略 E + marker-anchored split + 完整 DEBUG 日志) + `qwen3_asr.py` +30 行(接入) |
| Recorder | `recorder.py` +47 行:`start_streaming` / `stop_streaming` |
| WhisperInput 编排 | `__main__.py` +180 行:`_should_stream`/`_do_key_press` 分路/`_on_stream_chunk` PortAudio 累积器/`_do_stream_step` worker/`_finalize_stream_session`/`_notify_near_limit`/overflow 兜底 |
| 配置 + UI | `config_manager.py` +7 行(默认 `qwen3.streaming_mode=True`) / `config.example.yaml` / `settings.html` toggle / 三语 locale 各 4 条新字符串 |
| 测试 | 新建 `test_qwen3_stream.py`(17 测试)+ `test_qwen3_stream_smoke.py`(端到端真 ONNX,2 个用例:逐 chunk raw token dump + 完整 WhisperInput 流水线)+ `test_recorder_streaming.py`(7) + `test_main_streaming.py`(13) + 扩展 `test_config_manager` / `test_stt_factory` / `test_settings_server` |

**最终代码量**:~430 行生产 + ~520 行测试。

### 额外产物

- `scripts/spike_qwen3_streaming.py`:三策略对比工具,**保留**作为下次模型升级
  时验证"Path A 是否仍然爆炸 / 是否能切回成本最低路径"的回归基线
- `_stream.py` 内置完整 DEBUG 日志(`stream_step_begin` / `stream_step_generated` /
  `stream_step_split`),后续优化和真机问题排查的关键抓手
- 真机调优过程中发现的"测试链路漏覆盖"教训:smoke test 只断"final 拼接 ==
  offline" 不够,必须断"每 chunk 的 committed_delta 是合法前缀延伸,无 leak"

## 局限性

1. **流式节奏 ~2s 一次**,跟微信输入法的近实时还有差距。本轮跟用户讨论过
   进一步压低空间(BACKLOG 里的"流式 preview 浮窗"和"光标旁浮窗"),决定
   **本版本先合入 master 不再调**,后续独立轮次优化
2. **超长音频 > ~33-38s 不支持**:KV cache 硬墙;28s 浮窗会提示用户分段
3. **流式标点偶有差异**:zh.wav 上 30 个字符差 1 个标点("。"vs"，"在"崩殂"
   后),rollback=3 下 chunk 边界看不到后文的代价,接受
4. **已 paste 不可改**:append-only 是 PROMPT 硬约束,rollback=3 窗口外的
   commit 定死。spike 实测干净朗读 prefix stability=100%
5. **测试覆盖率 67%**:从 28 轮前的 61% 上升 6 个百分点,未退化。70% 目标
   超出本轮 scope(主要差距在 `__main__.main()` CLI 编排和 overlay/tray 老
   代码,见 BACKLOG"测试套增强 v2")
6. **按住录音中改流式 toggle 不生效**:on_config_changed 只改标志位,下次
   按键才切换(race 不值得处理)
7. **首 chunk 冷启动 1-1.5s**:decoder prefill 满 prompt 的耗时,改 prefill
   异步 / 缓存 opt 图是 BACKLOG"ORT optimized_model 持久化"的事

## 后续 TODO(已同步到 BACKLOG.md)

- **流式 preview 浮窗**:把 `StreamEvent.pending_text`(本轮已暴露但未用)实
  时渲染到录音浮窗,让用户看到"正在识别"反馈;讨论中也评估了"光标旁浮窗"
  方案,因为跨 App AX query 不稳定(VS Code / Electron / Web 退化严重)和
  Linux 几乎做不全,**先做"屏幕角落浮窗 pending 显示"**作为 ROI 最高的尝试
- **真·输入法集成(macOS IMK / Linux fcitx,微信输入法的蓝线 composition)**:
  最接近用户期望的体验,但要彻底改造交互模型(不再是按住热键,而是注册系统
  输入源)。**独立大改造,不是 28 轮延伸**
- **长音频滑动窗口**:支持 > 60s 连续说话,encoder 窗口淘汰 + decoder prefix
  capping
- **ROLLBACK_TOKENS 在真实使用下进一步调优**:可能 chunk=1.5s + rollback=2
  是当前架构的甜区,需要真机录音 fixture 集体回归

## Spike 脚本保留

`scripts/spike_qwen3_streaming.py` 留在 `scripts/` 下,下次 Qwen3-ASR ONNX 升
级后可直接跑:

```bash
uv run python scripts/spike_qwen3_streaming.py
```

期望输出:Path A edit distance >> 0(失败),Path E edit distance ≤ 5%(成功)。
如果未来上游训练数据 / 量化方式让 Path A 能 work,可考虑切回省每 chunk 一次
prefill 成本。
