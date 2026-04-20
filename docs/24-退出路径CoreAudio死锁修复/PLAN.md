# 实现计划

## 核心思路

**抢在 Python finalize 之前，在我们控制的上下文里把 PortAudio 干净地关掉**；同时**给 shutdown 加硬超时兜底**，哪怕 PortAudio 仍然死锁也不让进程变僵尸。

两层防御：

1. **主动层**：`shutdown()` 里在 stop worker 之后显式调 `sounddevice._terminate()`，并 `atexit.unregister(sd._exit_handler)`。成功的话，Py_FinalizeEx 阶段的 atexit 就没活可干了
2. **兜底层**：如果 `_terminate()` 自己都死锁（理论上可能，上下文虽然更健康但不保证），用单独的 daemon 线程调 + 主线程 `join(timeout)`。超时就 `os._exit(0)` 强制退出，跳过所有 atexit

## 改动范围

### 1. `src/whisper_input/__main__.py`

**新增一个函数 `terminate_portaudio(timeout: float = 2.0) -> bool`**：

- 动态 `import sounddevice as sd`（避免在 Linux / 无 sounddevice 环境下破坏 `--help` 等路径）
- `atexit.unregister(sd._exit_handler)` —— 不管 `_terminate()` 成功与否，都不让默认 atexit 再跑一次
- 起一个 daemon 线程调 `sd._terminate()`，主线程 `join(timeout)`
- 成功返回 True，超时返回 False，异常也返回 False 但 log 下来

**改 `shutdown()`**：

```python
def shutdown():
    if _shutting_down: return
    _shutting_down = True
    logger.info("shutting_down", ...)
    listener.stop()       # 先断输入源,不再有新事件进来
    wi.stop_worker()      # 清空 worker 队列
    # ↓ 新增:在我们控制的上下文里终结 PortAudio
    ok = terminate_portaudio(timeout=2.0)
    if not ok:
        logger.warning("portaudio_terminate_timeout", ...)
    settings_server.stop()
    _shutdown_event.set()
```

顺序很关键：**必须在 worker stop 之后**（保证没有 start/stop stream 操作在飞），**必须在 settings_server stop 之前**（HTTP server 停不停不影响音频，但放后面让退出消息能响应完）

**改 main() 退出路径**：

- tray 分支：`tray_icon.run()` 返回后，不再是 `return`，改成根据 `terminate_portaudio` 的结果决定 `sys.exit(0)` 还是 `os._exit(0)`。超时了就用 os._exit 跳过剩下的 atexit
- 非 tray 分支（Linux / --no-tray）：同上
- 更简单的做法：直接用一个模块级 `_force_exit` 标志，`terminate_portaudio` 超时时置位，main 结尾看这个标志决定走哪条

为了不把两个分支的退出逻辑写重复，把它抽成一个小函数 `_final_exit()`：

```python
def _final_exit(force: bool) -> None:
    if force:
        logger.warning("force_exit", ...)
        os._exit(0)
    sys.exit(0)
```

`shutdown()` 把 `terminate_portaudio` 的返回值记到一个 nonlocal / closure 变量里，main 结尾读这个变量。

### 2. 不改的文件

- `recorder.py`：录音层不用动，`terminate_portaudio` 是进程级清理，比录音层 stream.stop 更底下一层
- `hotkey_*`、`input_*`、`overlay_*`、`stt/*`：完全无关
- `tray.py`：tray quit 回调照旧调 `shutdown()`，`shutdown()` 自己变稳了 tray 就自动跟着变稳

## 关键设计决策

