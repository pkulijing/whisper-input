# Round 37 — 实施计划

> 镜像 `~/.claude/plans/greedy-inventing-sutherland.md`(已批)。

## 实施分两阶段

### 阶段 A — Spike(写产品代码前必做)

目标:**实测 baicai1145 1.7B fp16 在两个失败 case 上是否给出非空合理文本**。Spike 翻车则停下重选方向(GGUF / MLX / antirez),不浪费阶段 B 的工程投入。

步骤:
1. 写 `docs/37-*/spike.py`(独立脚本,不动产品代码):
   - `modelscope.snapshot_download("baicai1145/Qwen3-ASR-1.7B-ONNX")` 拉全量
   - `onnxruntime.InferenceSession` 加载 `encoder.onnx` + `decoder.onnx`,**先 dump input/output schema**
   - 复用 `daobidao.stt.qwen3._feature.{log_mel_spectrogram, pad_or_trim}`
   - 复用 `_prompt.build_prompt` + `_tokenizer.Qwen3Tokenizer`(load 路径改新根目录布局)
   - 手写 minimal decode loop,支持双 EOS、cache_len=1664、KV cache 命名按实际 schema
2. 跑三段 audio 验证:
   - `zh.wav` 全长 (10.56s) — 期望非空 + 含 "先帝"+"益州"(原 int8 翻车点)
   - `zh_long.wav[:5s]` — 期望非空(原 int8 翻车点)
   - `zh.wav[:10.5s]` — 期望非空(原 int8 PASS,作 sanity)
3. 0.6B 在 zh.wav 全长 sanity check

**Spike 通过门槛**:三段 audio 都给出至少 30+ 字符的中文文本,且 zh.wav 全长输出含 "先帝"+"益州"。

**Spike 失败 fallback**:停下,数据写进 SUMMARY,issue #7 标记"已诊断未根治"。

### 阶段 B — 产品代码适配(Spike 通过后)

#### B1. `src/daobidao/stt/qwen3/_onnx_runner.py` — 改造为 2-session

- 删 `conv_frontend.onnx` 加载 + encode_audio 第一步
- `encode_audio` 改为单一 encoder.onnx 调用,input/output schema 按 spike 实测
- KV cache shape 保留,**读 metadata.json 拿 `num_layers` / `static_cache_len`**(减少 hardcode)
- `audio_feature_dim` 已是 introspection,不动
- 添加 `eos_ids: tuple[int, ...]` 属性

#### B2. `src/daobidao/stt/qwen3/qwen3_asr.py` — repo 切换

- `REPO_ID` → `REPO_ID_BY_VARIANT` dict
- `load()` 改 `snapshot_download` per-variant,改 `allow_patterns`,改 tokenizer/model 路径
- 文件名 `decoder.int8.onnx` → `decoder.onnx`
- `transcribe()` / `_warmup` / streaming 里 `next_id == eos_id` → `next_id in runner.eos_ids`

#### B3. `src/daobidao/stt/qwen3/_tokenizer.py`

- 暴露 `eos_ids` list(`[151645, 151643]`)

#### B4. `src/daobidao/stt/qwen3/_download_manager.py` — 文件清单刷新

- `REPO_ID` → dict
- `REQUIRED_FILES` 改成 baicai1145 文件结构
- `allow_patterns` 同 B2

#### B5. 测试套适配

- `tests/conftest.py` fixture 路径 (`stt.cache_root` 不再加 `/tokenizer` 或 `/model_{variant}`)
- 移除所有 `_SKIP_E2E` 用法,`DAOBIDAO_SKIP_E2E_STT` 全删

#### B6. CI 缓存

- `.github/workflows/build.yml` cache key `v3` → `v4`

#### B7. 文档

- `CLAUDE.md` 同步:模型来源 / 量化精度 / 模型大小 / dep 描述 / tests 数字
- `docs/37-*/SUMMARY.md` 写完整 SUMMARY
- commit `Closes #7`

## 验收

```bash
# 1. 1.7B offline 不再翻车
uv run python -c "
from pathlib import Path
from daobidao.stt.qwen3 import Qwen3ASRSTT
stt = Qwen3ASRSTT(variant='1.7B'); stt.load()
text = stt.transcribe(Path('tests/fixtures/zh.wav').read_bytes())
assert '先帝' in text and '益州' in text, repr(text)
print('OK:', text)
"

# 2. 测试套不需要 SKIP_E2E,5 轮全过
for i in $(seq 1 5); do
  uv run pytest tests/test_qwen3_asr.py tests/test_qwen3_runner.py \
    tests/test_qwen3_stream_smoke.py tests/test_qwen3_stream_sliding_real.py \
    --no-cov -q 2>&1 | tail -1
done

# 3. CI ubuntu runner 全 PASS
```

## 风险点

1. encoder.onnx schema 跟假设不符 → spike 暴露,换 fp32 包等
2. fp16 CPU 推理太慢以至于 streaming 实时性挂 → fallback 双 backend 共存(offline fp16 / streaming int8)
3. fp16 也复现长音频返空(模型本身弱点)→ 标"已诊断未根治",改方向

## 不在 scope

- 推理速度优化(GPU EP / CoreML / Metal / MLX)
- GGUF / llama.cpp / antirez 替换
- streaming 行为优化
- 产品 settings UI 改动
