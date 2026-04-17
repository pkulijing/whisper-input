# 实现计划

## 核心思路

**把热键回调里所有"实际动作"搬到一个独立的单线程 worker 上串行执行**，pynput 回调只负责往队列里 push 事件后立刻 return。

为什么选"单线程 + 队列"而不是"每次 spawn daemon thread"：
- 串行保证 press → release 顺序，不会出现 release 的 `stop` 先于 press 的 `start` 执行的竞态
- 跟现有 `_processing` 标志天然兼容，不用再加锁
- 一个常驻线程，生命周期跟 `WhisperInput` 绑死，shutdown 好处理

## 改动范围

只改两个文件：

### 1. `src/whisper_input/__main__.py` — `WhisperInput` 类

新增一个串行 worker：

- `__init__` 里新建 `self._event_queue: queue.Queue` 和 `self._worker_thread: threading.Thread(daemon=True)`，worker 循环从队列里取 callable 并执行，异常只记日志不挂掉线程
- 新增 `start_worker()` 和 `stop_worker()`（stop 通过 sentinel `None` 让 worker 自然退出，`join(timeout=2)`）
- **重命名**当前同步的 `on_key_press` / `on_key_release` 为 `_do_key_press` / `_do_key_release`（实际干活的），保持逻辑不动
- **新建对外暴露的** `on_key_press` / `on_key_release`：只做 `self._event_queue.put(self._do_key_press)` 等入队，**不做任何 log / overlay / sound 调用**（这些统统留给 worker 去做），然后立刻 return
- `main()` 里创建完 `WhisperInput` 后 `wi.start_worker()`；`shutdown()` 里加 `wi.stop_worker()`

### 2. `src/whisper_input/recorder.py` — 不动

`AudioRecorder` 的 start/stop 内部已经有 lock，本身是 thread-safe 的，不需要改。关键是**调用者**不在 CGEventTap 线程里调它。

## 非改动项

- `HotkeyListener` (hotkey_macos.py / hotkey_linux.py)：不改。pynput 回调照旧同步调 `on_press` / `on_release`，只不过那俩现在变成了"立刻返回的入队"
- Linux 路径（evdev）：同一个 `WhisperInput.on_key_*` 接口，天然对称获益 —— evdev 回调现在也不再阻塞读线程
- 浮窗 / tray / STT / settings server：全部不动
- 日志系统、i18n、配置：全部不动

## 关键设计决策

- **Worker 是 `daemon=True`**：主线程退出时兜底不阻塞；但我们还是通过 sentinel 主动退出以便留下日志
- **Worker 内异常用 `logger.exception`**：避免一次失败让 worker 线程死掉，否则后续按键全都无响应
- **队列无上限**：`Queue()` 不设 maxsize，避免 `put` 阻塞住 pynput 回调（哪怕一瞬间也绝对不能阻塞）
- **不做去重 / 合并**：press/release 都如实入队。现有的 `self._processing` 守卫在 `_do_key_press` 里继续生效，跟之前语义一致
- **`_notify_status` 和 `play_sound` 也留在 worker 里**：这些之前也在 pynput 回调线程跑，现在挪到 worker 只会更干净；不挪会让"开始录音提示音"晚于 worker 里的 `recorder.start()` 触发 → 可能丢音频开头。为了**保持和原来一样的时序**，`_do_key_press` / `_do_key_release` 函数体基本就是现在 `on_key_press` / `on_key_release` 的 1:1 搬运

## 测试策略

### 自动化

- 已有的 hotkey 状态机测试不受影响（HotkeyListener 没改）
- 已有的 recorder / stt / settings_server 等测试不受影响
- **新增** `tests/test_worker_dispatch.py`（或加到 `tests/test_main_worker.py`）：构造 `WhisperInput` 实例（mock 掉 recorder / stt / overlay / status_callback），验证：
  1. `on_key_press()` 调用会立刻返回（<10ms），不触发 `_do_key_press`
  2. worker 启动后，入队的 `_do_key_press` 会被执行
  3. worker 里抛异常不会让 worker 挂掉（入队第二个事件仍会被执行）
  4. `stop_worker()` 能让 worker 2s 内退出

### 手动（macOS 必做）

- 预加载模型后连按 20 次热键，每次语速正常说一句话 —— 过去大约每 3-5 次必现一次死锁，修后应 0 次
- `sample <pid>` 抓一次运行中状态，验证 CGEventTap 线程永远在 `poll`/mach_msg 等空闲栈里，而不是 `AudioUnitStop`
- 组合键不被误触（如按住 Ctrl_R 再按 C），保持原有逻辑
- 托盘/浮窗状态切换跟以前一致
- Ctrl+C 退出流畅，没有"等 worker 2s"的明显卡顿

### 验收门槛

- `uv run ruff check .` 通过
- `uv run pytest` 全绿（含新增的 worker 测试）
- 手动 20 次连按无死锁

## 风险

- **Worker 里抛异常但 `_processing` 被设成 True 没复位**：现有 `_process` 用 `try/finally` 保证 `_processing = False`，保留这条。`_do_key_press` 本身不碰 `_processing` 置位，置位在 `_do_key_release` 末尾（跟现状一致），所以风险面不变
- **时序微调**：原来 press 回调里 `play_sound(start_sound)` 跟 pynput 回调线程同步，现在变成经过 queue 再执行 —— 延迟大约 <1ms，人耳听不出来
- **shutdown 顺序**：worker 必须在 settings_server / listener stop 之后、或之前？都可以，因为 worker 不依赖这俩。保守放在 listener stop 后、settings_server stop 前，让最后可能在途的 release 事件有机会被处理掉（但也加超时防呆）

## BACKLOG 同步

修完后在 `BACKLOG.md` 里删掉对应条目（如果之前记过类似现象），并考虑加一条"后续可以把 pynput 回调里其他潜在阻塞（如浮窗 show）也一起审一遍"
