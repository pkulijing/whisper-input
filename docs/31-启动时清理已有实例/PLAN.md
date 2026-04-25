# PLAN：启动时清理已有实例

## 设计要点

**核心协议**:让老实例的 `settings_server` 暴露一个 `GET /api/pid` 端点返自己的 `os.getpid()`。新实例发现端口被占后用 HTTP 握手验证"是不是我们自己的实例",然后才 kill。

**为什么是 HTTP 而不是 PID 文件 / lsof / psutil**:

- HTTP 握手是**双向身份验证**:对方必须懂这个端点 = 是 daobidao,降低误杀其它占了 51230 的程序的风险
- 不引入新依赖(stdlib socket + urllib 够用)
- 跨平台同一份代码(lsof / psutil 在 Linux / macOS 上路径和 PID 字段都不一样)
- PID 文件需要新建路径 + atexit 清理 + PID 复用校验 + 残留处理,复杂度比 HTTP endpoint 高

**主路径**:

```
启动 main()
  → ConfigManager 加载 → 拿到 settings_port (默认 51230)
  → 端口探测:socket.connect(("127.0.0.1", port)) with 100ms timeout
      → 连不上(refused/timeout):没有老实例,正常启动
      → 能连上:有东西占着这个端口,继续 HTTP 握手
  → HTTP GET http://127.0.0.1:<port>/api/pid (1s timeout)
      → 返 200 + JSON {"pid": N}:认领老实例
          → os.kill(N, SIGTERM) → 1s 内端口仍占? → SIGKILL → 1s 内仍占? → 报错退出
          → 端口空了:正常启动
      → 404 / 非 200 / 超时:
          → 不是 daobidao 占的,或老实例 server 已死端口残留
          → 报错退出 + 提示用户手动检查 / 用 --allow-multiple
```

**`--allow-multiple` flag**:跳过上面整段检测,直接进入正常启动。给开发者两个 shell 各起一个的场景。

## 步骤

1. **`settings_server.py` 加 `GET /api/pid` 端点**
   - 在 `_SettingsHandler` 的 GET 路由表加一项:返 `{"pid": os.getpid()}` JSON
   - 故意不带任何鉴权(本来就只 listen 127.0.0.1,只有本机能访问)
   - **唯一的契约保证**:这个 endpoint 的存在 = 当前进程是 daobidao,新实例靠它做身份验证

2. **新建 `single_instance.py` 模块**(代码集中在这里方便单测):

   ```python
   def kill_stale_instance(port: int, timeout_per_step: float = 1.0) -> bool:
       """检测并清理 ``port`` 上占着的旧 daobidao 实例。

       返回 True 表示端口现在空闲(没老实例 / 已 kill),可以启动。
       返回 False 表示端口被占且不是 daobidao(需要用户手动处理),调用方应 sys.exit。
       """
   ```

   内部步骤:
   - `_port_in_use(port) -> bool`:`socket.connect_ex(...)` 100ms timeout
   - `_query_remote_pid(port) -> int | None`:`urllib.request.urlopen(f"http://127.0.0.1:{port}/api/pid", timeout=1.0)`,解 JSON,失败返 None
   - 主流程:
     - 端口空 → return True
     - 拿到 PID → SIGTERM → wait_for_port_free(timeout=1s) → 还占? → SIGKILL → wait → 仍占? → return False
     - 拿不到 PID → log warning + return False
   - 每步用 logger.info 记录决策,方便事后排查("killed_stale_instance pid=12345 method=SIGTERM port=51230")

3. **`__main__.py` 启动序列加 hook**:
   - 在 argparse 加 `--allow-multiple` flag
   - 加载 config 后 / 创建 `WhisperInput` 之前调一次 `kill_stale_instance(config.get("settings_port", 51230))`
   - 失败(返 False) → `logger.error(...) + sys.exit(1)` 给清晰提示

4. **测试**(`tests/test_single_instance.py`):
   - 端口空闲 → return True,无 kill 行为
   - 端口被假 server 占着 + 假 server `/api/pid` 返 200 PID → 触发 SIGTERM(用 mock os.kill 验证调用),端口在 mock 下"释放"→ return True
   - 端口被假 server 占着 + `/api/pid` 返 404 → return False,无 kill
   - 端口被占 + `/api/pid` timeout → return False
   - SIGTERM 没用 → 升级 SIGKILL(mock 验证)
   - SIGKILL 仍占 → return False
   - 不需要在 `__main__.py` 测试 `--allow-multiple` 集成,直接单测 `kill_stale_instance` 这一层就够

5. **文档**:
   - `CLAUDE.md` 提一行新启动行为 + `--allow-multiple` flag

## 风险与回滚

- **风险 1**:用户手动 `daobidao --no-tray` 跑了一个,然后又手动跑一个 — 第二个会 kill 第一个。**这就是设计里"双击 = 重启"的语义,符合预期**。给开发者用 `--allow-multiple` 兜底。
- **风险 2**:别人占了 51230 这种"运气真差"的情况 — `/api/pid` 返 404 → 报错退出 + 提示。用户改 config 里 `settings_port` 解决。
- **风险 3**:老实例 settings_server 起的 thread 死了但端口还绑着(子进程 fork 后未关闭等小概率场景) — `/api/pid` timeout → 报错退出。MVP 不自动用 lsof 兜底强 kill,留给用户。
- **风险 4(已澄清,无实质风险)**:macOS 上 Daobidao.app launcher 用 `dlopen libpython` 在**同一进程内**调 `daobidao.__main__.main()`(不 fork / 不 spawn,见 [`launcher/macos/main.m:127-188`](../../launcher/macos/main.m#L127-L188))。因此 `os.getpid()` 返的就是 launcher 自己的 PID,kill 它 = 整个 .app 退出,无父子进程问题。**仍要在 macOS 手测里跑一遍确认**。
- **回滚**:全部改动局限在 `settings_server.py` (新增一个 endpoint)、`__main__.py` (启动 hook + flag)、新增 `single_instance.py` + 单测。git revert 单 commit 即可。

## 验证清单

- `uv run pytest tests/test_single_instance.py -v` 全过
- `uv run pytest --no-cov -q` 整体仍 pass
- `uv run ruff check .` 干净
- **手测 1**(主路径):
  1. 终端 A 跑 `uv run daobidao`,确认能启动
  2. 终端 B 跑 `uv run daobidao`,**预期**:终端 A 退出,终端 B 起来
  3. log 里能看到 "killed_stale_instance pid=... method=SIGTERM"
- **手测 2**(`--allow-multiple`):
  1. 终端 A 起一个,终端 B 跑 `uv run daobidao --allow-multiple`
  2. **预期**:第二个起不来(端口被占 → settings_server bind 失败抛错),不影响 A
  3. 这个场景的"两个实例"实际上是 settings_port 冲突,不是这个 flag 能解决的,只是绕开了"主动 kill"。如果开发者真要两个实例并存,得每个用不同 settings_port,这点在 flag 帮助文本里写清楚
- **手测 3**(macOS .app 路径):
  1. 双击 Daobidao.app 起一个
  2. 再双击一次
  3. 预期老实例死,新实例起。`ps | grep Daobidao` 看老 PID 真的退了(launcher 是 in-process,kill PID = .app 整个退)
