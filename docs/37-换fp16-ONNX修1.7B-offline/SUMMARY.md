# Round 37 — 换 baicai1145 fp16 ONNX 修 1.7B offline 翻车

## 开发项背景

issue [#7](https://github.com/pkulijing/daobidao/issues/7) 主体问题:1.7B 在 offline `transcribe()` 路径上**确定性返空字符串**,且这个失败模式跨 audio / 长度都会触发(zh.wav 切到 168800 samples 翻车、zh_long 切到 5s 翻车),0.6B 同输入稳。

issue 第一阶段(settings_server fixture race)在 commit `37284ff` 已收掉,本轮处理主体。

### 真实症状(本轮 spike 推翻原 issue body 的描述)

issue body 原写"long prompt(~800 token)int8 量化在不同 runner SKU 上数值不稳定,greedy 第 1 个 token 偶发翻 EOS"。spike 实测推翻几条关键假设:

- 不是概率性,是**audio-内容相关的确定性翻车**: zh.wav 切到 168500 samples (10.5312s) 在 1.7B 上完整正确文本;切到 168800 samples (10.5500s) 立即返空。差 300 samples (≈0.019s)
- 不是 prompt 长短问题:zh_long 5s 翻车,8s ~ 28s 全部正常 — zh.wav 翻车的 10.5s 区间反而稳过
- 同一 audio array、同样 mel/audio_features tensor shape、同样 prompt_len,只是数值差一点,1.7B 输出截然不同
- 失败案例的 EOS-vs-top1 logit gap 从典型 25+ 压到 0 甚至负,临界 token 互相吃掉
- 0.6B 在同样输入上稳,且 audio_features 扰动幅度甚至**比 1.7B 还大**(max\|Δ\|=4.5e-2 vs 1.7B 的 1.7e-2)— 1.7B int8 量化对 prefill logits 数值组合的敏感度才是真因

### 产品影响

产品默认 `streaming_mode=True`,实际推理走 [_stream.py](src/daobidao/stt/qwen3/_stream.py) chunked prefill 路径,不走整段 offline `transcribe()`。**用户日常不踩这条 path** — 只有用户主动在 settings 关掉 streaming + 选 1.7B 才会确定性返空。但:
1. 测试 `test_transcribe_zh_wav[1.7B]` 测的就是 offline path,用 `DAOBIDAO_SKIP_E2E_STT=1` 兜底等于把这片"产品功能区域"完全藏起来不测
2. 跨 CPU 微架构表现不一致(Linux x86 PASS、Mac ARM/CI 抽 SKU FAIL)是同一根因在不同硬件上的不同概率显现

## 实现方案

### 关键设计

把 0.6B / 1.7B 的 ONNX 包都换到 [`baicai1145/Qwen3-ASR-{0.6B,1.7B}-ONNX`](https://modelscope.cn/models/baicai1145/Qwen3-ASR-1.7B-ONNX)。该 export 是 fp16 量化,数值精度比 int8 高一档,spike 实测在原翻车点全过。

| 维度 | zengshuishui (老) | baicai1145 (新) |
|---|---|---|
| 量化 | int8 weight quant | fp16 |
| Repo | 单一 `zengshuishui/Qwen3-ASR-onnx`,内部 `model_{variant}/` 子目录 | 两个独立 repo per-variant |
| ONNX session | 3(`conv_frontend` + `encoder.int8` + `decoder.int8`) | 2(`encoder.onnx` + `decoder.onnx`,conv 焊进 encoder) |
| Tokenizer 位置 | `cache_root/tokenizer/` | `cache_root/` 直接根目录平铺 |
| KV cache shape | `(B, T=1200, H, D)` 时间 axis=1 | `(B, H, T=1664, D)` 时间 axis=2 (HF 风格) |
| KV cache dtype | float32 | float16 |
| KV 写回方式 | 输出 `key_delta_X`,scatter 进预分配 cache | 输出整段 `present_key/value_XX`,直接覆盖 cache 引用 |
| logits shape | `(B, seq, vocab)` | `(B, vocab)` 只输出最后一位置 |
| EOS | 单 `151645` | 双 `[151645, 151645]` 错,实际是 `[151645, 151643]` (`<|im_end|>` + `<|endoftext|>`) |
| Audio pad 策略 | 30s pad fixed-shape mel | chunk-aligned (window=100 帧),任意长度 |

为了对外 API 兼容,`Qwen3ONNXRunner.decoder_step` 内部 `unsqueeze(axis=1)` 把 `(B, vocab)` 包成 `(B, 1, vocab)`,callers 仍用 `logits[0, -1]` 拿最后位置 — 老代码零改动。

### Spike 数据

| Case | int8 (老) | fp16 (新 baicai1145) |
|---|---|---|
| zh.wav full (10.56s) on 1.7B | **'' FAIL** | "先帝创业未半而中道崩殂…" PASS |
| zh_long[:5s] on 1.7B | **'' FAIL** | "在近代中国,既有中西文化交流…" PASS |
| zh.wav[:10.5s] on 1.7B (对照) | PASS | PASS (对照通过) |
| zh.wav full on 0.6B (sanity) | PASS | PASS |

性能:1.7B fp16 RTF ~0.55,0.6B fp16 RTF ~0.24(均 Apple Silicon CPU)。RTF 比 int8 慢 ~2-3x,但 offline 路径下绝对延迟仍在可接受范围(10s 音频 ~5.9s 推理)。streaming 单 chunk 处理时间 ~3-4s 略勉强(< CHUNK_SIZE=2s 是预算,实测略超),本轮验证 streaming 端到端测试稳过即可,性能优化留给后续。

### 开发内容概括

按 PLAN.md 阶段 B 走完了 7 个子任务:
1. **`_onnx_runner.py`**:从 3-session 改 2-session,read metadata.json 拿 num_layers/cache_len/audio_output_dim/eos_ids 不 hardcode,KV cache shape/scatter 全适配
2. **`qwen3_asr.py`**:`REPO_ID` → `REPO_ID_BY_VARIANT` dict,`allow_patterns` / cache 路径 / 文件名全改,`transcribe()` / `_warmup` 改用 `runner.eos_ids` 集合判 EOS,删掉 `pad_or_trim` 调用
3. **`_stream.py`**:`Qwen3StreamState.eos_id` → `eos_ids: tuple[int, ...]`,`init_stream_state` 从 `runner.eos_ids` 拿,`_greedy_decode` 用 `eos_ids` set 判 EOS
4. **`_download_manager.py`**:`REPO_ID` → per-variant dict,`REPO_OWNER_NAME_BY_VARIANT` per-variant,`REQUIRED_FILES` / `_ALLOW_PATTERNS` 改 baicai1145 layout,`_cache_lookup(variant, rel_path)` 加 variant 参数
5. **测试套**:`tests/conftest.py` fixture 路径改(`qwen3_*_model_dir` / `qwen3_tokenizer_dir` 整合为 `qwen3_0_6b_dir` / `qwen3_1_7b_dir`,直接等于 `stt.cache_root`),`test_qwen3_runner.py` 重写为 behavior-level 测试(删掉测内部 `key_delta_X` 命名 / `_inspect_*` 私有方法的 case),`test_qwen3_asr.py` / `test_qwen3_stream_smoke.py` / `test_qwen3_stream_sliding_real.py` 移除所有 `DAOBIDAO_SKIP_E2E_STT` skipif 兜底,`FakeRunner` 加 `eos_ids` 属性,`test_download_manager.py` 适配新 `_cache_lookup` 签名跟 REQUIRED_FILES
6. **CI**:`.github/workflows/build.yml` cache key `modelscope-qwen3-asr-v3` → `v4`(repo 完全换),`Run tests` step 删 `DAOBIDAO_SKIP_E2E_STT` env
7. **CLAUDE.md**:量化精度 / 模型大小数字 / repo 来源 / KV cache 描述 / encoder schema / tests 数字全更新

### 额外产物

- `docs/37-*/spike.py` — schema dump + 三段 audio 对照实测脚本,后续如要 spike 别的 ONNX export 可以直接复用
- 修复了 round 33 留下的 `DAOBIDAO_SKIP_E2E_STT` 兜底坑 — CI 终于能完整验证 STT 端到端

## 局限性

1. **fp16 推理慢 ~2-3x**:套件总耗时从 ~20s 涨到 ~8min(本机 Apple Silicon)。原因是 onnxruntime CPU EP 对 fp16 ARM 微架构没像对 int8 那样精细优化。streaming 单 chunk 推理 ~3-4s 比 CHUNK_SIZE_SEC=2s 略长,实时性可能边缘吃紧 — 真机要按住说话边讲边出字会比之前慢一拍
2. **测试套整体很慢**:8 分钟跑全套件,在本地裸跑很伤心。CI 端因为有 modelscope cache,主要慢在推理上,跑一次 GH Actions runner 也得 ~10min
3. **冷启动模型 load 时间**:1.7B fp16 decoder.onnx 加载 ~15-20s(int8 时是 ~3s),首次按热键到出字会卡一下
4. **没用上 GPU/MLX/CoreML 加速**:本轮 scope 限制,所有推理仍走 CPU EP

## 后续 TODO

[新 issue 已规划但未创建] — 按用户优先级给:

1. **(P0)推理速度优化**:fp16 慢 2-3x 是主要痛点。候选方向(具体哪条要再 spike):
   - onnxruntime CoreML EP (macOS) / DML EP (Windows) / CUDA EP (Linux 有 GPU) — 可能直接 2-5x 加速
   - 切到 GGUF + llama.cpp(macOS Metal backend),已有 [llama.cpp 原生 Qwen3-ASR 支持](https://github.com/ggml-org/llama.cpp/issues/21847),但同 issue 显示长音频也有返空 bug 风险
   - antirez 的 [qwen-asr C 实现](https://github.com/antirez/qwen-asr) — 性能很好但要换整个推理 backend
   - moona3k 的 [mlx-qwen3-asr](https://github.com/moona3k/mlx-qwen3-asr) — Apple Silicon 专用,跨平台支持差
2. **(P1)冷启动 1.7B decoder.onnx ~15s 加载时间**:看能否用 onnxruntime session pre-warmup / 序列化 EP 做缓存
3. **(P2)测试套提速**:跑 8 分钟太长,可以让 1.7B 端到端测试改 `pytest.mark.slow` 默认 skip、CI 只在 release 前跑;或者重新切片测试用 audio 让 1.7B 也用更短的 wav
4. **(P2)给 1.7B variant 用 dropdown 时加 "fp16 推理慢" 的提示文案**:当前 settings UI 在切 1.7B 时没说明速度差异
5. **回看 issue #7 是否能正式 close**:本轮 commit 末尾会写 `Closes #7`,GitHub 会自动 close — 但实际"长音频在某些 backend 上仍可能返空"的隐患没完全消除(参考 llama.cpp 同种 bug),从产品视角 1.7B offline 已稳,issue 关掉合理
