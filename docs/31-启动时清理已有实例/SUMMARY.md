# SUMMARY:启动时清理已有实例

## 开发项背景

### 希望解决的问题

调试 / 日常使用时偶尔会遇到上次没退出干净的僵尸进程,或者用户双击 .app 起了第二个,这时新启动的实例 `SettingsServer.start()` 会因端口 51230 被占抛 `OSError: [Errno 98] Address already in use` 静默崩溃。用户看到的是程序启动失败,只能 `ps | grep daobidao` 手动 kill 老的再重启,体验差。

期望:启动序列里加一个前置步骤,自动检测并清理老实例,**用户感知到的就是"双击启动 = 重启"**。

来自 BACKLOG"应用生命周期 → 启动时检测并清理已有实例"条目。

## 实现方案

### 关键设计

#### 协议:HTTP `/api/pid` 端点做身份验证

两个候选方向 (PID 文件 / lsof / psutil / fcntl 锁) 都各有问题。最终选 **HTTP 协议层握手**:

- 老实例的 `settings_server` 暴露 `GET /api/pid` 返 `os.getpid()`
- 新实例发现端口被占后,HTTP 拿 PID + 验证身份("对方懂这个端点 = 是 daobidao"),然后才 SIGTERM/SIGKILL

为什么 HTTP 比 PID 文件 / lsof 好:

| 方案 | 双向身份验证 | 跨平台 | 新依赖 | 残留处理 |
| --- | :---: | :---: | :---: | :---: |
| HTTP /api/pid | ✓ | ✓ | 无 (stdlib) | 无 |
| PID 文件 | ✗ (PID 复用风险) | ✓ | 无 | 需 atexit + 残留清理 |
| lsof / psutil | ✗ (拿到 PID 即 kill) | 跨平台路径 / 字段不一致 | psutil ~3 MB | 无 |
| fcntl flock | 仅判定 | macOS / Linux ok,Windows 不通 | 无 | 锁文件残留 |

HTTP 路径还能优雅退化:对方占着端口但 `/api/pid` 返 404 / 超时 → 判定"不是 daobidao" → 拒绝 kill 报错让用户处理。**PID 文件 / lsof 路径在这个 case 下会误杀别的占了 51230 的进程**。

#### kill 升级链

```
SIGTERM → 等 1s 端口空闲 → 仍占 → SIGKILL → 等 1s → 仍占 → 报错退出
```

每一步 `os.kill` 包了 `ProcessLookupError`(进程已自己退,端口残留中) 和 `PermissionError`(跨用户没权限),前者继续走 SIGKILL,后者立刻 fail-fast 退出。

#### `--allow-multiple` flag

跳过整套检测,给开发者两个 shell 各起一个调试用的场景。注意:这个 flag 不是"魔法兼容多实例",settings_port 仍会冲突,真要并存得改 config 各开各的端口。help 文本说清楚了。

### 开发内容概括

- [`src/daobidao/single_instance.py`](../../src/daobidao/single_instance.py) **新增** ~110 行:`kill_stale_instance(port)` 是唯一对外 API,内部 `_port_in_use` / `_query_remote_pid` / `_wait_port_free` 三个 helper。模块级常量 `_STEP_TIMEOUT=1.0` / `_PID_QUERY_TIMEOUT=1.0` / `_PORT_PROBE_TIMEOUT=0.1`,单测用 monkeypatch 改它们加速。
- [`src/daobidao/settings_server.py`](../../src/daobidao/settings_server.py) +5 行:`do_GET` 加 `/api/pid` 路由返 `{"pid": os.getpid()}`。
- [`src/daobidao/__main__.py`](../../src/daobidao/__main__.py) +14 行:argparse 加 `--allow-multiple`,在 macOS 权限检查之后 / `WhisperInput(config)` 之前调 `kill_stale_instance(settings_port)`,fail 时 `logger.error + sys.exit(1)` 给清晰提示。
- [`src/daobidao/assets/locales/{zh,en,fr}.json`](../../src/daobidao/assets/locales/) 各 +2 条:`cli.allow_multiple_help` + `main.stale_instance_blocked`。
- [`tests/test_single_instance.py`](../../tests/test_single_instance.py) **新增** ~270 行,16 个 case:端口空闲、SIGTERM 成功、升级 SIGKILL、kill 失败、404 拒杀、非法 JSON 拒杀、payload 缺 `pid` 字段拒杀(parametrize 5 种 invalid payload)、ProcessLookupError 处理、PermissionError 处理 + 3 个 helper 直测。用 fake `HTTPServer` 在后台线程模拟"占着端口的进程",monkeypatch `os.kill` 验证升级链。
- [`CLAUDE.md`](../../CLAUDE.md) 更新 3 处:`uv run` 命令列表加 `--allow-multiple`,`__main__.py` 模块描述补一段启动检测,新增 `single_instance.py` 模块说明。

