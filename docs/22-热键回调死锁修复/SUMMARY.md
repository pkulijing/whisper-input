# 开发总结 — 热键回调死锁修复

## 开发项背景

### BUG 表现

macOS 下使用 whisper-input 时偶发"说到一半卡住"：松开热键后浮窗不消失、没有文字粘出来、整个 App 无响应，必须 `kill -9` 才能结束。用户已碰到多次，严重影响可用性。

### 影响

- 每次卡死都丢失正在录的这段话
- 必须手动 kill 才能恢复，对"桌面常驻工具"体验毁灭性
- 偶发性高但不稳定复现，过去一直没排查清楚

## 实现方案

### 关键设计 / 发现

2026-04-17 碰到一次活的卡死状态，用 `sample <pid>` 抓了两轮调用栈（2s + 1s，共 2543 次采样），进程 100% 停在同一套 frames 上：

- **pynput 的 CGEventTap 回调线程**（松开热键那一下）同步调到 `AudioRecorder.stop()` → portaudio `FinishStoppingStream` → CoreAudio `AudioOutputUnitStop` → `HALB_Mutex::Lock` → `__psynch_mutexwait`，卡住
- **CoreAudio 自己的两个 IO 线程**（`HALC_ProxyIOContext IO queue` 和 `com.apple.audio.IOThread.client`）同时卡在各自的 pthread mutex 上 —— 经典的三向 HAL mutex 死锁
- **ONNX 3 个 worker 全部在 `cond_wait`**，没被唤醒 —— 模型推理根本没被触发，卡点在"录音停止"

根因是一个很典型的反模式：**在 macOS 的 CGEventTap 回调线程里同步调 `AudioUnitStop`**。pynput 的全局键盘监听本质是个 CFMachPort 事件源，跑在某个系统后台线程里；在这个线程上阻塞等 CoreAudio HAL mutex，会跟 CoreAudio 自己用这把锁的 IO 线程发生顺序反向依赖，概率性死锁。pynput README 也明确警告"回调里不要做耗时操作"。

### 开发内容概括

只改了一个文件 `src/whisper_input/__main__.py`：

- 给 `WhisperInput` 加了一条**单线程事件 worker**：`queue.Queue` + 一个 daemon 线程，worker 循环从队列里取 callable 串行执行，异常只 `logger.exception` 不让线程挂掉
- `on_key_press` / `on_key_release` 从"同步执行"改成"只把任务 put 到队列后立刻返回"，原本的函数体搬到 `_do_key_press` / `_do_key_release`（逻辑 1:1 不变）
- `start_worker()` / `stop_worker()` 负责 worker 生命周期，分别在 `main()` 里创建 `WhisperInput` 之后、`shutdown()` 里 listener 停完之后调用（顺序：stop listener → stop worker → stop settings_server，保证 in-flight release 事件有机会完成，但不会无界等待）
- **没改** `HotkeyListener`、`AudioRecorder`、STT、overlay、tray —— 整个改动是纯纯的"调用时机重排"，行为不变

选"单线程 + 队列"而不是"每次 spawn daemon thread"，是因为串行队列天然保证 press → release 顺序不会倒转，不用额外加锁；一个常驻 worker 的生命周期也比"一次性线程"好推理。

### 额外产物

- **`tests/test_main_worker.py`**，7 个用例覆盖新 worker 的核心不变量：
  1. `on_key_press` 调用 < 10ms 返回，不触碰 recorder
  2. worker 启动后入队任务会被执行
  3. press → release 入队顺序被 worker 严格保留（side_effect 里故意加 sleep 验证不是并行）
  4. worker 里第一次抛异常不会让线程挂掉，第二次事件仍会执行
  5. `stop_worker` 能在 2s 内让线程退出
  6. `start_worker` 幂等，重复调不创建多个线程
  7. 没 start 过也能 stop，不报错

- **`docs/22-热键回调死锁修复/`** 完整三件套（PROMPT / PLAN / SUMMARY）+ 本次抓到的 `sample` 调用栈诊断（写在 PROMPT.md 里，留档）

测试状态：`uv run pytest` 103 passed（原 96 + 新增 7），`uv run ruff check .` 全绿。

## 验收结果

用户手动连按几十次热键正常说话，不再出现卡死（过去大约 3-5 次必现一次），修复有效。

## 局限性

- **没从根子上治好 pynput 回调阻塞风险**。当前只把**录音相关**的阻塞挪走了，但如果以后有人在回调链路上加别的同步 IO（比如直接在 press 回调里做网络 / 磁盘操作），还是会重蹈覆辙。本轮只做针对性修复，没引入全局的"回调线程白名单"机制
- **偶发性 bug 的收敛判定不够严格**。"几十次没再出现"只能说明显著降低了触发概率，不能排除某些极端时序下仍会复现。用户在 args 里也说"如果后面碰到再解决吧" —— 先认这个结果，留观
- **ONNX / STT 本身的鲁棒性没动**。本次 bug 的副作用之一是让人误以为"模型推理有问题"，但实际上 STT 并没被触发；STT 自身是否有卡死风险本轮没排查

## 后续 TODO

- 本轮没新增全局 backlog 条目：修得很聚焦，没发现顺带的架构问题
- 如果未来要做 20 轮日志系统的增强，可以考虑在 event worker 里加一个 "事件处理耗时" 指标，这样下次再卡住能直接从日志看出是哪条 `_do_key_*` 慢了，不用再 `sample`
- 下次再遇到类似偶发卡死：第一反应就是 `sample <pid>` 抓调用栈，比看日志快得多 —— 这次从"拿到 pid"到"定位根因"只用了 5 分钟
