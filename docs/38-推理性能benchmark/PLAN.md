# Round 38 调研轮 — Baseline 测量 + benchmark 框架计划

> 本计划分两段:**第一阶段** spike 出 baseline 数据 +
> **第二阶段** 把 spike 升级成可重复的 `benchmarks/` 框架。两段都在本轮内
> 完成,统称"调研产出"。性能优化的实际开发是另一轮的事。

## 第一阶段(spike)— Baseline 测量

## 输入清单

### 模型 cache(均已落盘,无需重下)

| Quant | Variant | Cache 目录 |
|---|---|---|
| int8 (老) | 0.6B | `~/.cache/modelscope/hub/models/zengshuishui/Qwen3-ASR-onnx/model_0.6B/` |
| int8 (老) | 1.7B | `~/.cache/modelscope/hub/models/zengshuishui/Qwen3-ASR-onnx/model_1.7B/` |
| int8 tokenizer (共用) | — | `~/.cache/modelscope/hub/models/zengshuishui/Qwen3-ASR-onnx/tokenizer/` |
| fp16 (新) | 0.6B | `~/.cache/modelscope/hub/models/baicai1145/Qwen3-ASR-0___6B-ONNX/` |
| fp16 (新) | 1.7B | `~/.cache/modelscope/hub/models/baicai1145/Qwen3-ASR-1___7B-ONNX/` |

如果 0.6B baicai1145 cache 是 `Qwen3-ASR-0.6B-ONNX/`(不带 ___ 转义)的旧目录
而非 `Qwen3-ASR-0___6B-ONNX/`,benchmark 脚本里两个名字都尝试一下。

### 音频 fixture

| Slug | 来源 | 切片 samples | 切片秒数 | int8 1.7B 是否安全 |
|---|---|---|---|---|
| `short` | `tests/fixtures/zh.wav` 前 5s | 80000 | 5.00 | ✅(zh.wav[:5s] 没在 round 37 翻车谱里) |
| `medium` | `tests/fixtures/zh.wav` 前 10.5s | 168000 | 10.50 | ✅(spike 实测 PASS,对照点) |
| `long` | `tests/fixtures/zh_long.wav` 前 25s | 400000 | 25.00 | ✅(round 37 SUMMARY "8s~28s 全部正常") |

避坑明确不测的长度:
- `zh.wav` full (10.56s = 168960 samples) —— 1.7B int8 翻车
- `zh_long[:5s]` —— 1.7B int8 翻车
- 长度 > 30s —— 老 int8 静态 cache `max_total_len=1200` 装不下
  (audio_token ~25/s × 30s = 750 + prompt 10 + gen 200 ≈ 960,30s 已经接近上限)

## 测量矩阵

3 段音频 × 2 variant × 2 quant = **12 个 case**

每 case 跑 1 次 warmup + 3 次正式测量,**取 3 次的 median**(不是 mean,
避开第 1 次系统抖动 / 内存预热的偶发尖峰)。

### 每个 case 上报指标

```
case_id                : "fp16-1.7B-medium"  (quant-variant-slug)
audio_seconds          : 10.50
encode_s               : encoder.run() 耗时
prefill_s              : 第一次 decoder_step (prompt processing) 耗时
decode_s               : 后续所有 decoder_step (token generation) 耗时之和
total_s                : encode + prefill + decode (排除 tokenizer / 后处理)
rtf                    : total_s / audio_seconds
generated_count        : 生成的 token 数(剔除 EOS)
text                   : 转写文本(用来 sanity check 没翻车)
n_audio_tokens         : encoder 输出长度(prompt 长度推算)
```

写盘成 `baseline_results.json`(机读)+ Markdown 表格(`baseline_results.md`,
给人看的)。

## 测试先行 —— 关键 case 的"输入 → 期待输出"契约

按构成清单全部列出来,benchmark 跑完用这些断言验收数据可信:

### Case 1:`int8-0.6B-medium` (zh.wav[:10.5s])
- 期待 text 包含 "先帝创业未半" 关键短语(round 37 spike 已实测 PASS)
- 期待 RTF < 0.5(int8 0.6B 在 Apple Silicon 历史经验值)
- 期待 generated_count 在 30-60 之间(典型出师表片段)

### Case 2:`int8-1.7B-medium` (zh.wav[:10.5s])
- 期待 text 包含 "先帝创业未半" 关键短语(spike 对照点 PASS)
- 期待 RTF < 0.6
- 期待 generated_count 在 30-60 之间

### Case 3:`fp16-0.6B-medium` (zh.wav[:10.5s])
- 期待 text 包含 "先帝创业未半"(round 37 SUMMARY 实测 PASS)
- 期待 RTF ~ 0.24(round 37 SUMMARY 已记录数值,benchmark 可重现)
- 期待比 int8-0.6B-medium 慢 2-3 倍(round 37 SUMMARY 给的对比口径)

