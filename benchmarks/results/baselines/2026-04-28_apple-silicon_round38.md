# Round 38 阶段 A baseline — int8 vs fp16, 0.6B + 1.7B (fp16-1.7B re-run)

## 环境指纹

```
platform: macOS-15.3.2-arm64-arm-64bit
machine: arm64
processor: arm
python_version: 3.12.13
onnxruntime_version: 1.24.4
git_sha: f2cecce1f432e64479a5cbeea8b5221c43a77302
git_dirty: True
timestamp_utc: 2026-04-28T08:02:10.710168+00:00
note: fp16-baicai1145-1.7B 三条数据来自 2026-04-28 隔离重测(repeats=7 warmup=3),其余记录来自原始全量 run(repeats=3 warmup=1)
isolation_rerun_fingerprint: {'platform': 'macOS-15.3.2-arm64-arm-64bit', 'machine': 'arm64', 'processor': 'arm', 'python_version': '3.12.13', 'onnxruntime_version': '1.24.4', 'git_sha': 'f2cecce1f432e64479a5cbeea8b5221c43a77302', 'git_dirty': True, 'timestamp_utc': '2026-04-28T08:17:04.577314+00:00'}
```

## RTF(real-time factor = total_s / audio_seconds)

| | **short** | **medium** | **long** |
|---|---|---|---|
| **qwen3-int8-zengshuishui-0.6B** | 0.29 | 0.17 | 0.12 |
| **qwen3-int8-zengshuishui-1.7B** | ⚠️ FAIL (0.40) | 0.32 | 0.21 |
| **qwen3-fp16-baicai1145-0.6B** | 0.25 | 0.24 | 0.26 |
| **qwen3-fp16-baicai1145-1.7B** | 0.58 | 0.56 | 0.58 |

## Total 推理时间(秒)

| | **short** | **medium** | **long** |
|---|---|---|---|
| **qwen3-int8-zengshuishui-0.6B** | 1.45 | 1.83 | 2.97 |
| **qwen3-int8-zengshuishui-1.7B** | ⚠️ FAIL (2.00) | 3.34 | 5.34 |
| **qwen3-fp16-baicai1145-0.6B** | 1.27 | 2.53 | 6.50 |
| **qwen3-fp16-baicai1145-1.7B** | 2.91 | 5.87 | 14.52 |

## 同 family + variant 不同 quant 的 slowdown 比

| | **short** | **medium** | **long** |
|---|---|---|---|
| **qwen3-0.6B** fp16 / int8 | 0.88× | 1.38× | 2.19× |
| **qwen3-1.7B** fp16 / int8 | 1.45× | 1.75× | 2.72× |

## Encode 时间(秒)

| | **short** | **medium** | **long** |
|---|---|---|---|
| **qwen3-int8-zengshuishui-0.6B** | 0.52 | 0.52 | 0.53 |
| **qwen3-int8-zengshuishui-1.7B** | ⚠️ FAIL (0.65) | 0.66 | 0.69 |
| **qwen3-fp16-baicai1145-0.6B** | 0.17 | 0.33 | 0.82 |
| **qwen3-fp16-baicai1145-1.7B** | 0.21 | 0.46 | 1.12 |

## Prefill 时间(秒)

| | **short** | **medium** | **long** |
|---|---|---|---|
| **qwen3-int8-zengshuishui-0.6B** | 0.54 | 0.53 | 0.54 |
| **qwen3-int8-zengshuishui-1.7B** | ⚠️ FAIL (1.24) | 1.24 | 1.28 |
| **qwen3-fp16-baicai1145-0.6B** | 0.26 | 0.49 | 1.17 |
| **qwen3-fp16-baicai1145-1.7B** | 0.67 | 1.38 | 3.26 |

## Decode 时间(秒,生成所有 token 的总和)

| | **short** | **medium** | **long** |
|---|---|---|---|
| **qwen3-int8-zengshuishui-0.6B** | 0.40 | 0.78 | 1.90 |
| **qwen3-int8-zengshuishui-1.7B** | ⚠️ FAIL (0.13) | 1.45 | 3.39 |
| **qwen3-fp16-baicai1145-0.6B** | 0.84 | 1.71 | 4.54 |
| **qwen3-fp16-baicai1145-1.7B** | 2.03 | 4.02 | 10.13 |

