# 计划:流式识别长音频滑动窗口

## 关键设计

### 滑窗只动两个东西

`_stream.py:Qwen3StreamState` 里有:

- `audio_features_pieces: list[np.ndarray]` —— 每 chunk 一片,累积。**滑窗时直接折叠 + 截切**
- `committed_tokens: list[int]` —— 已 commit 的 token,跟 `committed_text` 强绑定。**本体不能改**,只在拼 `mid_ids` 时用一个 slice

### 滑窗策略

在 `_stream.py:stream_step` 的 line 261 之后(audio_features 已 concat)、line 265 之前(mid_ids 还没拼)插入:

```python
# --- 滑窗 1: audio 端 ---
if audio_features.shape[1] > MAX_AUDIO_TOKENS:
    audio_features = audio_features[:, -MAX_AUDIO_TOKENS:, :]
    state.audio_features_pieces = [audio_features]  # 折叠,避免下次又 grow

n_af = audio_features.shape[1]

# --- 滑窗 2: committed 端(只切 prefill slice,不动 state 本体) ---
if len(state.committed_tokens) > MAX_COMMITTED_TOKENS:
    committed_for_prefill = state.committed_tokens[-MAX_COMMITTED_TOKENS:]
else:
    committed_for_prefill = state.committed_tokens

mid_ids = (
    [state.audio_pad_id] * n_af
    + state.chat_suffix_ids
    + committed_for_prefill   # ← 只这里换成 slice
)
```

### 为什么 marker 检测不受影响

`_stream.py:346-348` 检测 `asr_text_id in state.committed_tokens` —— 这扫的是 **state 本体**(没动),不是 `committed_for_prefill`(slice)。所以即使 prefill 看不到 marker,marker tracking 仍然正常。

### `decoder_step` 的 `cache_position` 在滑窗后仍 forward-only

`_onnx_runner.py:230` 的 `cache_position = np.arange(cur_len, cur_len + seq)` 是单调递增写入。滑窗只是让 `mid_ids` 变短 → `prefill_end = prefill_start + len(mid_ids)` 变小 → 后续 greedy decode 从更小的 `cur_len` 开始,但仍然单调递增。**符合 forward-only 语义**。

### 阈值起始值

总预算 `1200 - chat_prefix(~30) - chat_suffix(~5) - MAX_NEW_TOKENS_PER_CHUNK(32) = 1133` 给 `audio + committed` 分。

| 常数 | 起始值 | 含义 |
|------|--------|------|
| `MAX_AUDIO_TOKENS` | 700 | 滑动音频窗,~56-70s 等效音频(假设 ~10-12.5 token/s) |
| `MAX_COMMITTED_TOKENS` | 400 | 历史 committed 上下文,~80-130s 等效说话长度 |

总 1100 ≤ 1133,余 33 防 off-by-one。

**精调方式**(不开独立 spike):实施时在 `stream_step` 加一行 `logger.debug("stream_step", n_af=n_af, committed_len=len(state.committed_tokens))`。用户做完 90s 人肉测试后看 log,反推真实速率,必要时调阈值并写进 SUMMARY.md。

## 删除清单

### `__main__.py`(~50 行删,~10 行改)

- L63: `_STREAM_WARN_SAMPLES = 28 * 16000` → 删
- L213-215: `_stream_near_limit_warned`/`_stream_overflow_hit` 字段初始化 → 删
- L312-313, L445-446, L580-581: 各 reset 行 → 删
- L485-505: 28s 触发 `_notify_near_limit` 的整块 → 删,只保留 chunk swap_out 入队逻辑
- L515-522: `_notify_near_limit` 整方法 → 删
- L528-554: overflow 处理 → **改写** 保留 `try/except StreamingKVOverflowError`,内部改为 `logger.warning("stream_kv_overflow_unexpected") + self._finalize_stream_session()`,**不丢 chunk、不调 overlay**

### `assets/locales/{zh,en,fr}.json` 各删 2 行

每文件第 111-112 行的 `main.streaming_overflow` + `main.streaming_near_limit` 两个 key

### 不动

- `_stream.py:277-282` 和 `_stream.py:460-461` 的 `raise StreamingKVOverflowError` —— 防御性兜底,理论上永不触发
- `overlay_{macos,linux}.py:update(text)` 的 text 参数无视 bug —— 整条上游调用链已删,bug 自动归零

## TDD 步骤

### 步骤 1:写新测试 `tests/test_qwen3_stream_sliding.py`

四个 case,全部用 `FakeRunner` 模拟,不依赖真实模型:

- **case A — audio 滑窗**:喂足够多 chunk 让累积 `audio_features.shape[1]` 超 `MAX_AUDIO_TOKENS`,断言:
  - 下次 `stream_step` 后 `mid_ids` 中 audio_pad 数 = `MAX_AUDIO_TOKENS`
  - `state.audio_features_pieces` 折叠为单元素
  - 不抛 `StreamingKVOverflowError`
- **case B — committed 滑窗**:让 `state.committed_tokens` 涨到超 `MAX_COMMITTED_TOKENS`,断言:
  - 拼出来的 `mid_ids` 中 committed 段长度 ≤ `MAX_COMMITTED_TOKENS`
  - **`state.committed_tokens` 本体长度未变**
- **case C — 输出连贯性**:case A 触发滑窗后,`state.committed_text` 跟 `tokenizer.decode(state.committed_tokens)` 一致(无漏字、无重复)
- **case D — long 模拟**:N=200 chunks(每 chunk 5 audio tokens + 8 committed tokens),不抛任何错误,断言整个 session 跑通

### 步骤 2:实现 `_stream.py` 滑窗逻辑

在 `_stream.py` 顶部加常数:
```python
MAX_AUDIO_TOKENS = 700
MAX_COMMITTED_TOKENS = 400
```

在 `stream_step` 的 line 261 之后插入滑窗代码块(见上文设计)。

跑 step 1 的测试,直到全部 pass。

### 步骤 3:改写 `__main__.py` overflow 路径,删 28s 死代码

按"删除清单"逐项执行。

### 步骤 4:更新 `tests/test_main_streaming.py`

增量验证:
- overflow catch 路径改为 `logger.warning + finalize`,不再设 `_stream_overflow_hit`、不再调 overlay.update
- 删掉旧的 near_limit / overflow 相关测试用例(如果有)

### 步骤 5:删 i18n locale 三语行

### 步骤 6:跑全套验证

- `uv run ruff check .` 无新警告
- `uv run pytest -q` 全过(预期 240+ → 245+)
- `git grep -n "_STREAM_WARN_SAMPLES\|_stream_near_limit_warned\|streaming_near_limit\|streaming_overflow"` 应返回空

### 步骤 7:人肉测试(用户操作)

`uv run daobidao` → 按住热键念稿子 90s+ → 验收:
- 浮窗:全程正常声音条波动
- 焦点应用:文本持续追加,无 ~80s 处的截断
- log:无 `stream_kv_overflow` warning,无 `streaming_near_limit` info
- 看 `stream_step` debug log 的 n_af / committed_len 数字,如有需要调阈值

### 步骤 8:收尾

- 更新 `docs/BACKLOG.md` 删除"流式识别长音频滑动窗口"条目
- 用户跑 `/devtree` 加 N35 节点
- 写 `docs/35-流式滑窗/SUMMARY.md`,含实测 token 速率 + 最终阈值

## Files to Modify

| 文件 | 改动 |
|------|------|
| `src/daobidao/stt/qwen3/_stream.py` | 加滑窗逻辑 + 一行 debug log,~30 行净增 |
| `src/daobidao/__main__.py` | 删 ~50 行死代码,改 ~10 行 overflow 处理 |
| `src/daobidao/assets/locales/zh.json` | 删 line 111-112 |
| `src/daobidao/assets/locales/en.json` | 删 line 111-112 |
| `src/daobidao/assets/locales/fr.json` | 删 line 111-112 |
| `tests/test_qwen3_stream_sliding.py` | 新建,~150 行(case A/B/C/D) |
| `tests/test_main_streaming.py` | 增量 ~30 行 |
| `docs/BACKLOG.md` | 删除"流式识别长音频滑动窗口"条目 |
| `docs/35-流式滑窗/SUMMARY.md` | 收尾时新建 |

## Risks

1. **滑窗触发时下一段开头衔接可能略不顺**(早期 committed 上下文丢失) —— 接受;若严重再调大 `MAX_COMMITTED_TOKENS`
2. **encoder cross-attn 丢早期音频信息** —— 对"按住即说"场景影响极小,因为不存在跨段语义引用
3. **实测 token 速率比预估快** —— 起始阈值留了 33 token headroom,允许 ~3% 误差;debug log 会暴露真实数字
4. **`committed_for_prefill` 截断后,model 看不到 `<asr_text>` marker 也能继续生成 ASR 文本** —— LLM 自回归的天然特性(看最近 token 决定下一个);若实测有问题,fallback 是把 `<asr_text>` 强行塞进 slice 头部