- **用 `sd._terminate()` 而不是自己造 `ctypes.CDLL(...).Pa_Terminate()`**：sounddevice 管理着 `_initialized` 计数器，绕开它会让库进入不一致状态
- **`atexit.unregister` 放在调 `_terminate()` 之前**：这样哪怕我们自己调的 `_terminate()` 死锁了、join 超时跳过，剩下的 atexit 也不会重新撞一次
- **超时 2 秒**：22 轮的 `stop_worker` 也用 2 秒。经验值，够正常 Pa_Terminate 跑完（<100ms）、又不会让用户等烦
- **超时就 `os._exit(0)`**：听起来很暴力，但此时我们自己的资源（settings_server 可能还没来得及 stop、config 可能没写盘）已经尽力清理过了，剩下的就是 CoreAudio 那边被挂住的状态，进程死掉系统会自动回收。用 os._exit 就是为了**不留下僵尸进程让用户困惑**
- **`sd._terminate()` 用的是私有 API**：上游 API 不稳定风险。但 sounddevice 这几年几乎没动过 `_initialize`/`_terminate` 的形状，可接受；写个 test 在 CI 上发现上游改名即可
- **Linux 上不需要但也跑**：Linux 的 PortAudio 用 ALSA / PulseAudio，不存在 CoreAudio HAL 锁问题，但跑一次 `_terminate()` 是无害的，省得两边代码分叉

## 测试策略

### 自动化

- `tests/test_main_shutdown.py`（新增），用例：
  1. `terminate_portaudio` 正常路径：mock `sounddevice` 模块 + fake `_terminate` 立即返回 True，验证 `atexit.unregister` 被调了、返回 True
  2. `terminate_portaudio` 死锁兜底：mock `_terminate` 为 `time.sleep(10)`，设 timeout=0.2，验证返回 False、主线程没被 block 住、没抛异常
  3. `terminate_portaudio` 异常路径：mock `_terminate` 抛 RuntimeError，验证返回 False + `logger.exception` 被触发
  4. sounddevice 未安装时（`ImportError`）：优雅返回 True（无事可做）

- 不新增 end-to-end shutdown 测试：真的起一遍 App 再 shutdown 涉及 tray 主循环、pyobjc，测试成本 / 收益不划算

### 手动（macOS 必做）

1. 正常退出：托盘菜单 → 退出。应该 <1s 进程消失
2. Ctrl+C 退出：跟上面一样，进程干净结束
3. SIGTERM：`kill <pid>`（不加 -9），观察进程在 2s 内消失
4. 人为制造 Pa_Terminate 死锁场景（难）：skip，兜底路径只能靠 unit test 验证
5. 再复刻 22 轮的"连按 20 次"，确认按热键 hot path 没被本轮引入回归

### 验收门槛

- `uv run ruff check .` 通过
- `uv run pytest` 全绿（含新增）
- 手动三种 quit 场景都能在 2s 内清洁退出

## 风险

- **`sd._terminate()` 是私有 API**：上游一旦改名我们就炸。缓解：用 `getattr(sd, "_terminate", None)` + fallback 到"不做主动 terminate 只 unregister atexit"（相对保守）。决定：**第一版直接用私有 API，带 AttributeError catch 记日志，不再 fallback** —— 等真出问题再想
- **`os._exit(0)` 跳过 finally 块**：我们依赖 finally 做的事（config 写盘？日志 flush？）可能丢。审查下：`ConfigManager` 的写是 save 时主动调的，不靠 finally；日志是 stdlib `logging` + structlog，没缓冲 atexit 清理依赖。可接受
- **Linux 回归**：Linux 的 sd._terminate 能正常返回，测试 fake 能覆盖；真实 Linux 本来就没死锁问题，加这一步只会更干净
- **竞态：shutdown 被并发调用**：现有的 `_shutting_down` 标志挡住了第二次进入；新代码不破坏这个保护

## BACKLOG 联动

BACKLOG 里"启动时清理僵尸实例"这条**保留不删**。本轮是治"不产生僵尸"，那条是"万一产生了怎么办"，两者相互独立，都该做。本轮修完后那条优先级可以降低（因为僵尸产生概率大幅下降），但不是零，留着兜底。