### Case 4:`fp16-1.7B-medium` (zh.wav[:10.5s])
- 期待 text 包含 "先帝创业未半"
- 期待 RTF ~ 0.55(round 37 SUMMARY)
- 期待比 int8-1.7B-medium 慢 2-3 倍

### Case 5-12:short / long 上的 4 组 quant×variant
- 期待 text 非空且不含 `<|...|>` chat token 残留(后处理正确性)
- 期待 RTF 单调:long 略高于 medium,short 因为 KV cache fill 不足 RTF 可能偏高
- 期待 generated_count 与音频长度大致正相关

任何 case 跑出 text 为空 / RTF > 5 / generated_count = 0 → 视为该 case 失败,
benchmark 脚本退出非零并报告哪个 case 翻了。

## 实现步骤

### 1. 写 benchmark 脚本骨架(不联网,只跑本地 cache)

`docs/38-推理性能benchmark/benchmark.py`:

```python
# 顶层 import
import json, time, statistics
from pathlib import Path
import numpy as np
import onnxruntime as ort
from daobidao.stt.qwen3._feature import log_mel_spectrogram
from daobidao.stt.qwen3._tokenizer import Qwen3Tokenizer
from daobidao.stt.qwen3._prompt import build_prompt
from daobidao.stt.qwen3._postprocess import parse_asr_output
from daobidao.stt.qwen3.qwen3_asr import _wav_bytes_to_float32
```

**注意**:不 import 当前的 `Qwen3ONNXRunner`,因为它已经被改成只支持
fp16 的 2-session 了。要内嵌**两个独立 runner 类**:

- `Int8Runner`(从 git `6dec467^` checkout `_onnx_runner.py` 抠出来,3-session,
  KV cache `(B, T=1200, H, D)`,scatter 写 delta,单 EOS = 151645)
- `Fp16Runner`(直接抄 `docs/37-*/spike.py` 的 `SpikeRunner`,2-session,
  KV cache `(B, H, T=1664, D)`,整段 present 覆盖,双 EOS = (151645, 151643))

### 2. 写 transcribe 包装函数(共用)

```python
def transcribe(audio: np.ndarray, runner, tokenizer, max_new_tokens=400):
    # encode
    t0 = time.perf_counter()
    af = runner.encode_audio(audio)   # 接口 0.6B/1.7B 一致
    t_enc = time.perf_counter() - t0

    # prefill
    prompt = build_prompt(int(af.shape[-2]))
    prompt_ids = tokenizer.encode(prompt)
    input_ids = np.array([prompt_ids], dtype=np.int64)
    caches = runner.alloc_caches()
    cur_len = 0
    seq = input_ids.shape[1]
    cache_position = np.arange(cur_len, cur_len + seq, dtype=np.int64)

    t1 = time.perf_counter()
    logits = runner.decoder_step(input_ids, af, caches, cache_position)
    t_pre = time.perf_counter() - t1
    cur_len += seq

    # decode loop
    generated, t2 = [], time.perf_counter()
    for _ in range(max_new_tokens):
        nid = int(np.argmax(logits[0, -1]))      # 老 fp16 callers 兼容接口
        if nid in runner.eos_ids:
            break
        generated.append(nid)
        next_in = np.array([[nid]], dtype=np.int64)
        cache_position = np.array([cur_len], dtype=np.int64)
        logits = runner.decoder_step(next_in, af, caches, cache_position)
        cur_len += 1
    t_dec = time.perf_counter() - t2

    text = parse_asr_output(tokenizer.decode(generated, skip_special_tokens=True))
    return text, generated, dict(
        encode_s=t_enc, prefill_s=t_pre, decode_s=t_dec,
        total_s=t_enc + t_pre + t_dec,
        n_audio_tokens=int(af.shape[-2]),
        generated_count=len(generated),
    )
```

**关键点**:`af.shape[-2]` 跟 `logits[0, -1]` 是 int8/fp16 都能用的统一接口
(int8 logits shape `(B, seq, vocab)`、fp16 runner 内部 unsqueeze 后也是
`(B, 1, vocab)`)。`runner.eos_ids` 在 int8 runner 里手动设成 `{151645}`,
fp16 里就是 `{151645, 151643}`,统一用 set 判 EOS。

### 3. 跑测量矩阵

外层循环 `for quant in [int8, fp16]` × `for variant in [0.6B, 1.7B]`
× `for slug in [short, medium, long]`,每 case warmup 1 次 + 3 次正式
测量,`statistics.median` 聚合时间。所有 12 case × 4 次推理 = 48 次推理,
保守估计每次平均 4s → ~3 分钟跑完。

