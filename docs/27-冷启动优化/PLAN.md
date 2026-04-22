# PLAN：冷启动优化（local_files_only fast path）

## 背景

第 26 轮切到 Qwen3-ASR 后，cache 命中冷启动实测 **4.3–5.2s**
（`logs/whisper-input.log` 里 `model_preload_start → ready` 段）。
三段大头：

| 阶段 | 实测耗时 | 本轮 |
|---|---|---|
| `modelscope.snapshot_download` manifest 校验 | 1.5–2.4s | **优化** |
| 3 个 ONNX session 串行 init | ~1.5s | 原 plan 想并行，实测收益低被回滚 |
| `_warmup()` 0.5s 静音整路 encode+decode | ~1.1s | 不动 |

目标：压到 **~2.5-3s**。warmup 不动，其省下来的时间会在首次热键摊回来。

### Plan 阶段 spike 实测

- `snapshot_download(local_files_only=True)` cache 命中实测 **0.002s**
  （vs 原路径 1.5–2.4s），ROI 巨大
- cache 缺失时抛 **`ValueError`**（消息含 "outgoing traffic has been
  disabled"），**不是** PROMPT 最初假设的 `FileNotFoundError`——
  plan 据此修正为宽范围 `except Exception`
- 损坏 ONNX 抛 `onnxruntime.capi.onnxruntime_pybind11_state.
  InvalidProtobuf`（不易直接 import，按 `Exception` 兜底捕获）
- 当前环境：`onnxruntime==1.24.4`、`modelscope==1.35.4`

### 实现过程中回滚的方向：ONNX session 并行

原 plan 包含"`ThreadPoolExecutor(max_workers=3)` 并行 3 个 session"，
估收益 30-50%。代码写完实测只省 ~7%（1500ms → 1400ms），与估算差一个
数量级，决定**回滚**。原因是 ORT `InferenceSession.__init__` 内部
protobuf 图反射大量跨 C++/Python 边界 + `CPUExecutionProvider`
allocator 进程级 mutex，两块夹击让三条线程串行等。真正能砍这一段的
路子是 `SessionOptions.optimized_model_filepath` 持久化（留后续轮次）。

## 改动总览

| 文件 | 改动 |
|---|---|
| `src/whisper_input/stt/qwen3/_downloader.py` | 加 local-only fast path + force_network 参数 |
| `src/whisper_input/stt/qwen3/qwen3_asr.py` | `load()` 包损坏文件兜底：runner 构造失败 → 强制重下 → 重试 |
| `tests/test_qwen3_downloader.py` | 加 local-only 命中 / miss fallback / force_network 四条 |
| `tests/test_qwen3_asr.py` | 加 mock 用例验证损坏文件兜底链路 |

预估 ~90 行（含 docstring + 测试）。

---

## 改动 1：downloader 加 local-only fast path

`download_qwen3_asr` 加 `force_network` 关键字参数：

```python
def download_qwen3_asr(variant: str, *, force_network: bool = False) -> Path:
    # variant 校验...

    from modelscope import snapshot_download
    allow_patterns = [
        f"model_{variant}/conv_frontend.onnx",
        f"model_{variant}/encoder.int8.onnx",
        f"model_{variant}/decoder.int8.onnx",
        "tokenizer/*",
    ]

    # Fast path: cache 命中省 1.5–2.4s
    if not force_network:
        try:
            root = snapshot_download(
                REPO_ID,
                allow_patterns=allow_patterns,
                local_files_only=True,
            )
            logger.info("qwen3_snapshot_local_only_hit", variant=variant)
            return Path(root)
        except Exception as exc:
            logger.info(
                "qwen3_snapshot_local_only_miss",
                variant=variant,
                reason=type(exc).__name__,
            )

    root = snapshot_download(REPO_ID, allow_patterns=allow_patterns)
    return Path(root)
```

设计要点：

- `force_network` 给损坏文件兜底用。默认 False，现有调用方零感知
- catch `Exception`（不是具体 `ValueError`），modelscope 文档提到
  `OSError` / `EnvironmentError` 等多种，未来可能变；`KeyboardInterrupt`
  不会被吞（不属于 `Exception`）
- 新增模块级 logger，命名跟 `qwen3_snapshot_start/done` 一致

## 改动 2：`Qwen3ASRSTT.load()` 包损坏文件兜底

```python
t0 = time.perf_counter()
logger.info("qwen3_runner_start")
try:
    self._runner = Qwen3ONNXRunner(root / f"model_{self.variant}")
except Exception as exc:
    logger.warning(
        "qwen3_runner_corrupt_fallback",
        variant=self.variant,
        reason=type(exc).__name__,
    )
    root = download_qwen3_asr(self.variant, force_network=True)
    self._runner = Qwen3ONNXRunner(root / f"model_{self.variant}")
logger.info(
    "qwen3_runner_ready",
    elapsed_ms=int((time.perf_counter() - t0) * 1000),
)
```

- downloader 没法判断"文件存在但损坏"，只有真正构造 session 才发现
  protobuf 坏；所以兜底必须放在 runner 构造之后
- 第二次构造失败不再 retry，异常直接向上抛，避免无限循环

---

## 测试

两处新增，全部 mock（不下模型、不构造真 session）：

- `test_qwen3_downloader.py`：local_only 命中 / `ValueError` fallback /
  `OSError` fallback / `force_network` 四条路径
- `test_qwen3_asr.py`：mock `download_qwen3_asr` + `Qwen3ONNXRunner`，
  让 runner 第一次 raise、第二次成功，断言 downloader 被调两次
  （`[False, True]`），runner 被构造两次；再加一条"第二次仍失败 →
  异常直接上抛"

性能本身不写进单测——pytest 不做基准，手动看日志。

## 验收

性能（手动）：

```bash
uv run whisper-input --no-tray   # 启动后立刻 Ctrl+C
grep -E "model_preload_start|qwen3_snapshot_done|qwen3_runner_ready|ready" \
  logs/whisper-input.log | tail -10
```

期望：
- `qwen3_snapshot_done.elapsed_ms` ≤ 100ms
- `qwen3_runner_ready.elapsed_ms` 仍然 ~1500ms（本轮不动这一段）
- `model_preload_start → ready` 间隔 ≤ 3s

功能（自动）：

```bash
uv run pytest tests/test_qwen3_downloader.py tests/test_qwen3_asr.py -v
uv run pytest --cov-report=term-missing
uv run ruff check .
```

端到端：`test_qwen3_asr.py` 里 `zh.wav` smoke test 跑通，识别结果不变。

## 局限性

1. 首次下载场景没省时间——local_only 只对 cache 命中有效
2. runner 1.5s 原样保留——并行方案被回滚，真正的优化路径是 ORT
   optimized_model 持久化（独立轮次）
3. warmup 仍然 1.1s——本轮不动
4. 损坏文件兜底依赖 modelscope 重下行为，其升级后要跟着调
