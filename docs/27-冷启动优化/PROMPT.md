# 需求：冷启动优化（local_files_only + ONNX session 并行）

## 背景

第 26 轮把 STT 切到 Qwen3-ASR 之后，cache 命中场景下的冷启动耗时被
日志精确测量出来了。`logs/whisper-input.log` 在 2026-04-22 两次采样
（0.6B variant，cache 已就位）的关键事件：

```
model_preload_start             0.000s
qwen3_snapshot_start            0.000s
qwen3_snapshot_done             1.531s — 2.372s   ← modelscope manifest 校验
qwen3_runner_ready              ~1.5s             ← 3 个 ONNX session 串行构造
qwen3_tokenizer_ready           ~0.115s
qwen3_warmup_done               ~1.1s             ← 0.5s 静音空跑一次
ready                          总计 4.3 — 5.2s
```

三个真延迟大头已经定位清楚：

| 阶段 | 耗时 | 本质 |
|---|---|---|
| `modelscope.snapshot_download` | 1.5–2.4s | 跨境 HTTP 往返 + cache 文件 size/hash 比对 |
| 3 个 ONNX session 串行 init | ~1.5s | mmap + protobuf 解析 + kernel 选择 |
| `_warmup()`(0.5s 静音整路 encode+decode) | ~1.1s | encoder 首次前向 lazy 编译 |

**目标**：cache 命中冷启动从 ~5s 压到 ~2s。本轮**只动前两条**——
warmup 那条不动，省下的时间会在首次热键上摊回来，用户对首次识别延迟
更敏感（按下热键说话时本地已经有反馈延迟感知，开机时则在等托盘可点）。

## 本轮目标

### 核心目标

1. **`snapshot_download(local_files_only=True)` 兜底路径**
   - 命中 cache 时跳过 manifest 校验，省 ~1.5–2.4s
   - **关键**：损坏文件兜底要把 `InferenceSession` 构造一起包进去——
     "local_only 拿到路径 + 三个 session 成功建出"才算通过；任何
     `FileNotFoundError` / ONNX `InvalidProtobuf` / `RuntimeException`
     都要降级到带网络的正常路径（让 modelscope 自愈：re-verify hash +
     重下损坏文件）
2. **3 个 ONNX session 并行构造**
   - `Qwen3ONNXRunner.__init__` 里 `conv_frontend` / `encoder.int8` /
     `decoder.int8` 三个 session 之间无依赖，用 `ThreadPoolExecutor
     (max_workers=3)` 并发建立，I/O 等待和 graph init CPU 期重叠
   - 预期省 ~0.5–0.8s

合计预期：**~5s → ~2s**。

### 非目标（本轮明确不做）

- **裁剪 `_warmup()`**：不动，理由如上
- **lazy load decoder / 拆 conv+encoder 先行加载**：跟 `--no-preload`
  语义重合，需要重新设计 preload 模型，scope 翻倍
- **持久化 variant→path 映射到 config.yaml**：local_files_only 已经
  能把 manifest 开销降到 ~10ms 级别，再缓存路径属于过度优化
- **改 warmup 策略 / 多 variant 并发预热**：与本轮无关
- **28 轮流式相关改动**：本轮不依赖 28 轮，28 轮也不依赖本轮——两者
  完全正交，本轮先做掉是因为 scope 小、ROI 立刻可见

## 硬约束

1. **不引入新 runtime 依赖**：只用 `concurrent.futures` (stdlib) +
   现有的 `modelscope` / `onnxruntime`
2. **降级路径必须真的跑得通**：损坏文件场景实际很难触发（modelscope
   tmp + 原子 rename 基本不会留半成品），但兜底必须有测试覆盖
3. **不改对外接口**：`download_qwen3_asr(variant) -> Path` 签名不变；
   `Qwen3ONNXRunner(model_dir, *, max_total_len, providers)` 签名不变。
   所有调用方（`Qwen3ASRSTT.load`、测试、未来 27 轮的流式状态机）感知
   不到内部已经走了 fast path
4. **测试覆盖率不退化**：第 26 轮把整体覆盖率拉到 ~61%，`stt/qwen3/`
   子包 100%，本轮收尾时这两个数字都不能掉
