# 热键回调死锁修复

## 现象

macOS 上使用时，偶发"说到一半卡住"：松开热键后浮窗不消失、没有文字输出、整个 App 无响应。必须 `kill -9` 才能结束。用户已碰到多次。

## 诊断（2026-04-17 抓到一次活的）

`sample` 采 1696 次（2 秒，全满）+ 847 次（1 秒）两轮，进程所有线程 100% 停在同一套调用栈上，状态完全稳定 = 死锁：

1. **主事件线程（pynput CGEventTap 回调）**
   ```
   m_CGEventTapCallBack
     → pynput 的 on_release 回调
     → WhisperInput.on_key_release
     → AudioRecorder.stop
     → sounddevice/portaudio 的 FinishStoppingStream
     → AudioOutputUnitStop
     → HALC_ShellDevice::StopIOProc
     → HALB_Mutex::Lock → __psynch_mutexwait   ← 死等
   ```
2. **CoreAudio 内部 IO 队列线程**（`HALC_ProxyIOContext IO queue`）和 **CoreAudio 客户端 IO 线程**（`com.apple.audio.IOThread.client`）同时卡在各自的 pthread mutex / recursive_mutex 上
3. **ONNX 线程池 3 个 worker 全部在 `cond_wait`**，根本没被唤起 —— 模型推理**没被触发**，卡死点在"录音停止"这一步，还没走到 transcribe

所以用户感觉的"说到一半卡住"，实际是**松手瞬间卡住**，只是从用户视角看起来像是在"识别中途"。

## 根因

在 macOS 的 `CGEventTap` 回调线程里同步调 CoreAudio 的 `AudioUnitStop`，会跟 CoreAudio 自己的 HAL IO 线程抢同一把 HAL mutex —— 这是苹果在多个 issue tracker 里都有记录的已知反模式，只要时序碰巧就会 ABA 死锁。pynput 官方 README 也明确警告：回调里不应该做耗时 / 阻塞操作，否则 event tap 会被内核判定无响应而禁用。

当前代码里 `HotkeyListener._on_key_release` 直接在 pynput 回调线程上调 `WhisperInput.on_key_release`，后者又同步调 `recorder.stop()`（= `sd.InputStream.stop()`）。路径一旦踩中时序，就 100% 卡死。

Linux 下 evdev 是自己的 select loop、不涉及 CoreAudio，所以没这个问题 —— **本轮只需修 macOS 路径，但修法应该保持跨平台对称**（把"回调里做阻塞 IO"这类坏味道一次性消掉）。

## 目标

- 消除 macOS 下这个 CoreAudio 死锁（不能再出现"必须 kill -9"的状态）
- 修法要保证：hotkey 回调线程里再也不执行任何 **PortAudio / sounddevice / CoreAudio** 调用，也不应做任何可能被系统阻塞的 IO
- 不回归现有功能：按/松热键体验、浮窗状态切换、组合键延迟判定、托盘状态、tray / settings server 等都应保持原行为
- 不改 STT 本身，不改 Linux 的 evdev 路径结构（逻辑上可以对称受益，但不引入新问题）
