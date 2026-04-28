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

> **本分支不可上线**。用户真机实测推理速度慢到完全不能接受 —— 按住说话出字延迟、松开后等到崩溃感是常态;0.6B 还能将就,1.7B 在 Apple Silicon CPU 上几乎卡到不可用。本轮成果只能作为下一轮性能优化的**起点暂存**,不能 push 到 master,不能直接发版。issue #7 不在本轮关闭(commit message 里的 `Closes #7` 应改成 `Refs #7`,见下文)。

1. **fp16 推理 实测慢到无法接受**:onnxruntime CPU EP 对 fp16 在 Apple Silicon ARM 微架构上没精细优化,实测 1.7B 单次离线 transcribe 10s 音频 ~5.9s(RTF 0.55),streaming 单 chunk(2s 真音频)处理 ~3-4s 比窗口本身长 — 真机操作"按住说话边讲边出字"延迟感肉眼可见。这才是阻塞 ship 的真正问题
2. **测试套整体 ~8min**(int8 时代 ~20s)。本地裸跑严重劝退,CI 也会从 ~3min 涨到 ~10min
3. **冷启动模型 load**:1.7B fp16 decoder.onnx 首次 load ~15-20s(int8 ~3s),首次按热键到出字会卡一下
4. **代码 ↔ 文档暂时不一致**:rebase 阶段为反映"本分支不会 ship"的事实,`CLAUDE.md` / `tests/test_download_manager.py` 仍保留 zengshuishui int8 的描述/断言。这是有意的 — 等下一轮性能优化通过、可以真正 ship 时,再把这两份文档一并更到 baicai1145 fp16 状态。当前分支跑 `test_download_manager.py` 会 fail(REQUIRED_FILES assert 跟代码对不上),已知,留在那当 ship gate

## 后续 TODO

1. **(P0,真正阻塞)推理速度优化轮**:必须让 1.7B fp16 推理速度回到接近 int8 量级或更好,不然本轮成果直接丢失。候选方向需要专门 spike:
   - onnxruntime **CoreML EP** (Apple Silicon) — 最对口、改动最小,理论可 2-5x 加速。先 spike 这个
   - onnxruntime **CUDA EP** (有显卡的 Linux 用户) — 服务端 / 个人工作站场景
   - 切 backend 到 [llama.cpp + GGUF](https://github.com/ggml-org/llama.cpp/issues/21847)(原生支持 Qwen3-ASR,有 Metal 后端,但同 issue 显示长音频可能返空)、[antirez/qwen-asr](https://github.com/antirez/qwen-asr) (C 实现) 或 [moona3k/mlx-qwen3-asr](https://github.com/moona3k/mlx-qwen3-asr) (MLX) — 改动量大,fallback
   - 实在不行考虑回滚到 zengshuishui int8 + 仅保留 0.6B(放弃 1.7B variant) — 用户体验上 0.6B 已够用且性能能接受
2. **(P1)冷启动 1.7B decoder.onnx ~15s 加载**:onnxruntime session pre-warmup / 序列化 EP 缓存
3. **(P2)测试套提速**:1.7B 端到端测试 `pytest.mark.slow` 默认 skip、CI release 前再跑;或重新切片让 1.7B 也用更短 wav
4. **(P2)settings UI**:在 1.7B variant dropdown 加"fp16 推理慢"提示文案

### Issue #7 处理

Commit `6dec467` 的 message 写了 `Closes #7`,但**本分支不会 push 到 master**(性能不达标)。issue #7 由"不 merge"自然不会被 GitHub 自动 close — 人为防呆。下一轮性能优化做完,再决定是合并这两轮一起带 `Closes #7` 还是分开关。本 finish 步骤的新 commit message 用 `Refs #7` 不写 Closes,避免误关。
