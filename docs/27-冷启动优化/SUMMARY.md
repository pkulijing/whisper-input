# SUMMARY：冷启动优化（local_files_only fast path）

## 背景

26 轮把 STT 切到 Qwen3-ASR 之后，cache 命中冷启动被
`logs/whisper-input.log` 精确测到 **4.3–5.2s**（`model_preload_start →
ready` 段）。拆解：

| 阶段 | 改前 | 本质 |
|---|---|---|
| `modelscope.snapshot_download` manifest 校验 | 1.5–2.4s | 跨境 HTTP 往返 + cache 文件 size/hash 比对 |
| 3 个 ONNX session 串行 init | ~1.5s | mmap + protobuf 解析 + kernel 选择 |
| `_warmup()` 0.5s 静音整路 encode+decode | ~1.1s | encoder 首次前向 lazy 编译 |

用户视角是"双击图标 → 等 4-5 秒图标才可点"——不是致命但扎眼，够痛
才做。warmup 那一项不动，因为它省下的时间会在首次热键上摊回来，对延迟
更敏感。

## 实现方案

### 关键设计

1. **`snapshot_download(local_files_only=True)` fast path**。
   Plan 阶段 spike 实测 cache 命中这条路径只要 2ms（vs 原路径
   1.5-2.4s）。失败路径的异常类型实测是 **`ValueError`**（消息含
   "outgoing traffic has been disabled"），和 PROMPT 最初假设的
   `FileNotFoundError` 不一样——最终 plan 用 `except Exception` 宽捕
   获，兼顾 modelscope 升级和其它可能的 I/O 异常。
2. **损坏文件兜底放在 `Qwen3ASRSTT.load()` 层**，不是 downloader 层——
   因为 downloader 没法判断"文件存在但损坏"，只有真正构造
   `InferenceSession` 才会抛 `InvalidProtobuf`。兜底链路：local-only 拿
   路径 → runner 构造抛异常 → 用 `force_network=True` 重新调 downloader
   → 再构造一次。第二次失败直接放任异常上抛，避免无限重试卡死冷启动。

### 实现过程中回滚的方向：ONNX session 并行构造

Plan 里原本还包含"`ThreadPoolExecutor(max_workers=3)` 并行构造三个
session"一条，估收益 30-50%。**代码写完 + 实测后发现实际只省
~7%（1500ms → 1400ms），与估算差一个数量级，决定回滚**。

原因分析（事后推测）：ORT 的 `InferenceSession.__init__` 内部有两块
GIL 释放不彻底的阶段：

- protobuf 图反射里大量跨 C++/Python 边界的调用，每次都重新
  acquire GIL
- `CPUExecutionProvider` 的 allocator 是进程级单例，多 session 同时
  init 时在内部 mutex 上排队

这两块叠加让 3 条线程大段时间在串行等。真正能把这一段砍下来的路子是
**ORT `SessionOptions.optimized_model_filepath` 持久化优化后的图**——
第一次编译落盘，后续启动跳过所有 graph optimization pass，社区报告
能省 30-60%。但这条改动面更大（新增 `~/.cache/whisper-input/ort_cache/`
目录 + 多一份 optimized .onnx 落盘 + ORT 版本变更时的兜底），不适合
塞进本轮。留作独立后续轮次。

### 开发内容概括

- [src/whisper_input/stt/qwen3/_downloader.py](../../src/whisper_input/stt/qwen3/_downloader.py)：
  - `download_qwen3_asr` 加 `force_network: bool = False` 关键字参数
  - fast path：`local_files_only=True` 先尝试，失败日志
    `qwen3_snapshot_local_only_miss` 后降级到完整网络路径
  - 新增模块级 `logger`
- [src/whisper_input/stt/qwen3/qwen3_asr.py](../../src/whisper_input/stt/qwen3/qwen3_asr.py)：
  - `load()` 的 runner 构造包 `try/except Exception`，失败时日志
    `qwen3_runner_corrupt_fallback` + 调 downloader（force_network=True）
    重下 + 重构造一次
- 2 个测试文件补全 5 条新用例：
  - `test_qwen3_downloader.py`：local_only 命中、`ValueError` fallback、
    `OSError` fallback、`force_network` 跳过 fast path
  - `test_qwen3_asr.py`：第一次构造失败 → 自动 force_network 重下 → 第二
    次成功；第二次仍失败直接 raise

总改动 ~90 行（含 docstring + 5 条测试）。

### 额外产物

无——本轮不需要 spike 脚本、额外 fixture 或 migration 工具。spike
实测已在 plan 阶段随手完成；并行方案的否决实验也没留脚本，回滚后代码
就是最终形态。

### 实测验收

cache 已就位，4 次连跑 `Qwen3ASRSTT().load()`：

| 阶段 | 改前 | 改后 | 结论 |
|---|---|---|---|
| `qwen3_snapshot_done.elapsed_ms` | 1531–2372ms | **~44ms** | ~50× 快 ✓ |
| `qwen3_runner_ready.elapsed_ms` | ~1500ms | ~1500ms | 未动 |
| 总 `load()` | 4.3–5.2s | **~2.8s** | ~45% 省时 ✓ |

- 244 项原测试 + 5 项新测试 = 245 全通过
- 整体覆盖率 61% → **62%**（轻微改善）
- `stt/qwen3/` 子包仍然 **100%**
- `zh.wav` 端到端 smoke test 识别结果未变

## 局限性

1. **首次下载场景未省任何时间**：local_only 只对 cache 命中有效，首次
   下载仍走完整 modelscope 流程
2. **runner 1.5s 原样保留**：并行方案因为 ORT 内部 GIL 释放不彻底被
   回滚；进一步压这一段需要走 `optimized_model_filepath` 持久化，留
   独立轮次
3. **warmup 1.1s 原样保留**：按 PROMPT 决策——省下来会转嫁到首次热键
   延迟，用户对后者更敏感
4. **损坏文件兜底是"信 modelscope"**：我们不主动 hash 校验 ONNX 文件，
   只在真正构造 `InferenceSession` 失败后重下。如果未来 modelscope 的
   "已有同名文件不重下"策略改变，兜底逻辑要跟着调

## 后续 TODO

- **`SessionOptions.optimized_model_filepath` 持久化**：让 ORT 把 graph
  optimization 的产物落盘到 `~/.cache/whisper-input/ort_cache/`，
  二次启动跳过所有优化 pass。社区报告省 30-60% session init 时间。
  改动面：~60 行 + 一份 cache 失效兜底（ORT 升级后旧 opt 文件解析失败
  → 删掉重跑）
- **持久化 variant→model_dir 到 config.yaml**：彻底跳过 modelscope
  API 调用，启动再省几十 ms。现在 44ms 已经不扎眼，优先级低
- **warmup 异步化 + 首次热键前置等待**：让 `ready` 事件先触发，warmup
  在后台补齐。复杂度翻倍且 "用户松手时 warmup 没做完" 的 UX 边角要
  想清楚，留给未来单独一轮

已同步到 [BACKLOG.md](../../BACKLOG.md)（本轮主目标条目已删除；新增
"ORT optimized_model 持久化"作为后续条目）。
