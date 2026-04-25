# 实现计划

## 三块改动

### 1. bump cache key v2 → v3

**改**: `.github/workflows/build.yml` 第 54 行 `key: modelscope-qwen3-asr-v2` → `v3`,同步注释里 `v2 →` 段落补一行 `v2 → v3: round 33 修 CI 必现失败,强制 cache miss 重下一份干净的`。

**为什么这能修**: 假设是 v2 这份 cache 在某次 save 时存了损坏副本(部分 onnx 文件 size/内容不对),**所有后续 cache hit 都拿到这份坏副本**。未必是真因,但 bump key 触发一次干净下载是最便宜的实验,如果 v3 下来后过 → 假设证实。如果 v3 还挂 → 排除 cache 假设,继续下一步。

**风险**: 第一次 v3 build 因为冷下载会慢 ~3-5 分钟。可接受。

### 2. warmup 改用真音频 + assert

**改**: `src/daobidao/stt/qwen3/qwen3_asr.py` `_warmup()`。

**关键设计**:

- **input 不再用 `np.zeros`**。改成 `np.random.default_rng(0).standard_normal(SAMPLE_RATE).astype(np.float32) * 0.05` —— 1s 固定 seed 高斯噪声,峰值 ~0.05(避免 clip 也避免太小),完全确定性、不依赖文件 fixture
- **跑完整 prefill + 几步 greedy decode**(比如 5 步),不只 prefill 一次
- **assert 三条**:
  1. `np.isfinite(logits).all()` —— prefill logits 不能含 NaN / Inf
  2. `(logits != 0).any()` —— 不能全零(garbage 标志)
  3. `len(generated) > 0` —— 5 步 greedy 至少吐出 1 个非 EOS token(纯 EOS 是模型坏的强信号)
- assert 失败抛 `RuntimeError("qwen3 warmup produced degenerate output: ...")`,把上述 3 个统计值塞进 message —— `load()` 会冒上来,fixture 直接 fail,在 CI 里 `stt_0_6b` / `stt_1_7b` setup 阶段就挂,比"等 transcribe 返空"更早暴露

**为什么这能修**:
- 不直接修 CI 失败(它本来就是因为 warmup 没卡住 garbage,改成卡住后失败位置往前移),但**消除"silent garbage" 路径** —— 以后再出问题不会"transcribe 神秘返空",而是"load 直接抛"
- 真音频 input 比 silence 更接近生产路径,给 ORT 更真实的 graph 优化机会(silence 的 mel 是常数,某些算子可能走特殊数值路径,不能代表真实 workload)

**TDD 切入点**:
- 写一个 `test_warmup_raises_on_degenerate_output`,monkeypatch runner 让 `decoder_step` 返全 0 或全 NaN,assert `Qwen3ASRSTT.load()` 抛 RuntimeError
- 写一个 `test_warmup_real_audio_passes`,正常加载下不抛
- 这两条 + 现有的 `test_load_is_idempotent` 一起跑,保证不破现有契约

### 3. transcribe / 关键路径加诊断日志

**改**: `src/daobidao/stt/qwen3/qwen3_asr.py` `transcribe()` 和 `_warmup()`,用 `logger.info` 打 structured event。

**打什么**:

- `qwen3_transcribe_prefill_done`: prefill 后 logits 的 `min`/`max`/`mean`/`has_nan`/`has_inf`,prompt 长度,audio_features.shape
- `qwen3_transcribe_decode_done`: `generated_count`,前 5 个 token id (避免 PII / log 太长不打文本),decode 用时,`hit_eos` (bool) / `hit_max` (bool)
- `qwen3_warmup_logits_stats`: 同 prefill_done 的 logits 统计
- onnx 文件 size: load 完后遍历 `cache_root / model_{variant}/*.onnx`,打每个文件的 size(检测 cache 损坏)

**为什么**: BACKLOG 里那条说"下次 flaky 时立刻能定位是 prefill 出 NaN 还是 token 选 EOS 还是 ONNX 文件 size 不对"——现在补齐这层观测。pytest 默认不展示 logger.info,但 CI 里 `xvfb-run uv run pytest` + structlog 走 stdout 应该能看到(回头确认下)。如果 pytest 默认捕获 logger,加 `--log-cli-level=INFO` 或在 pyproject.toml 里配。

**风险**: 生产路径 transcribe() 多 1-2 个 logger.info 调用,微秒级开销,可忽略。

## 测试计划

新加测试(TDD,先红后绿):

| 测试 | 目的 |
|---|---|
| `test_warmup_raises_on_all_nan_logits` | monkeypatch decoder 返 NaN,assert load() 抛 RuntimeError |
| `test_warmup_raises_on_all_zero_logits` | monkeypatch decoder 返全 0,assert load() 抛 |
| `test_warmup_raises_on_immediate_eos` | monkeypatch decoder 让 argmax 立即 EOS,assert load() 抛 |
| `test_warmup_passes_with_real_runner` | 不 mock,真跑,正常通过(已经是 `test_load_is_idempotent` 的覆盖,但显式加一条更直白) |

现有测试:
- `test_transcribe_zh_wav[0.6B/1.7B]` 保留,继续作为"端到端真识别"的回归
- `test_load_is_idempotent` 保留

## 落地顺序

1. 先在本地起 ruff + 现有 pytest,确保基线绿
2. 写 4 条新测试(先红)
3. 改 `_warmup()` 实现,跑新测试到绿
4. 加诊断日志,跑全套测试
5. 改 workflow cache key
6. push fix/ci,等 CI 跑,看新日志
   - 如果 v3 build 通过 → 假设 1 (cache 损坏) 证实,合 master
   - 如果 v3 build 仍挂 → 看新日志,根据 logits 统计 / token 序列定位下一步
7. 写 SUMMARY.md

## 风险 / 局限性

- bump cache key 是"试试看"性质,如果根因不是 cache 损坏,光靠这一招没用 —— 但 fail-fast warmup + 诊断日志能保证下一轮迭代有信号
- warmup 加 assert 后,如果 ORT 在某些 runner 上**真的**会零星给 garbage 输出,会变成 load 直接挂,用户启动 daobidao 失败 —— 这是 fail-fast 的本意,可通过 retry warmup (比如 1 次重试) 缓和。**先不做 retry,等观察到再说**
- 假设 cache 损坏不成立的话,下一步可能要做的:retry-on-empty 在 transcribe 层 / 用 self-hosted runner / 切别的 ORT 版本。这些都不在本轮 scope