### 额外产物

- [PROMPT.md](PROMPT.md) + [PLAN.md](PLAN.md):标准开发流程
- **意外的"老版本升级测试"**:跑手测前发现机器上还跑着 v1.0.0 实例(没 `/api/pid` 端点),新实例正确判定为"非 daobidao"拒绝 kill,符合预期的安全语义。这一段实战记入 SUMMARY 的"局限性"。

## 验证

- `uv run ruff check .` 全过
- `uv run pytest --no-cov -q` **312 passed, 0 skipped, 0 failed** (+16 新单测,从 296 → 312)
- 手测 1(主路径):终端 A 起一个,终端 B 启动 → A 收 SIGTERM 退出,B 拿到端口,日志 `killed_stale_instance pid=380856 port=51230 signal=SIGTERM`,只用到 SIGTERM,没升级 SIGKILL ✓
- 手测 2(`--allow-multiple`):A 在跑,B 加 flag → 跳过 kill → `HTTPServer.bind` 抛 `OSError: [Errno 98]` exit 1 → A 安然无恙 ✓
- 手测 2.5(意外触发的"非 daobidao 占用方"路径):机器上已有 v1.0.0 实例 → 新实例 GET `/api/pid` 返 404 → 判定不可信 → 报错退出不误杀 ✓

## 局限性

1. **macOS .app 路径手测未做**:开发机是 Linux,手头没 Mac 设备。PLAN 里的"手测 3"(双击 Daobidao.app 起一个,再双击,验证老 .app 真的退、launcher 父进程跟着退)需要后续补做。launcher 是 in-process `dlopen libpython` 跑(见 [`launcher/macos/main.m:127-188`](../../launcher/macos/main.m#L127-L188)),理论上 `os.getpid()` 就是 launcher 的 PID,kill 它 = .app 整个退,**无父子进程问题**——但仍需实测确认。
2. **从 v1.0.0 升级到 v1.0.x 的第一次启动会报错而不是 kill**:老实例没 `/api/pid` 端点,新实例 GET 返 404 → 安全语义判定"非 daobidao" → 报错退出。用户必须手动 kill 一次老的,之后所有版本都自带 `/api/pid` 不再有这个问题。**这是正确取舍**:宁可让一次升级时麻烦点,也不要写 lsof / psutil fallback 把"端口占用方 = daobidao"的判定降级为"端口占用方就 kill",那会引入误杀别人占了 51230 的进程的风险。
3. **僵尸状态的精细判断未做**:"进程在但热键监听死了 / settings_server thread 死了" 这种细化判断 BACKLOG 提到过,本轮按 PROMPT 不做。MVP 语义是"端口被占且 `/api/pid` 200 响应 = kill"。如果哪天碰到"进程在但 server thread 死了端口残留"的情况,新实例会因 `/api/pid` timeout 报错退出,需要用户手动 `kill` —— 不致命但 UX 不好,以后真碰到再考虑加 `/health` 或 lsof fallback。

## 后续 TODO

1. **macOS 手测 3**:用户在有 Mac 设备时跑一遍。重点确认:
   - 双击 Daobidao.app 起一个,再双击 → 老 .app 真的退、launcher 父进程跟着退、新 .app 起来
   - macOS log: `Console.app` 里搜 `killed_stale_instance` 看日志输出对
   - 涉及 TCC 权限的细节:新实例 inherit launcher 的 TCC 授权状态 (round 19 设计)
2. **从 v1.0.0 升级文档**:在 release notes 里说一句"首次升级到 v1.0.2+ 时,如果之前在跑 v1.0.0 / v1.0.1,需要先手动 `pkill -f daobidao` 或重启,因为老版本没有 `/api/pid` 端点会被新版当成未识别占用方"。
3. **补 `__main__.py` 的集成测试**(可选):本轮单测全在 `single_instance` 这一层,`__main__.main()` 里"拿 settings_port → 调 `kill_stale_instance` → fail 时 `sys.exit(1)`"这条 wiring 没单测覆盖。如果觉得风险高可以加一条端到端 mock 测试,不阻塞本轮。
