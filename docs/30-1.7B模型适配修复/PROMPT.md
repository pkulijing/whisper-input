# 1.7B 模型适配修复

## 现象

1. 在设置页把 STT 模型从 `0.6B` 切到 `1.7B` 后**完全不可用**：按住热键说话、松手后没有文字粘贴出来；多次尝试，"输入不是每次都能成功"。
2. 单测层面 `pytest` 跑下来有 38 个 qwen3 相关 case 被 skip（`tests/test_qwen3_*.py` 全套），实际跑过的只有跟模型无关的部分。
3. 0.6B 在产品里能正常工作，单测虽然也都 skip 但产品路径已被人肉验证过。

## 根因（已查明）

### A. conftest 没认 `MODELSCOPE_CACHE` 环境变量

`tests/conftest.py:140` 的 `_candidate_qwen3_roots()` 只查这几个候选：

- `DAOBIDAO_QWEN3_DIR`
- `~/.cache/modelscope/hub/models/zengshuishui/Qwen3-ASR-onnx`
- `~/.cache/modelscope/hub/zengshuishui/Qwen3-ASR-onnx`
- `/tmp/qwen3-asr-spike`

而开发机把 ModelScope cache 重定向到了 `MODELSCOPE_CACHE=/mnt/1C46F5D146F5ABA0/modelscope`，实际的 Qwen3-ASR cache 在 `/mnt/1C46F5D146F5ABA0/modelscope/models/zengshuishui/Qwen3-ASR-onnx`，因此 `_find_qwen3_root()` 永远返回 `None`，所有依赖 `qwen3_cache_root` fixture 的 case 全部 skip。

产品代码不存在这个问题——因为产品里走 `modelscope.snapshot_download`，库本身会读 `MODELSCOPE_CACHE`。conftest 是单测自己手写的候选路径表，没跟着读，所以单测看不到 cache。

附带的 fixture 设计缺口：`tests/conftest.py` 只暴露了 `qwen3_0_6b_model_dir`，**没有 `qwen3_1_7b_model_dir`**。即便 conftest 修好认到 cache，1.7B 路径也仍然零单测覆盖。

### B. 流式初始化把 `audio_features` 维度写死成 1024

`src/daobidao/stt/qwen3/_stream.py:171`：

```python
dummy_af = np.zeros((1, 1, 1024), dtype=np.float32)
runner.decoder_step(
    np.array([chat_prefix_ids], dtype=np.int64),
    dummy_af,
    caches,
    0,
)
```

这个 1024 是 0.6B 模型 encoder 的输出维度，被当作常量写在了"chat prefix prefill"步里。直接 inspect 两份 ONNX 拿到的真实 schema 是：

| 维度 | 0.6B | 1.7B |
| --- | --- | --- |
| conv_frontend 输入（mel bins） | 128 | 128 |
| conv_frontend 输出 / encoder 输入 | 896 | **1024** |
| encoder 输出 / decoder `audio_features` 输入 | **1024** | **2048** |
| decoder KV cache（layers / kv_heads / head_dim） | 28 / 8 / 128 | 28 / 8 / 128 |

1.7B 的 decoder `audio_features` 输入要求 last dim = 2048。`dummy_af` 喂 `(1, 1, 1024)` 进去，onnxruntime 直接报 shape mismatch。

**这就是 1.7B "完全不能用"的直接根因。**

`init_stream_state` 抛异常 → 上层流式 worker 也没把 state 标成不可用 → 后续 `stream_step` 操作半残 state → 用户感受到的就是"按了没反应""偶尔能出字偶尔不能"。

离线 `Qwen3ASRSTT.transcribe()` 路径不过这一行（直接拿真 `audio_features` 喂 decoder），所以离线模式 1.7B 不挂；当前默认是开流式，所以症状才看着像"1.7B 整个挂了"。

### C. 测试代码里也散落着写死的 1024

`tests/test_qwen3_runner.py` 多处：

```
L79:  assert audio_features.shape[2] == 1024
L122: audio_features = np.zeros((1, 100, 1024), dtype=np.float32)
L135: audio_features = np.random.RandomState(0).randn(1, 50, 1024)...
L154: audio_features = np.random.RandomState(1).randn(1, 50, 1024)...
L177: audio_features = np.zeros((1, 50, 1024), dtype=np.float32)
```

这些是为 0.6B 写的 fake 张量。如果将来要把 runner 测试 parametrize 到两个 variant，这些都得换成从 runner 自己反推的维度。

## 期望

- conftest 认 `MODELSCOPE_CACHE`，能在本机直接找到 cache，0.6B / 1.7B 的 qwen3 单测不再被 skip。
- `_stream.py` 不再硬编码 1024，dummy 张量从 runner 实际暴露的维度推导。
- 单测覆盖到 1.7B：至少有一条 end-to-end smoke 跑过 1.7B 全链路（包括流式 `init_stream_state` + `stream_step`），保证以后再有类似 variant-specific 的硬编码进来时能被卡住。
- 产品里切到 1.7B 后，按住热键说话、松手能正常出字，跟 0.6B 体验一致。

## 不做的事

- 不动模型本身（不重训、不重新 export ONNX）。
- 不改流式策略 E（rollback / marker-anchored split 等流式算法逻辑保持原样）。
- 不引入新的 STT 后端、不引入 GPU provider。
- 不调整 KV cache 大小（两份模型 KV 形状一致，`max_total_len=1200` 仍然适用）。
