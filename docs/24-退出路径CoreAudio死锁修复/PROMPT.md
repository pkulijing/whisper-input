# 退出路径 CoreAudio 死锁修复（22 轮的另一半）

## 现象

22 轮修掉了"按热键说话时卡死"，用户连按几十次验收通过。但 **3 天后又碰到一次僵尸进程**：App 完全无响应、按热键没反应、托盘菜单点不动、必须 `kill -9`。

## 诊断（2026-04-20 10:29 抓活的 sample）

僵尸进程 pid 99719，版本 **0.7.1（已含 22 轮修复）**，启动时间 2026-04-17 20:38，卡死时间 ~10:23（sample 抓到已经 6 分钟）。

### 日志对齐

```
02:23:10  recording_start     ← 用户按住热键
02:23:11  recording_stop       ← 1 秒后松开(录得太短,wav_data 为空早退)
... 7 秒间隔 ...
02:23:18  shutting_down        ← 不知道谁触发的 shutdown()
... 往后再无日志,进程卡死 ...
```

用户自述"就按了一下就松开"，没点托盘退出、没 Ctrl+C。02:23:18 的 shutdown 来源未知（候选：macOS 系统级 SIGTERM / 误触托盘 / 某个 daemon 进程信号）。**但来源不重要 —— 重要的是 shutdown 路径本身死锁了**。

### sample 栈（主线程）

```
main
  → Py_FinalizeEx                       ← Python 解释器在做退出清理
    → atexit_callfuncs
      → cdata_call (cffi)
        → Pa_Terminate                  ← sounddevice 模块 import 时注册的 atexit
          → FinishStoppingStream
            → AudioOutputUnitStop       ← 跟 22 轮完全一样的撞锁点
              → HALB_Mutex::Lock
                → __psynch_mutexwait    ← 死等
```

同时 `com.apple.audio.IOThread.client` 线程卡在 `AudioUnitGetProperty` 的 recursive_mutex 上 —— 22 轮观察到的三向 HAL 锁相持又出现了一次，只不过这次的触发点从 pynput CGEventTap 换成了 Python atexit handler。

### 关键观察

- **22 轮加的 worker 线程已经 join 完毕**，pynput Listener 也 stop 了（sample 里 11 个线程没一个是它们）—— 说明 `shutdown()` 函数体正常跑完了，是 `sys.exit(0)` 之后 Python finalize 阶段才出的事
- **22 轮的修复本身没回归**：录音/识别链路没参与这次死锁

## 根因

22 轮只治了"按热键"这一个入口上的 `AudioUnitStop`，但 `sounddevice` 模块 import 时做了：

```python
# sounddevice.py
_initialize()  # Pa_Initialize
_atexit.register(_exit_handler)

def _exit_handler():
    while _initialized:
        _terminate()  # Pa_Terminate → AudioUnitStop
```

这个 atexit 在 Python `Py_FinalizeEx` 阶段跑在**解释器主线程**上，此时：
- GIL 语义特殊（准备销毁，其他线程被逐步 kill）
- Cocoa runloop 已经退出
- 但 CoreAudio IO 线程还活着，且处于某种 in-flight 状态

在这个"半死不活"的上下文里调 `AudioUnitStop`，跟 CGEventTap 回调线程里调一样概率性撞 HAL 锁。只不过这次的触发是"退出时"，频率低于"每次按热键"，所以 22 轮验收期没复现。

## 目标

- **退出流程在任何情况下都不能死锁**。用户触发 quit（不管通过 tray / SIGTERM / SIGINT）必须在合理时间内真的退掉，不能留下僵尸
- **不回归 22 轮**：按热键的 hot path 继续走 worker，不动
- **解决方案不能依赖猜准 shutdown 的触发源**。来源不重要，修法要对所有来源都有效
- **就算 PortAudio 本身还是会死锁，我们的 shutdown 也能超时兜底**（`os._exit(0)` 级别的防御）

## 非目标

- 不排查"02:23:18 是谁触发的 shutdown"这件事 —— 改不动它（可能是系统信号），也不该改
- 不试图让 PortAudio 的 atexit "变可靠" —— 那是上游库的事，我们控制自己这一层
- 不碰 BACKLOG 里"启动时清理僵尸实例"那条 —— 那是兜底机制（防万一真的留下僵尸），本轮是正面修"不产生僵尸"，两者正交，本轮不做
