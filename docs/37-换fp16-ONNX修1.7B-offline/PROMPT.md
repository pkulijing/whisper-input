> 来自 [#7 1.7B 端到端测试在非 Linux x86 上不稳定](https://github.com/pkulijing/daobidao/issues/7)
> Labels: `type:bug` `area:test` `priority:P0`
>
> issue 第一阶段(settings_server fixture race)已在 commit `37284ff` 收掉,本轮处理 issue 主体 —— 1.7B 离线模式确定性翻车。

## 背景

issue #7 原 body 把症状归因到"GH Actions 不同 SKU 上 int8 量化推理不稳定,greedy 第 1 个 token 偶发翻 EOS",并以 `DAOBIDAO_SKIP_E2E_STT=1` 兜底跳过 4 个相关测试。本次会话通过定向 spike 推翻了这套描述的几条关键假设,事实重述如下:

### 真实症状

- **不是概率性,是 audio-内容相关的确定性翻车**:
  - `zh.wav` 切到 168500 samples (10.5312s) 在 1.7B 上能给出完整正确文本;切到 168800 samples (10.5500s) 立即返空字符串。差 300 samples (≈0.019s)
  - `zh_long.wav` 切到 5s 翻车,8s ~ 28s 全部正常 —— 在 zh.wav 翻车的 10.5s 区间反而稳过
  - 同一份 audio array、同样的 mel/audio_features tensor shape、同样的 prompt_len,只是数值差一点,1.7B 输出截然不同
- **跟 prompt 长度无关**:zh_long 5s prefill 跟 12s prefill 的 prompt_len 几乎一样,5s 翻 12s 不翻
- **0.6B 在同样输入上稳**:0.6B 的 audio_features 扰动幅度甚至**比 1.7B 还大**(max\|Δ\|=4.5e-2 vs 1.7B 的 1.7e-2),但 0.6B 给完整正确文本

### 失败机制(prefill logits 解构)

两个失败 case 在 prefill 后的 token 生成轨迹:

| 步 | zh.wav full (10.56s) | zh_long[:5s] | zh.wav[:10.5s] (PASS) |
|---|---|---|---|
| 0 | `' language'` (logit 20.55, EOS rank 310) | **EOS top-1** (gen=0) | `'language'` (logit 32.67, EOS rank 66) |
| 1 | `' None'` (logit 31.10) | - | `' Chinese'` (logit 38.47) |
| 2 | `'<asr_text>'` | - | `'<asr_text>'` |
| 3 | **EOS top-1** | - | `'先'` (logit 36.70) |

通过 case 在 step 1 永远是 `' Chinese'`(语种识别成功),失败 case 给 `' None'`(模型自己说"不知道是什么语种"),进而 asr_text 内容那一步直接 EOS。或者更早,prefill 完直接 EOS。

两个失败模式表面不同(早死 vs 晚死),本质都是**1.7B int8 prefill logits 在特定 audio 数值组合上的置信度退化**:正常时 top-1 跟 EOS 的 logit gap 在 25+,失败时 gap 压到 0 或更小,临界 token 互相吃掉。

### 产品影响

产品默认 `qwen3.streaming_mode=True`,实际推理走 [_stream.py](src/daobidao/stt/qwen3/_stream.py) 的 chunked prefill 路径,不走 `transcribe()` 整段离线 prefill。**用户日常不踩这条 path**,issue 描述的"不影响实际用户体验"成立。但:

1. 产品 settings UI 仍提供"关闭流式模式"开关,用户主动关掉 + 选 1.7B 后会确定性返空
2. 测试 `test_transcribe_zh_wav[1.7B]` 测的就是这条路径,用 `DAOBIDAO_SKIP_E2E_STT=1` 兜底相当于把这片"产品功能区域"完全藏起来不测
3. 跨 CPU 微架构表现不一致(原 issue 中 Linux x86 PASS、Mac ARM/CI 抽 SKU FAIL)是**同一根因在不同硬件上的不同概率显现**,不是几套独立的 bug

## 希望达到

让 1.7B 在 offline `transcribe()` 路径上稳定工作,达到跟 0.6B 同档次的可靠度,并:

- **测试套不再需要 `DAOBIDAO_SKIP_E2E_STT` 环境变量,4 个端到端 STT 测试可以无条件 enable**
- **CI ubuntu runner 上同样稳过**(原 issue 描述的"runner 抽签翻车")
- 不引入新的产品契约弱化(不让"关 streaming + 1.7B"成为"不支持"的组合)

**本轮聚焦正确性,识别速度不在 scope**(用户明确要求)。fp16 在 CPU 上比 int8 慢 ~2-3x,1.7B 离线 10s 音频可能从 ~2s 涨到 ~5-6s,接受。后续若需性能优化(比如 GPU EP / MLX / GGUF backend 替换),再开新一轮。

## 候选方向

| 方向 | 改动量 | 性能 | 数值精度 | 风险 |
|---|---|---|---|---|
| **baicai1145/Qwen3-ASR-1.7B-ONNX (fp16)** | 中 — runner 适配 + 双 EOS + cache_len 改 | CPU 比 int8 慢 2-3x | fp16 完整精度 | export 工艺未知,可能仍踩同种长音频 bug |
| antirez/qwen-asr (C 实现) | 大 — 换 inference backend + Python binding | 优(C native) | 看具体量化方案 | 推理 backend 替换工程量大 |
| moona3k/mlx-qwen3-asr (MLX) | 大 — Apple Silicon only | 优(GPU 加速) | fp16 native | 平台分裂,Linux 用户无路径 |
| llama.cpp + GGUF | 大 | 优 | Q4_K_M / Q5_K_M (per-block) | [llama.cpp #21847](https://github.com/ggml-org/llama.cpp/issues/21847) 同种长音频返空 bug 已记录 |

**采纳 baicai1145 fp16 ONNX 路径**,理由:

1. 改动量最小,只换 ONNX 文件 + 适配 runner,不动产品上层任何代码 / 测试 / 配置 schema
2. fp16 在 ONNX 标准量化路径上数值精度比 int8 显著高,同时跟 zengshuishui int8 是同一系导出血统(都是 Qwen3-ASR 官方权重过 Whisper 风格 preprocess),容易类比 / 回退
3. metadata.json 已经把 schema(audio_pad_id / num_layers / cache_len / EOS list / preprocessor 配置)全暴露,没有黑盒
4. 如果 fp16 也复现长音频空,直接坐实"长音频返空是模型/export 弱点,跟 backend/量化无关",此时再考虑 GGUF / MLX 才是有数据支撑的决策。否则前期就猛冲 GGUF 一旦同样踩,工程投入白瞎

## scope 边界

- **包含**:换 baicai1145 ONNX 包(0.6B + 1.7B 都换,避免分裂维护两个 export 来源);适配 runner(无 conv_frontend、双 EOS、cache_len=1664);适配 prompt builder 跟 metadata 对齐;移除 `DAOBIDAO_SKIP_E2E_STT` 兜底;CI 验证;UI 无可见改动
- **不包含**:性能优化(慢 2-3x 接受);GPU/Metal/CoreML EP 评估;切换到 GGUF / MLX / antirez backend;改 streaming 路径(产品已稳)

## 验收

1. `uv run pytest tests/test_qwen3_asr.py::test_transcribe_zh_wav[1.7B]` 在 Mac 上裸跑稳过(连跑 5 次全过)
2. zh.wav 全长 + zh_long[:5s] + zh_long[:13s] 三段 1.7B offline transcribe 都给出非空合理文本
3. 测试套移除 `DAOBIDAO_SKIP_E2E_STT` 跳过后,`uv run pytest` 全部 PASS
4. CI ubuntu runner 跑同样套件全 PASS
5. 0.6B 离线 + streaming 路径无回归
6. 1.7B streaming 路径无回归(`test_streaming_via_full_whisperinput_pipeline[1.7B]` 等)