### 4. 输出结果

写两份:
- `baseline_results.json` —— 机读,每 case 一条 record
- `baseline_results.md` —— 给人看,4 张表(RTF 矩阵 / encode 时间矩阵 /
  prefill 矩阵 / decode 矩阵),最后一段写"主要发现"文字总结

### 5. Sanity check

跑完打开 `baseline_results.md`,人工对照"测试先行"那 4 个关键 case 的契约,
不符就回头看是 benchmark 写错了还是 baseline 真的偏离 round 37 SUMMARY 数据。

## 风险 & 备选方案

1. **int8 0.6B baicai1145 cache 目录名可能是 `Qwen3-ASR-0.6B-ONNX/` 而不是
   `Qwen3-ASR-0___6B-ONNX/`** —— benchmark 用 `glob` 兜底匹配两种命名,选
   存在的那个,缺谁就 modelscope 重下谁(不需要的话不动)
2. **int8 1.7B 在我们以为安全的长度上突然又翻车了** —— round 37 spike 的
   翻车谱不是穷举的,如果某 case 跑出空文本,在结果表里标 ⚠️ 但不 abort,
   该 case 时间数据剔除不参与对比
3. **JIT / cache miss 导致测量噪音** —— warmup 1 次 + 3 次取 median 已经够
   降噪;额外加个 `os.sched_yield()` / 系统负载提示是过度
4. **本地 onnxruntime 版本差异** —— 用项目锁定的 `uv.lock` 里那个,跑
   `uv run python docs/38-推理性能benchmark/benchmark.py` 而非系统 python

## 不做的事

- 不写 pytest 集成 —— benchmark 是一次性 spike 风格脚本,跑出数据就完事
- 不做线程级 / SIMD 级 profiling —— 那是下一轮性能优化 的事
- 不在 CI 里跑 —— 跑一次 ~3min 太重,且要 ~7GB cache,不进 CI

## 验收标准

第一阶段(spike) 完成的标志:
- [x] `benchmark.py` 跑完无异常,12 case 全部产出数据
- [x] `baseline_results.md` 4 张表填齐,文字结论包含:
   - fp16 vs int8 在每个 (variant, length) 上的 slowdown 倍数
   - 主要瓶颈是 encode / prefill / decode 中的哪一段
   - 长度 scaling 是线性还是非线性(暗示 cache fill / quadratic attention)
- [x] 4 个关键 case 的"测试先行契约"全部满足

---

## 第二阶段(框架正规化) — 把 spike benchmark 正规化成 `benchmarks/` 目录

第一阶段(spike) 的 `docs/38-*/benchmark.py` 是一次性 spike,但模型选型(后续每来一个
候选 backend 都要重测)需要长期可重复的工具。把它升级成跟 `tests/` 同级的
顶层 `benchmarks/` 目录。

### 目录布局

```
benchmarks/
  README.md                           # 怎么跑、怎么加 backend、怎么读 baseline
  __init__.py
  __main__.py                         # CLI: uv run python -m benchmarks
  config.py                           # FIXTURES / N_REPEATS / 默认 backend 集合
  harness.py                          # transcribe 计时 + median 聚合 + run record 构造
  fixtures.py                         # WAV 解码 + 切片(共用 tests/fixtures/*.wav,不复制)
  reporting.py                        # records → JSON + Markdown(机器指纹 / git SHA / 表格)
  backends/
    __init__.py
    base.py                           # `class Backend(Protocol)`
    qwen3_int8_zengshuishui.py        # 现 spike 里的 Int8Runner 抽过来
    qwen3_fp16_baicai1145.py          # 现 spike 里的 Fp16Runner 抽过来
    # 未来:qwen3_fp16_coreml.py / qwen3_gguf_llamacpp.py / ...
  results/
    .gitkeep
    baselines/                        # 人为标记的"基线",入 git
      2026-04-28_apple-silicon_round38.json
      2026-04-28_apple-silicon_round38.md
    # 其他 *.json / *.md 全 gitignore(每次跑产物)
```

### Backend 协议

`benchmarks/backends/base.py`:

```python
from typing import Protocol, runtime_checkable
import numpy as np

@runtime_checkable
class Backend(Protocol):
    name: str           # "qwen3-fp16-baicai1145-1.7B"
    family: str         # "qwen3"
    variant: str        # "0.6B" / "1.7B" — 矩阵分组
    quant: str          # "fp16" / "int8" / "gguf-q4_k_m" / ...
    eos_ids: set[int]
    def load(self) -> None: ...
    def encode_audio(self, audio: np.ndarray) -> np.ndarray: ...
    def alloc_caches(self): ...
    def decoder_step(self, input_ids, audio_features, caches, cache_position) -> np.ndarray: ...
```

