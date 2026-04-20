# 开发总结 — 退出路径 CoreAudio 死锁修复

## 开发项背景

### BUG 表现

22 轮修完、用户连按几十次验收通过的 3 天后再次出现卡死僵尸进程：App 完全无响应，按热键 / 点托盘都没反应，必须 `kill -9`。

### 影响

- 僵尸进程不下班（直到 reboot 或手动 kill）
- 用户无法判断 App 是否在运行（托盘图标还在，但功能全废）
- 破坏了 22 轮建立起来的"按热键路径稳定可靠"的信任感

## 实现方案

### 关键设计 / 发现

2026-04-20 抓到活的 sample，主线程栈：

```
main → Py_FinalizeEx → atexit_callfuncs → Pa_Terminate (sounddevice 注册的)
     → FinishStoppingStream → AudioOutputUnitStop → HAL mutex → 死等
```

同时日志最后一条是 `event='shutting_down'`，时间 02:23:18，sample 时间 10:29，**进程卡在 shutdown 已经 ~6 分钟**。

根因：22 轮只治了"按热键时 pynput CGEventTap 回调线程里调 AudioUnitStop"这一个入口，**但 sounddevice 模块 import 时还偷偷注册了一个 atexit `_exit_handler`**，它会在 `Py_FinalizeEx` 阶段主线程上跑 `Pa_Terminate` → `AudioUnitStop`，跟 22 轮同源的 CoreAudio HAL 锁互等，只不过触发频率低（每次退出一次，不是每次按热键一次），所以 22 轮验收期没复现。

### 开发内容概括

只改了一个文件 `src/whisper_input/__main__.py`（+85 / -2）：

1. **新增 `terminate_portaudio(timeout: float = 2.0) -> bool`**：动态 `import sounddevice as sd`，先 `atexit.unregister(sd._exit_handler)` 把默认退出 hook 摘掉，再起 daemon 线程调 `sd._terminate()`，主线程 `Event.wait(timeout)` 兜底。成功返 True，超时 / 异常 / sounddevice 未装都有优雅降级
2. **改 `shutdown()`**：顺序变成 `listener.stop() → wi.stop_worker() → terminate_portaudio() → settings_server.stop()`。worker 停完再关 PortAudio 保证没有 start/stop stream 在飞
3. **新增 `_final_exit()` 内部函数**：`terminate_portaudio` 超时时走 `os._exit(0)` 跳过 Python finalize，否则 `sys.exit(0)` 正常退。main() 里 tray 分支和 Linux / --no-tray 分支都改成统一调 `_final_exit()`

两层防御的逻辑：
- **主动层**：绝大多数情况下 `sd._terminate()` 在我们控制的上下文里能干净跑完（实测 `sd._initialized` 从 1 降到 0，耗时 <10ms）
- **兜底层**：万一 `sd._terminate()` 自己也死锁，2s 超时后 `os._exit(0)` 硬退，不留僵尸

### 额外产物

- **`tests/test_main_shutdown.py`**，7 个用例覆盖 `terminate_portaudio` 的各条路径：
  1. 正常路径返回 True 且调用了 `_terminate`
  2. `atexit.unregister` 确实把 handler 从链里摘掉
  3. `_terminate` hang 死时 200ms 内主线程能拿到 False 返回
  4. `_terminate` 抛异常时返回 False，不崩
  5. sounddevice 未安装时返回 True
  6. sounddevice 没有 `_terminate` 属性（上游改 API）时仍然 unregister + 返回 True
  7. 正常路径 <0.5s 完成

- **实机 sanity check**：在 dev venv 里直接 `terminate_portaudio()`，确认 `sd._initialized` 从 1 变 0，无挂起

测试状态：`uv run ruff check .` 全绿，`uv run pytest` 110 passed（22 轮的 103 + 本轮新增 7）

## 验收结果

**部分验收，受制于 bug 触发源未知**：

- ✅ **22 轮修复没有回归**：按热键 hot path 正常工作
- ✅ **正常退出路径能干净退出**：用户可以正常退出 App
- ⚠️ **无法主动复现原 bug**：02:23:18 那次 shutdown 是"某个未知的触发源"导致的，既然不知道是什么触发的，就没办法主动再触发一次验证修复有效。只能先把修复放进去，之后观察：
  - 如果再遇僵尸进程，用 `sample` 抓栈 + 看日志里有没有 `portaudio_terminate_timeout` / `force_exit` warning 判断走到了哪条路径
  - 如果 warning 出现，说明兜底超时生效（进程至少干净退了，没留僵尸），但 `_terminate()` 自己确实也会死锁 → 下一轮就放弃"优雅关" 方案直接 `os._exit(0)`
  - 如果没再出现僵尸，本轮可视为修复成功

## 局限性

- **`sd._terminate()` 是 sounddevice 的私有 API**：上游一旦改名就炸。缓解措施：`getattr` + 降级到"只 unregister atexit 不跑 terminate"（也能避免死锁，只是 PortAudio 状态泄漏——进程已死无所谓）。CI 里有 test 会发现上游改名
- **`os._exit(0)` 跳过所有 Python atexit 和 finally 块**：可能丢没刷盘的日志 / 配置。审查过，ConfigManager 保存是同步主动的，logger 是 stdlib logging + structlog 没 atexit 依赖，实际影响面小。但理论上仍是 trade-off
- **无法复现"02:23:18 是谁触发 shutdown 的"**：不追。本轮是"让 shutdown 在任何触发源下都能干净退"，来源已不重要
- **BACKLOG 里"启动时清理僵尸实例"条目仍保留**：本轮是正面修（不产生僵尸），那条是兜底（万一产生了怎么办），互相独立
- **Linux 实机没验证**：Linux 的 PortAudio 走 ALSA / PulseAudio 没 HAL 锁问题，`sd._terminate()` 跑一次无害。但形式上没 double check

## 后续 TODO

- 观察几天，如果再出现僵尸且日志里带 `force_exit` warning，说明就算在可控上下文里 `_terminate()` 也会死锁 —— 那就得彻底放弃"优雅关 PortAudio"的想法，只 unregister atexit + 直接 `os._exit(0)`，让 OS 回收资源
- 长期看，pynput / sounddevice / CoreAudio 这条链路在 macOS 上仍然脆弱。等哪天真正要搞稳定性 v2，可以考虑把音频录制放进单独的子进程（进程级隔离，崩了不影响主 App）
- 22 + 24 轮两次都是 CoreAudio HAL 锁的不同触发点。下次遇到类似症状，**第一反应就是 `sample <pid>` + 找 `HALB_Mutex::Lock`**。已经成模式了
