# SUMMARY:CI 失败修复

## 背景

### BUG 表现
master / fix/ci 分支连续 3 次 build 卡在同 4 个 case:

- `test_qwen3_asr.py::test_transcribe_zh_wav[0.6B/1.7B]`
- `test_qwen3_stream_smoke.py::test_streaming_via_full_whisperinput_pipeline[0.6B/1.7B]`

症状一律是 `Qwen3ASRSTT.transcribe(zh.wav)` 返空字符串。GitHub Actions cache **HIT** v2(模型文件已 warm),仍然返空。本地全过(本地 cache 跟 CI cache 是两份独立副本)。

### 影响
后续 PR 全堵着合不进 master,30 轮当时只挂了 1 次,我把它当 flaky 写进 BACKLOG「先观察」—— 事后看是错的判断,从 commit `5d4f448` 起变成必现。

## 实现方案

### 关键设计

**根因仍未证实**(也没法在本地复现)。这一轮做的是**三件事的组合,任何一条单独都不一定能修,但合在一起把"flaky → 必现 → 不可观测"的死循环拆开**:

1. **bump cache key v2 → v3 + 拿掉 restore-keys**:假设 v2 cache 在某次 save 时存了损坏副本,所有 hit 都拿坏副本。这一招**便宜、风险小**,如果假设成立直接修。**关键细节**:不能保留 `restore-keys: modelscope-qwen3-asr-`,否则 v3 miss 时会 fallback 拿 v2 的坏副本,bump 等于白做
2. **`_warmup()` 加 assert + 改用真信号**:原 warmup 用全零静音空跑,**输出不检查**。改成用 fixed-seed 高斯噪声(1s,峰值 ~0.05)+ prefill 后 + 5 步 greedy decode + 三条 assert(logits finite / 非全 0 / greedy 至少吐 1 个非 EOS token)。fail 抛 `RuntimeError`,把 silent garbage 在 load 阶段就暴露出来,而不是等 transcribe 返空才暴露
3. **加诊断日志**:`transcribe()` / `_warmup()` / `load()` 用 structlog 打 logits min/max/mean/all_finite/any_nonzero、generated 前 5 个 token id、ONNX 文件 size。下次 flaky 时直接从 CI log 看到关键信号,不用盲猜

### 开发内容

**改 `src/daobidao/stt/qwen3/qwen3_asr.py`**:

- `_warmup()` 重写:`np.zeros` → fixed-seed 高斯噪声;增加 5 步 greedy + 三条 assert;输出 `qwen3_warmup_logits_stats` / `qwen3_warmup_greedy` 两条诊断 event
- `transcribe()` 增加 `qwen3_transcribe_prefill_done` / `qwen3_transcribe_decode_done` event,打 prompt_len、audio_features.shape、logits 统计、generated_count、first 5 token ids、hit_eos / hit_max
- 新增 `_log_onnx_file_sizes()`:load 完打 conv_frontend / encoder / decoder 三个 onnx 的 size
- 新增 `_logits_stats(logits) -> dict`:抽出来给 warmup / transcribe 共用

**改 `.github/workflows/build.yml`**:

- cache key `modelscope-qwen3-asr-v2` → `v3`
- 拿掉 `restore-keys`,避免从 v2 fallback 拿坏副本
- 注释里补 `v2 → v3` 的原因

**新增测试 `tests/test_qwen3_asr.py`**(TDD,先红后绿):

- `test_warmup_raises_on_all_nan_logits` — fake decoder 返 NaN → 抛 RuntimeError
- `test_warmup_raises_on_all_zero_logits` — fake decoder 返全 0 → 抛
- `test_warmup_raises_on_immediate_eos` — fake decoder 让 argmax 立即 EOS → 抛
- `test_warmup_passes_with_healthy_runner` — fake decoder 返非零 finite + non-EOS → 不抛
- 配套 `_FakeRunner` / `_FakeTokenizer` / `_make_unloaded_stt` helper,绕过真 modelscope download

### 额外产物

- `docs/33-CI失败修复/PROMPT.md` + `PLAN.md`:把这一轮的需求 + 实现思路落了文档
- `BACKLOG.md` 里那条「CI 冷 cache transcribe flaky 隐患」可以删 / 标 done(本轮总结里直接说,不动 BACKLOG —— 留给下轮如果 v3 仍挂时回滚使用)

## 局限性

1. **根因没证实**:bump cache key 是"试试看"性质。如果 v3 build 挂的话,新加的诊断日志能告诉我们 logits 长什么样、是 prefill 出 NaN 还是 token 选 EOS,但**修**还得继续探索
2. **warmup fail-fast 改变了用户感知**:之前模型坏了用户看到的是"按热键松开后没反应",现在变成"daobidao 启动失败 + 报 RuntimeError"。在生产环境如果出现真概率极低的"warmup 单次抖动"(没有任何证据它会发生),会变成启动失败。**没加 retry 是有意为之** —— 治标且可能掩盖真 bug,等观察到再加
3. **本地复现不到**:本地的 modelscope cache 在 `/mnt/.../modelscope`(memory 里有 reference),跟 CI cache 是两份独立副本,本地一直 warm 一直过。这一轮的 fix 验证只能靠 push 到 fix/ci 看 CI 跑结果
4. **诊断日志在 pytest 默认输出里看不到**:pytest 默认捕获 logger.info,要 `--log-cli-level=INFO` 或者改 pyproject 才能在 CI log 里看到。这一轮**没改 pytest 配置** —— 如果 v3 build 通过那本来就不需要看;如果挂,下轮再加

## 后续 TODO

- **如果 v3 build 通过**:把 BACKLOG 里那条「CI 冷 cache transcribe flaky 隐患」标记完成 / 删掉。观察 1-2 周再确认稳定
- **如果 v3 build 仍挂**:开下一轮基于诊断日志定位:
  - `qwen3_warmup_logits_stats` 显示 NaN / 0 → ORT graph optimization 问题,可能要换 ORT 版本或加 `SessionOptions.graph_optimization_level = ORT_DISABLE_ALL` 试
  - prefill logits 正常但 `first_5_token_ids` = [eos_id] → 模型权重读取问题,看 `qwen3_onnx_file_sizes` 是否对得上
  - 三个统计都正常但 transcribe 仍返空 → parse_asr_output 路径有 bug,看 raw token 序列
- **可能的扩展**:在 pytest CI 里加 `--log-cli-level=INFO` 让诊断 event 在 GitHub Actions log 里直接可见,目前 pytest 默认会捕获
- **warmup retry 兜底**(暂不做):如果出现 warmup 真概率极低的抖动导致启动失败,加 1 次 retry。证据出现再做