每个 adapter 文件暴露 `def discover() -> list[Backend]`,CLI 枚举所有 backend
就遍历 `backends/qwen3_*.py` 模块的 `discover()`。

### 结果存储策略(本节是第二阶段(框架正规化) 的 ADR 决定)

- **`benchmarks/results/*.json` / `*.md` 默认 gitignore** —— 每次跑都生成
  `<timestamp>_<git_sha>.json/.md`,git history 不污染
- **`benchmarks/results/baselines/` 不 gitignore** —— 人为决定"这次结果作为
  基准入库",manual move + commit。文件名约定:
  `<YYYY-MM-DD>_<machine-tag>_<context>.{json,md}` 例如
  `2026-04-28_apple-silicon_round38.json`
- 每个 run 文件头包含机器指纹(`platform.platform()` / chip / ort.\_\_version\_\_ /
  intra/inter op num_threads)+ git SHA + dirty flag,出问题可追溯

第一阶段(spike) 的 baseline 数据迁移到 `benchmarks/results/baselines/2026-04-28_apple-silicon_round38.json` +
对应 .md。`docs/38-*/baseline_results.{md,json}` 留 placeholder 文件指过去
(避免双源)。

### CLI 形态

```bash
# 跑全量(默认 backend 矩阵 × 默认 fixture 矩阵)
uv run python -m benchmarks

# 跑指定 backend 全部 fixture
uv run python -m benchmarks --backend qwen3-fp16-baicai1145-1.7B

# 跑某 backend × 某 fixture
uv run python -m benchmarks --backend qwen3-fp16-baicai1145-1.7B --fixture medium

# 自定义输出位置(默认 benchmarks/results/<timestamp>_<sha>.{json,md})
uv run python -m benchmarks --output benchmarks/results/baselines/2026-04-28_xxx
```

`--backend` / `--fixture` 都支持 prefix-match + 多个用逗号分隔。

### 跟 spike 的关系

- 不删 `docs/38-*/benchmark.py` —— 留作 round 38 spike 历史快照,有自己的
  导入路径独立运行
- `docs/38-*/baseline_results.{md,json}` 改成 placeholder pointer 文件
  (内容只 1 行 + `这份数据已迁移到 benchmarks/results/baselines/2026-04-28_apple-silicon_round38.{md,json}`),
  避免选型时读到过时数据

### 跟 tests/ 的关系

- 不进 pytest,不进 CI(跑一次太重 + 要 ~7GB 模型 cache)
- 共用 `tests/fixtures/*.wav` —— `benchmarks/fixtures.py` 直接 `Path(__file__).parent.parent / "tests/fixtures"`
- 两边目录平级,不互相 import。benchmarks 可以 import `daobidao` 的
  `_feature` / `_tokenizer` / `_prompt` / `_postprocess`(产品代码,稳定)

### 实现步骤

1. 写 `benchmarks/` 骨架(config / harness / fixtures / reporting / __main__ / backends/base.py)
2. 把 spike 里 `Int8Runner` 抽到 `backends/qwen3_int8_zengshuishui.py`,
   暴露 `discover()` 返 0.6B + 1.7B 两个 backend instance
3. 把 spike 里 `Fp16Runner` 抽到 `backends/qwen3_fp16_baicai1145.py`,同上
4. CLI 接 `--backend` / `--fixture` / `--output`(argparse,prefix match)
5. 跑一次全量,把结果 commit 进 `results/baselines/2026-04-28_apple-silicon_round38.{json,md}`
6. 把 `docs/38-*/baseline_results.{md,json}` 替换成 pointer
7. 写 `benchmarks/README.md`(怎么跑、怎么加 backend、baseline 命名规范)
8. `.gitignore` 加 `benchmarks/results/*.json` + `benchmarks/results/*.md`
   但允许 `benchmarks/results/baselines/`

### 第二阶段(框架正规化) 验收标准

- [ ] `uv run python -m benchmarks` 跑出跟 spike 等价的 12 case 数据(±5% 噪音)
- [ ] `uv run python -m benchmarks --backend qwen3-fp16-baicai1145-1.7B --fixture medium`
  能精确跑单 case
- [ ] `benchmarks/results/baselines/2026-04-28_apple-silicon_round38.{json,md}`
  入库,内容跟 docs/38 旧 baseline 数据一致(±噪音)
- [ ] `benchmarks/README.md` 含:跑法 / 加 backend 流程 / baseline 命名规范 / 跟
  第一阶段(spike) spike 的关系说明
- [ ] `.gitignore` 排除 `results/*.{json,md}` 但保留 `results/baselines/`