## 运行离散度(total_s 的 N 次正式 run 统计 —— median 之外的可信度参考)

| backend / fixture | runs | median | min | max | std | cv |
|---|---|---|---|---|---|---|
| qwen3-int8-zengshuishui-0.6B/short | [1.46, 1.45, 1.45] | 1.45s | 1.45s | 1.46s | 0.01s | 0.5% |
| qwen3-int8-zengshuishui-0.6B/medium | [1.82, 1.83, 1.88] | 1.83s | 1.82s | 1.88s | 0.03s | 1.7% |
| qwen3-int8-zengshuishui-0.6B/long | [2.97, 2.99, 2.97] | 2.97s | 2.97s | 2.99s | 0.01s | 0.4% |
| qwen3-int8-zengshuishui-1.7B/short | [2.13, 1.99, 2.00] | 2.00s | 1.99s | 2.13s | 0.08s | 3.8% |
| qwen3-int8-zengshuishui-1.7B/medium | [3.27, 3.34, 3.42] | 3.34s | 3.27s | 3.42s | 0.07s | 2.2% |
| qwen3-int8-zengshuishui-1.7B/long | [5.34, 5.32, 5.39] | 5.34s | 5.32s | 5.39s | 0.04s | 0.7% |
| qwen3-fp16-baicai1145-0.6B/short | [1.27, 1.27, 1.27] | 1.27s | 1.27s | 1.27s | 0.00s | 0.1% |
| qwen3-fp16-baicai1145-0.6B/medium | [2.52, 2.53, 2.54] | 2.53s | 2.52s | 2.54s | 0.01s | 0.3% |
| qwen3-fp16-baicai1145-0.6B/long | [6.50, 6.50, 6.68] | 6.50s | 6.50s | 6.68s | 0.11s | 1.6% |
| qwen3-fp16-baicai1145-1.7B/short | [2.89, 2.89, 2.90, 2.91, 2.93, 2.92, 2.93] | 2.91s | 2.89s | 2.93s | 0.02s | 0.6% |
| qwen3-fp16-baicai1145-1.7B/medium | [5.79, 5.82, 5.84, 5.87, 5.87, 5.89, 5.90] | 5.87s | 5.79s | 5.90s | 0.04s | 0.7% |
| qwen3-fp16-baicai1145-1.7B/long | [14.76, 14.41, 14.44, 14.52, 14.48, 14.53, 14.54] | 14.52s | 14.41s | 14.76s | 0.11s | 0.8% |

## Per-case 详情

- **qwen3-int8-zengshuishui-0.6B/short** (5.00s): status=PASS gen_tokens=17 n_audio=390
  - encode=0.52s prefill=0.54s decode=0.40s total=1.45s (min 1.45 / max 1.46 / std 0.01 / cv 0.5%) rtf=0.29
  - text: '先帝创业未半而中道崩殂，今天下。'
- **qwen3-int8-zengshuishui-0.6B/medium** (10.50s): status=PASS gen_tokens=33 n_audio=390
  - encode=0.52s prefill=0.53s decode=0.78s total=1.83s (min 1.82 / max 1.88 / std 0.03 / cv 1.7%) rtf=0.17
  - text: '先帝创业未半而中道崩殂，今天下三分，益州疲弊，此诚危急存亡之秋也。'
- **qwen3-int8-zengshuishui-0.6B/long** (25.00s): status=PASS gen_tokens=79 n_audio=390
  - encode=0.53s prefill=0.54s decode=1.90s total=2.97s (min 2.97 / max 2.99 / std 0.01 / cv 0.4%) rtf=0.12
  - text: '在近代中国，既有中西文化交流，又有新陈制度代谢。文化变迁与制度新格的关系就很有趣了。制度上的现代化与文化上的西化，当时往往混在一起，而且后者有时还被看成是最为深刻的变化。过去有种流行的说法，说是近代中西交往是个三阶段的过程：洋务运动时代，学'
