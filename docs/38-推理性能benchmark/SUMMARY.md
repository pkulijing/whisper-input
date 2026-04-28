# Round 38 — 推理性能 benchmark(调研轮)

> **性质:调研轮,不是开发轮**。产出"性能数据 + 测量框架",不动产品代码,
> 不解决性能问题。性能优化的实际开发由独立的下一轮承接(由本轮 baseline
> 数据驱动选方案)。issue [#7](https://github.com/pkulijing/daobidao/issues/7)
> **不在本轮关闭**,等下一轮性能优化做完再决定是否一并 close。

## 开发项背景

### 希望解决的问题

Round 37 切到 baicai1145 fp16 ONNX 修了 1.7B offline 确定性返空的 issue #7
正确性问题,但 **fp16 推理在 Apple Silicon CPU 上比 int8 慢 2-3x**,真机
实测"按住说话出字延迟、松开后等到崩溃感"。Round 37 SUMMARY 已明确写"本
分支不可上线"——分支暂存等优化。

下一轮要做的实际性能优化(CoreML EP / CUDA EP / GGUF / 回滚 int8 等候选)
需要**严谨的可重复 baseline 数据**驱动选择,而不是凭直觉。**本轮就是为
做这件调研**:测出数据 + 立可复用框架,让下一轮性能优化的选型有客观依据。

## 实现方案

### 关键设计

#### 第一阶段(spike) — Baseline 测量

挑了 **3 段音频长度 × 2 variant × 2 quant = 12 case**:

- 长度:short (5s) / medium (10.5s) / long (25s)。挑选时**避开 round 37
  SUMMARY 列出的 1.7B int8 翻车谱**(`zh.wav` 完整 / `zh_long[:5s]`),
  且不超 30s(老 int8 静态 cache `max_total_len=1200` 装不下)
- variant:0.6B / 1.7B
- quant:zengshuishui int8(老,round 26-36)/ baicai1145 fp16(新,round 37+)

每 case warmup 1 + 正式 3 run,取 median。报 encode / prefill / decode /
total / RTF 五个时间分量。

#### 第二阶段(框架正规化) — 把 spike 升级成正式 `benchmarks/` 目录

第一阶段(spike) 的 spike 脚本一次性,但模型选型(后续每来一个候选 backend 都要重测)
需要长期可重复的工具。立顶层 `benchmarks/` 目录跟 `tests/` 同级:

- **Backend 协议**(`benchmarks/backends/base.py`):每个候选 backend 实现
  ``encode_audio`` / ``alloc_caches`` / ``decoder_step`` + `eos_ids` /
  `name` / `family` / `variant` / `quant` 协议字段,模块暴露 `discover()`
  返 backend 实例列表。harness 跟具体实现解耦
- **CLI**(`uv run python -m benchmarks`):支持 `--backend prefix` /
  `--fixture slug` / `--output stem` / `--repeats N` / `--warmup N`,
  prefix 匹配 + 逗号分隔
- **结果存储策略**:`results/*.{json,md}` 默认 gitignore(每次跑产物),
  `results/baselines/*` 不 gitignore(人为标记的"基线"入库)。文件名约定
  `<YYYY-MM-DD>_<machine-tag>_<context>.{json,md}`
- **Fingerprint**:每个 run JSON 头都含 platform / chip / onnxruntime
  version / git SHA + dirty flag / UTC timestamp,跨机对比可追溯
- 跟 `tests/` 共用 `tests/fixtures/*.wav` 不复制文件,但**不进 pytest 也
  不进 CI**(跑一次太重 + 要 ~7GB 模型 cache)

#### 第二阶段(框架正规化) 后续修订 —— fp16-1.7B 数据高离散度排查

第一次全量 baseline 跑出 fp16-1.7B-medium 三次 6.67/8.96/13.35s 跨度
2x,σ=3.36s。隔离重测(N_REPEATS=7 N_WARMUP=3,只跑 fp16-1.7B,前面没
其他 backend 抢散热)发现:**这是 ONNX session 暖机不足 + sequential 全量
跑触发 thermal headroom 收紧的叠加,不是 1.7B 本身性质**。重测后 σ=0.04s
(CV 0.7%)。

由此修了 harness:`measure_case` 多吐 `total_min_s` / `total_max_s` /
`total_std_s` / `total_cv` 四字段,markdown 报告新增"运行离散度"表格 +
per-case 详情展示 raw runs。CLI 加 `--warmup N`(默认 1)。**任何 case
CV > 5% 就是测量数据有问题,先重测再看 median** 写入方法学。

baseline JSON 里 fp16-1.7B 三条用隔离重测的数据替换,其他 9 条保留(它们
CV < 4%,数据可信),fingerprint 加 `note` 字段标注此事。

### 开发内容概括

按 PLAN.md 走完第一阶段(spike) 7 步 + 第二阶段(框架正规化) 8 步 + 后续修订:

**第一阶段(spike)**:
- `docs/38-*/benchmark.py` 一次性 spike 脚本,内嵌 `Int8Runner`(从 git
  `6dec467^` 抠老代码)+ `Fp16Runner`(抄 round 37 spike)
- 跑出 12 case 数据,12/12 PASS(int8-1.7B-short FAIL 是 issue #7 翻车谱
  新增 case,见关键发现 #2)
- `docs/38-*/baseline_results.{md,json}` 初版

**第二阶段(框架正规化)**:
- 立 `benchmarks/` 顶层目录(README / `__main__` / config / harness /
  fixtures / reporting / backends/)
- 把 spike 的 `Int8Runner` / `Fp16Runner` 抽到 `backends/qwen3_int8_zengshuishui.py`
  / `backends/qwen3_fp16_baicai1145.py`,实现 Backend 协议 + `discover()`
- CLI 跑通,跑一次完整 baseline 写到 `benchmarks/results/baselines/2026-04-28_apple-silicon_round38.{json,md}`
- 根 `.gitignore` 加 `benchmarks/results/*.{json,md}` + `!benchmarks/results/baselines/`,
  规则验过(普通 run 文件被忽略,baseline 子目录入库)
- `docs/38-*/baseline_results.{md,json}` 改成 pointer + 6 段人工分析

**后续修订**:
- harness 加 `n_warmup` 参数 + 4 字段统计(min/max/std/cv)
- reporting 加"运行离散度"表格
- 修了 `Path.with_suffix` bug(stem 含 "." 会被当后缀截掉,改成
  `stem.parent / f"{stem.name}.json"`)
- 隔离重测 fp16-1.7B,merge 三条记录回 baseline JSON
- 重新渲染 baseline markdown
- docs/38 第 5 节"测量方法学"重写,把噪声故事从"1.7B fp16 本身就抖"
  改成"测量瑕疵 + 怎么避免"

### 额外产物

- **3 段音频切片清单**(`benchmarks/fixtures.py` 的 FIXTURES):short /
  medium / long 都避开了 round 37 翻车谱 + 不超 30s,后续下一轮性能优化候选直接
  复用同一组 fixture
- **CV-based 测量方法学**:CV > 5% 必重测、新 backend 默认 `--repeats 5
  --warmup 3` 起步、不只比 median 也比 max。**这套规矩比"baseline 数字
  本身"更值钱** —— 数字会随硬件 / ONNX 版本 / 候选 backend 变,方法学不变
- **Baseline 关键数据**(median):

  | | int8-0.6B | int8-1.7B | fp16-0.6B | fp16-1.7B |
  |---|---|---|---|---|
  | RTF short (5s) | 0.29 | ⚠️FAIL | 0.25 | 0.58 |
  | RTF medium (10.5s) | 0.17 | 0.32 | 0.24 | 0.56 |
  | RTF long (25s) | 0.12 | 0.21 | 0.26 | 0.58 |
  | fp16/int8 long | — | — | 2.19× | **2.72×** |

## 局限性

1. **本轮没做实际性能优化** —— issue #7 主体性能问题仍然存在,1.7B fp16
   在 Apple Silicon CPU 上 long case 仍要 14.5s(RTF 0.58)。本轮交付的是
   "测量框架 + baseline 数据",不是优化方案。**分支仍不可上线**
2. **Baseline 数据 quant 不完全等价** —— int8 + fp16-0.6B 用 N_REPEATS=3
   N_WARMUP=1 跑,fp16-1.7B 是隔离重测的 N_REPEATS=7 N_WARMUP=3。虽然其他
   9 条 CV < 4% 数据可信,但严格说不是同 protocol。重要的是后续 backend
   候选时**用 N_REPEATS=5 N_WARMUP=3 起步**作为新 protocol(已写进方法学)
3. **只测了 Apple Silicon CPU** —— Linux x86 / 有显卡的环境 baseline 缺失。
   下一轮性能优化引入 CoreML EP / CUDA EP 候选时,需要补对应硬件的 baseline 跑
4. **`tests/conftest.py` 仍用 fp16 path** —— 本轮没动产品代码,所以 tests
   套不变。如果下一轮性能优化切 backend 引入新依赖,test 套要相应跟
5. **Round 37 留下的"代码 ↔ 文档不一致"问题没修** —— `CLAUDE.md` /
   `tests/test_download_manager.py` 仍保留 zengshuishui int8 描述/断言。
   这是 round 37 故意留的 ship gate,本轮不动。等下一轮性能优化通过、可以真正
   ship 时再统一更

## 后续 TODO

### (P0)下一轮 —— 实际推理性能优化

由本轮 baseline 数据驱动,候选方向按预期收益排序:

1. **decoder_step 单步加速 = 最大杠杆**(fp16-1.7B-long 总 14.52s 里
   decode 占 70%):
   - **CoreML EP** (Apple Silicon 原生,理论 2-5x) — 最对口,先 spike
   - **CUDA EP**(有显卡的 Linux 用户)
   - GGUF / MLX 后端(改动大,fallback)
2. **prefill 跟音频长度线性增长**(attention 矩阵 O(N²))短期缓解:预热
   session、首次按键先跑一次空 mel(round 37 SUMMARY 后续 TODO 已列)
3. **回滚 int8 + only-0.6B**(放弃 1.7B variant)是兜底:0.6B int8 medium
   只要 1.83s,RTF 0.17,用户体验完全 OK
4. **int8 30s pad 策略移植到 fp16?** 让长 / 短音频成本一致;但 short 会
   退化,需要 spike 验证

### (P1)Linux x86 baseline 补齐

下一轮性能优化引入 backend 时一并跑。

### (P2)benchmark 框架次轮迭代

- 加 `OMP_NUM_THREADS` / ONNX `intra_op_num_threads` / `inter_op_num_threads`
  显式锁的 CLI 参数 —— 当前 SessionOptions 不设线程数,走 ONNX Runtime 默
  认值,会随系统可用核数浮动。如果下一轮性能优化发现 fp16-1.7B 还有残余噪声(就算
  warmup=3 也是),这是下一个该排查的点
- run JSON 头补充系统负载快照(load average / CPU temp)给 outlier 排查用
- `results/` 目录加自动归档清理(>30 天的非 baseline 文件)

> **不再追踪**:benchmark 框架不进 CI / nightly。已加进 [BACKLOG.md
> 「不再追踪」段](../BACKLOG.md#已完成--不再追踪)。
