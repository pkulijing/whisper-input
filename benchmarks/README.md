# benchmarks/

Daobidao 推理性能 benchmark 框架 —— 跟 `tests/` 同级的顶层目录,**重而长**
(跑一次 ~3-15 min,需要 ~7 GB 模型 cache),**不进 pytest 也不进 CI**,
独立 CLI 跑。

## 用途

后续做模型选型(候选 backend:CoreML EP / CUDA EP / GGUF / MLX / 或回滚
int8)时,每来一个候选都用同一套 fixture + 同一份 harness 测出可比数字,
入 `results/baselines/` 长期累积。区别于 `docs/<轮次>/` 下的 spike 脚本
(一次性、不维护)。

## 跑法

```bash
# 跑全量(默认 backend × 全 fixture)
uv run python -m benchmarks

# 跑指定 backend(prefix match,逗号分隔)
uv run python -m benchmarks --backend qwen3-fp16

# 单 case
uv run python -m benchmarks --backend qwen3-fp16-baicai1145-1.7B --fixture medium

# 自定义输出位置(stem,不带后缀)
uv run python -m benchmarks --output benchmarks/results/baselines/2026-04-28_apple-silicon_round38

# 减少重复次数(只跑 warmup + 1 次,适合 debug)
uv run python -m benchmarks --repeats 1
```

输出默认写到 `benchmarks/results/<UTC stamp>_<git sha7>.{json,md}`。

## 目录结构

```
benchmarks/
  __init__.py
  __main__.py          CLI 入口
  config.py            FIXTURES_BY_DEFAULT / N_REPEATS / DEFAULT_BACKEND_MODULES
  fixtures.py          AudioFixture 定义,共用 tests/fixtures/*.wav
  harness.py           transcribe 计时 + median 聚合
  reporting.py         records → JSON + Markdown(机器指纹 / 表格 / per-case)
  backends/
    base.py            Backend 协议
    qwen3_int8_zengshuishui.py    int8 (round 26-36)
    qwen3_fp16_baicai1145.py      fp16 (round 37+)
    # 未来:qwen3_fp16_coreml.py 等
  results/
    baselines/         人为标记的"基线",commit 进 git
    *.{json,md}        每次跑的产物,gitignore
```

## Backend 协议

每个 adapter 文件实现一个或多个 `Backend`(见 [backends/base.py](backends/base.py)):

```python
class Backend(Protocol):
    name: str           # CLI prefix-match 用,例 "qwen3-fp16-baicai1145-1.7B"
    family: str         # 同 family 才直接对比,例 "qwen3"
    variant: str        # 矩阵分组,例 "0.6B" / "1.7B"
    quant: str          # 例 "fp16" / "int8" / "gguf-q4_k_m"
    eos_ids: set[int]
    def load(self) -> None: ...
    def encode_audio(self, audio: np.ndarray) -> np.ndarray: ...
    def alloc_caches(self): ...
    def decoder_step(self, input_ids, audio_features, caches, cache_position) -> np.ndarray: ...
```

`logits` 必须保证 `logits[0, -1]` 取得到最后位置 vocab(shape `(1, seq, vocab)`
或 `(1, 1, vocab)`)。adapter 可在 `load()` 里给 `self.tokenizer = ...`,
harness 自动用它 build prompt。

## 加新 backend 流程

1. 在 `benchmarks/backends/` 加 `qwen3_<quant>_<source>.py`
2. 类实现 `Backend` 协议,模块暴露 `def discover() -> list[Backend]`
3. 把模块路径加进 `benchmarks/config.py` 的 `DEFAULT_BACKEND_MODULES`
4. `uv run python -m benchmarks --backend <new-prefix>` 跑一遍验证
5. 如果想入基线:`uv run python -m benchmarks --output benchmarks/results/baselines/<date>_<machine>_<context>` + git commit

## 结果文件命名

- 普通跑:`results/<UTC ISO 8601 stamp>_<git sha7>.{json,md}` —— gitignore
- 入库基线:`results/baselines/<YYYY-MM-DD>_<machine-tag>_<context>.{json,md}`
  例如:
  - `2026-04-28_apple-silicon_round38.{json,md}`
  - `2026-05-15_apple-silicon_coreml-spike.{json,md}`

每个 JSON 文件头都含 `fingerprint`(platform / chip / onnxruntime version /
git SHA + dirty flag / UTC timestamp),跨机对比看清楚环境。

## Fixture 设计

见 [fixtures.py](fixtures.py)。三段切片:`short` (5s, zh.wav)、
`medium` (10.5s, zh.wav)、`long` (25s, zh_long.wav)。挑选时避开 round 37
SUMMARY 列出的 1.7B int8 翻车谱(`zh.wav` 完整 / `zh_long[:5s]`),且不超
30s(老 int8 静态 cache `max_total_len=1200` 装不下)。

如果加 fixture:在 [fixtures.py](fixtures.py) 的 `FIXTURES` 列表新增
`AudioFixture(slug, wav_path, n_samples)`,共用 `tests/fixtures/*.wav` 不
复制文件。

## 跟 tests/ 的关系

- 不进 pytest,不进 CI(跑一次太重 + 要 ~7 GB cache)
- 共用 `tests/fixtures/*.wav`,fixtures.py 直接相对路径引用
- 共用 `daobidao` 产品代码的 stable utility(`_feature` / `_tokenizer` /
  `_prompt` / `_postprocess`)—— harness build prompt + decode token 全靠它们

## 跟 docs/<轮次>/ 的 spike 脚本的关系

`docs/<轮次>/spike.py` / `docs/<轮次>/benchmark.py` 是"一次性 + 跟某轮次
紧绑定"的 spike,有 round 历史价值,不删。`benchmarks/` 是"长期可重复 +
跨轮次累积"的工具。新 backend 候选先在 spike 里探,跑通了再升进 `benchmarks/`
adapter。