- **qwen3-int8-zengshuishui-1.7B/short** (5.00s): status=FAIL gen_tokens=3 n_audio=390
  - encode=0.65s prefill=1.24s decode=0.13s total=2.00s (min 1.99 / max 2.13 / std 0.08 / cv 3.8%) rtf=0.40
  - text: ''
- **qwen3-int8-zengshuishui-1.7B/medium** (10.50s): status=PASS gen_tokens=33 n_audio=390
  - encode=0.66s prefill=1.24s decode=1.45s total=3.34s (min 3.27 / max 3.42 / std 0.07 / cv 2.2%) rtf=0.32
  - text: '先帝创业未半而中道崩殂，今天下三分，益州疲弊，此诚危急存亡之秋也。'
- **qwen3-int8-zengshuishui-1.7B/long** (25.00s): status=PASS gen_tokens=79 n_audio=390
  - encode=0.69s prefill=1.28s decode=3.39s total=5.34s (min 5.32 / max 5.39 / std 0.04 / cv 0.7%) rtf=0.21
  - text: '在近代中国，既有中西文化交流，又有新陈制度代谢。文化变迁与制度新格局的关系就很有趣了。制度上的现代化与文化上的西化，当时往往混在一起。而且，后者有时还被看成是最为深刻的变化。过去有种流行的说法，说是近代中西交往是个三阶段的过程。洋务运动时代'
- **qwen3-fp16-baicai1145-0.6B/short** (5.00s): status=PASS gen_tokens=17 n_audio=65
  - encode=0.17s prefill=0.26s decode=0.84s total=1.27s (min 1.27 / max 1.27 / std 0.00 / cv 0.1%) rtf=0.25
  - text: '先帝创业未半而中道崩殂，今天下。'
- **qwen3-fp16-baicai1145-0.6B/medium** (10.50s): status=PASS gen_tokens=33 n_audio=137
  - encode=0.33s prefill=0.49s decode=1.71s total=2.53s (min 2.52 / max 2.54 / std 0.01 / cv 0.3%) rtf=0.24
  - text: '先帝创业未半而中道崩殂，今天下三分，益州疲弊，此诚危急存亡之秋也。'
- **qwen3-fp16-baicai1145-0.6B/long** (25.00s): status=PASS gen_tokens=77 n_audio=325
  - encode=0.82s prefill=1.17s decode=4.54s total=6.50s (min 6.50 / max 6.68 / std 0.11 / cv 1.6%) rtf=0.26
  - text: '在近代中国，既有中西文化交流，又有新陈制度代谢。文化变迁与制度新格局的关系就很有趣了。制度上的现代化与文化上的西化，当时往往混在一起，而且后者有时还被看成是最为深刻的变化。过去有种流行的说法，说是近代中西交往是个三阶段的过程：洋务运动时代学'
- **qwen3-fp16-baicai1145-1.7B/short** (5.00s): status=PASS gen_tokens=17 n_audio=65
  - encode=0.21s prefill=0.67s decode=2.03s total=2.91s (min 2.89 / max 2.93 / std 0.02 / cv 0.6%) rtf=0.58
  - text: '先帝创业未半而中道崩殂，今天下。'
- **qwen3-fp16-baicai1145-1.7B/medium** (10.50s): status=PASS gen_tokens=33 n_audio=137
  - encode=0.46s prefill=1.38s decode=4.02s total=5.87s (min 5.79 / max 5.90 / std 0.04 / cv 0.7%) rtf=0.56
  - text: '先帝创业未半而中道崩殂，今天下三分，益州疲弊，此诚危急存亡之秋也。'
- **qwen3-fp16-baicai1145-1.7B/long** (25.00s): status=PASS gen_tokens=77 n_audio=325
  - encode=1.12s prefill=3.26s decode=10.13s total=14.52s (min 14.41 / max 14.76 / std 0.11 / cv 0.8%) rtf=0.58
  - text: '在近代中国，既有中西文化交流，又有新陈制度代谢。文化变迁与制度性格的关系就很有趣了。制度上的现代化与文化上的西化，当时往往混在一起，而且后者有时还被看成是最为深刻的变化。过去有种流行的说法，说是近代中西交往是个三阶段的过程：洋务运动时代学习'