5. **失败时日志要清楚**：fast path 失败回退到网络路径要有明确日志事件
   （`qwen3_snapshot_local_only_miss` 之类），这样以后用户报"启动慢"
   能直接看出是哪条路径

## 设计大方向

**downloader 改造**（`stt/qwen3/_downloader.py`）：

```python
def download_qwen3_asr(variant: str) -> Path:
    # 1. 校验 variant
    # 2. try: snapshot_download(local_files_only=True) → 直接返回
    # 3. except FileNotFoundError: 走正常 snapshot_download
```

但**仅靠 downloader 兜底不够**——它只能保证"文件存在"，无法保证
"文件能被 ONNX 解析"。损坏文件兜底必须放到调用层（`Qwen3ASRSTT.load`
或者 `Qwen3ONNXRunner.__init__` 外面包一层），具体哪一层在 PLAN 里
定。

**runner 改造**（`stt/qwen3/_onnx_runner.py`）：

```python
class Qwen3ONNXRunner:
    def __init__(self, model_dir, *, max_total_len=..., providers=None):
        # ...校验 + SessionOptions 准备...
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                "conv":    pool.submit(_make_session, "conv_frontend.onnx"),
                "encoder": pool.submit(_make_session, "encoder.int8.onnx"),
                "decoder": pool.submit(_make_session, "decoder.int8.onnx"),
            }
            self.conv    = futures["conv"].result()
            self.encoder = futures["encoder"].result()
            self.decoder = futures["decoder"].result()
        # ...inspect_decoder + 输出名 cache...
```

需要确认 `SessionOptions` 在三个 session 间共享是否安全（`onnxruntime`
官方文档：单个 SessionOptions 可被多个 InferenceSession 复用，构造后
不能再修改；三个 session 各自独立运行）。

## 验收标准

### 性能

- [ ] cache 命中冷启动从日志测得 **≤ 2.5s**（`model_preload_start` →
      `ready`），相比当前 4.3–5.2s 至少省 50%
- [ ] `qwen3_snapshot_done.elapsed_ms` 在 cache 命中时 **≤ 100ms**
- [ ] `qwen3_runner_ready.elapsed_ms` 相比串行版至少减少 30%
- [ ] cache 缺失场景行为不变（仍然走完整 modelscope 下载，不引入额外
      延迟）

### 功能正确性

- [ ] cache 命中：fast path 直接返回，不发起任何网络请求（用 mock 或
      日志事件验证）
- [ ] cache 缺失（`FileNotFoundError`）：自动降级到带网络的
      `snapshot_download`，下载完成后正常构造 runner
- [ ] cache 损坏（某个 .onnx 存在但 protobuf 解析失败）：自动降级到
      带网络的 `snapshot_download` 触发 modelscope 重下，最终能成功
      load
- [ ] 已有的端到端 smoke test（`test_qwen3_asr.py` 跑 `zh.wav`）
      仍然通过

### 代码质量

- [ ] 新增日志事件：`qwen3_snapshot_local_only_hit` /
      `qwen3_snapshot_local_only_miss` /
      `qwen3_runner_corrupt_fallback` 三类，方便事后排错
- [ ] 单元测试新增：local_only 命中、`FileNotFoundError` fallback、
      损坏 ONNX fallback 三条路径，全部 mock，**不实际下载、不实际
      构造 ONNX session**
- [ ] 整体覆盖率不低于 61%，`stt/qwen3/` 子包仍 100%
- [ ] ruff 通过

## 局限性 / 不解决

1. **首次下载场景没省时间**：local_files_only 只对 cache 命中场景有效，
   首次下载该花的网络时间一秒不少
2. **warmup 仍然 1.1s**：见上文，本轮明确不动
3. **session 并行只省 30–50%**：CPU 仍然是瓶颈（每个 session 的 graph
   init CPU 阶段没法并行加速），节省的是"I/O + CPU 重叠"那部分
4. **损坏文件兜底依赖 modelscope 行为**：一旦 modelscope 升级后
   `local_files_only` 行为变化（比如不再抛 `FileNotFoundError`），
   兜底逻辑要跟着调
