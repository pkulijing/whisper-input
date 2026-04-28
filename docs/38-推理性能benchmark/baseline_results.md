# Round 38 baseline — 数据已迁移

baseline 数据 + 自动渲染矩阵表已迁到正式 benchmark 目录:

- 机读 JSON:
  [`benchmarks/results/baselines/2026-04-28_apple-silicon_round38.json`](../../benchmarks/results/baselines/2026-04-28_apple-silicon_round38.json)
- 给人看的 Markdown:
  [`benchmarks/results/baselines/2026-04-28_apple-silicon_round38.md`](../../benchmarks/results/baselines/2026-04-28_apple-silicon_round38.md)

> 第一次跑的 spike 脚本仍在本目录 [`benchmark.py`](benchmark.py)
> 作为历史快照,但**结果数据以上述 baseline 文件为准**(spike 那次没含
> 环境指纹 / 单源数据,正式 baseline 跑出来口径更全)。
> 重跑请用 `uv run python -m benchmarks --output benchmarks/results/baselines/<date>_<machine>_<context>`。

## 人工分析与结论

### 1. fp16 vs int8 整体 slowdown(总时间)

按 baseline 数据(int8 + fp16-0.6B 用 3 repeats / 1 warmup;**fp16-1.7B
重测过 7 repeats / 3 warmup**,见下方观察 #5):

| variant | short (5s) | medium (10.5s) | long (25s) |
|---|---|---|---|
| 0.6B | **0.88×**(fp16 反而更快) | 1.38× | 2.19× |
| 1.7B | 1.45× | 1.75× | 2.72× |

- **0.6B short**:fp16 比 int8 快 12% —— 因为 fp16 用 chunk-aligned encoder
  只算 65 个 audio token,int8 走 30s pad 强制算 390 个。短音频上 fp16 的
  encoder/prefill 固定成本被省掉
- **fp16 的劣势随音频长度显著放大** —— long 上 1.7B 接近 2.7x
- 跟 round 37 SUMMARY "fp16 比 int8 慢 2-3x" 的口径完全对得上

### 2. 时间分布 —— decoder_step 是绝对大头

baseline 里 fp16-1.7B-long 总 14.52s,各段中位数:

- encode = 1.12s (8%)
- prefill = 3.26s (22%)
- **decode = 10.13s (70%)**

77 个生成 token,平均每步 ~132ms。**优化首要瞄准 decoder_step 单步延迟**,
其次才是 prefill / encode。

### 3. fp16 跟音频长度的关系比 int8 敏感得多

| 维度 | int8 | fp16 |
|---|---|---|
| Encode 时间 5s→25s | 0.52s → 0.53s(几乎平) | 0.17s → 0.82s(4.8×) |
| Prefill 时间 5s→25s | 0.54s → 0.54s(几乎平) | 0.26s → 1.17s(4.5×) |
| Audio tokens 5s→25s | 390 → 390(30s pad 恒定) | 65 → 325(5×) |

老 int8 的 30s pad 是"以恒定开销换可预测延迟"。新 fp16 chunk-align 的好处
是短音频快、坏处是长音频劣势放大。这是下一轮性能优化 优化时要考虑的 trade-off。

### 4. ⚠️ int8-1.7B-short 翻车 —— issue #7 翻车谱新增 case

`zh.wav[:5s]` 这段音频:

| 配置 | 生成 token 数 | 文本 |
|---|---|---|
| int8-0.6B | 17 | "先帝创业未半而中道崩殂，今天下。" ✅ |
| **int8-1.7B** | **3** | **'' (空)** ⚠️ |
| fp16-0.6B | 17 | 同 0.6B int8 ✅ |
| fp16-1.7B | 17 | 同 0.6B int8 ✅ |

只有 1.7B int8 在这条 audio 上提前 EOS。Round 37 SUMMARY 列的翻车谱
(`zh.wav 完整 10.56s` / `zh_long[:5s]`)再加上本轮新发现的 `zh.wav[:5s]`,
说明 1.7B int8 的"audio-内容相关确定性翻车"覆盖面比当时记录的更广。**这同时
反向佐证了 round 37 切到 fp16 的正确性 —— 不是为了边边角角的 fix,而是 1.7B
int8 在 offline path 上根本不可用。**

### 5. 测量方法学 —— fp16-1.7B 第一次 run 噪声排查

**第一次 baseline 的高离散度被证实是测量瑕疵,不是模型本身性质。**

第一次跑(N_REPEATS=3, N_WARMUP=1, 4 backend × 3 fixture sequential)时
fp16-1.7B-medium 三次原始数据:

```
run 1: total=6.67s
run 2: total=8.96s
run 3: total=13.35s   ← 跨 run 接近 2× 差异,σ=3.36s
```

加大 warmup 隔离重测(N_REPEATS=7, N_WARMUP=3,只跑 fp16-1.7B,前面没有
其他 backend 抢散热):

```
medium: 5.79 / 5.82 / 5.84 / 5.87 / 5.87 / 5.89 / 5.90s (median 5.87, σ=0.04s, CV 0.7%)
```

**结论**:噪声是 ONNX session graph init + tensor allocator 暖机不足 +
sequential 全量跑触发 thermal headroom 收紧的叠加,而不是 1.7B 本身不稳。
1 次 warmup 不够,3 次后稳到 CV < 1%。

**对本轮 benchmark 框架的修订**(已应用):

- `measure_case` 多吐 `total_min_s` / `total_max_s` / `total_std_s` /
  `total_cv` 字段,markdown 报告新增"运行离散度"表格 + per-case 详情里展示
  raw runs。**任何 case 的 CV > 5% 就是测量数据有问题,先重测再看 median**
- CLI 加 `--warmup N`(默认 1),配合 `--repeats N` 让重型 backend 专门多 warm
- baseline 文件里 fp16-1.7B 的三条记录用隔离重测的数据替换,JSON `fingerprint`
  加 `note` 字段标注"这三条来自隔离重测",其他 9 条保留原始全量 run 数据
  (它们的 CV 都 < 4%,数据可信)

**对下一轮性能优化 的方法学要求**:

- 加新 backend 候选(CoreML / GGUF / MLX 等)时,默认跑 `--repeats 5
  --warmup 3` 起步,看 CV 决定是否要更多
- 跑完看运行离散度表格 —— CV > 5% 必须重测,不进 baseline
- 不同 backend 对比时,**不只比 median**,还要比 max(worst-case 用户体感)
  和 std(系统稳定性)

### 6. 下一轮性能优化 优化方向(按预期收益排序)

1. **decoder_step 单步加速 = 最大杠杆**(占总时间 70%)
   - **CoreML EP**:Apple Silicon 原生,理论 2-5x。先 spike
   - **CUDA EP**(有显卡的 Linux 用户):服务端 / 工作站
   - GGUF / MLX 后端(改动量大,fallback)
2. **prefill 跟音频长度线性增长是必然的(attention 矩阵 O(N²))**;短期
   缓解:预热 session、首次按键先跑一次空 mel(round 37 SUMMARY 后续 TODO 已列)
3. **回滚 int8 + only-0.6B**(放弃 1.7B variant)是兜底:0.6B int8 medium
   只要 1.83s,RTF 0.17,用户体验完全 OK。但代价是 1.7B variant 永久下线
4. **int8 PAD 策略移植到 fp16?** 老 int8 30s pad 让长 / 短音频成本一致;
   fp16 上 pad 到固定 30s 也许能让 long case 不变更长,但 short case 会
   退化。需要 spike 验证收益是否值得
