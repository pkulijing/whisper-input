# SUMMARY:更新检测加 TTL + 顺手两件事

## 背景

### 触发的现象

老用户(装着 daobidao v1.0.1 的)发完 v1.0.3 之后,**设置页打开看不到「可
升级」横幅**,横幅一直说「无更新」。手动 `uv tool upgrade daobidao` 是有
效的,但 daobidao 自己的更新检测 UI 不再触发。

### 排查到的根因

`UpdateChecker` 自首次 `checked_at` 写满后**永不再查 PyPI**,直到进程重启。
[settings_server.py 老逻辑](src/daobidao/settings_server.py):

```python
if snap["checked_at"] is None and not snap["checking"]:
    checker.trigger_async()
```

只在「从未检查过」时触发。用户启动时拉过一次 PyPI(那时最新 = 已装版本 =
1.0.1, has_update=False),之后就只读那次缓存。新 release 上线后,得**重启
daobidao** 才能看到。

实际验证:用户重启进程后立刻看到「可升级到 1.0.3」横幅,锁死是 TTL 问题,
不是改名遗留 bug、也不是网络问题。

## 实现方案

### 关键设计

#### 1. UpdateChecker 加 TTL(主路径)

```python
_STALE_AFTER_SECONDS = 3600.0  # 1 小时

class UpdateChecker:
    def is_stale(self) -> bool:
        if self._checked_at is None:
            return True
        return (time.time() - self._checked_at) > _STALE_AFTER_SECONDS

    def trigger_if_stale(self) -> bool:
        if self._checking or not self.is_stale():
            return False
        return self.trigger_async()
```

`settings_server._handle_update_check` 把 if 判断改成一行
`checker.trigger_if_stale()`,stale-while-revalidate 语义:不阻塞前端,
旧 snapshot 立刻返,后台检查的结果留给下次轮询。

TTL 选 1 小时:用户来回开设置页这种短交互直接走缓存;真发新版本到用户
看见的延迟够短(1 小时内),又不会因为反复开页就刷 PyPI。

#### 2. 高级设置「立即检查」按钮(power-user 路径)

[settings.html](src/daobidao/assets/settings.html) 「自动检查更新」开关
**正下方**新增一行:

```
┌──────────────────────────────────────────┬────────┐
│ 手动检查更新                              │        │
│ 立即查询 PyPI 是否有新版本                │ [检查] │
└──────────────────────────────────────────┴────────┘
```

设计要点:**按钮只触发检测,不直接升级**。检测后发现新版本走顶部已有的
update banner(`#update_banner`)显示,「立即升级」按钮还是在 banner 上。
零新升级 UI,避免重复实现。

JS:`forceCheckUpdate()` POST `/api/update/check/force` → poll `/api/update/check`
直到 `checking=false` → 调现有 `checkUpdate()` 刷 banner。无新版本时弹一
条 toast「已是最新版本」给反馈。

后端 `/api/update/check/force` 端点直接调 `trigger_async()`(无视 TTL),
但仍尊重「自动检查更新」总开关 —— 用户关了它表示不想联网,force 也不绕过。

#### 3. 顺手:terminal log 默认静默(SUMMARY 里提,DEVTREE 不单列)

`configure_logging(level, *, stderr: bool = False)` 默认**只挂 file handler**,
terminal 不打 INFO log。文件日志一直写 `~/.local/state/daobidao/daobidao.log`
(macOS:`~/Library/Logs/Daobidao/`,dev 模式:`logs/`)。

`main()` 加 `--verbose` flag,显式 opt-in 才挂 stderr handler:

```bash
uv run daobidao              # 默认:terminal 干净,文件 log 还在
uv run daobidao --verbose    # 排错时打印 log 到 terminal
```

跟 `-v`(已经被 `--version` 占用)冲突,所以只用 `--verbose` 长格式,不
动现有 `-v` 行为。

#### 4. 顺手:吞掉 modelscope 的 print 杂讯(DEVTREE 不单列)

`modelscope.snapshot_download()` 不走 stdlib logging,用 `print()` 直接打:

```
Downloading Model from https://www.modelscope.cn to directory: ...
```

每次启动 cache 命中也照打。第 3 件事关掉 stderr 后这条还在,因为它是
**stdout** 不是 logger。

[qwen3_asr.py](src/daobidao/stt/qwen3/qwen3_asr.py) 的 `load()` 用
`contextlib.redirect_stdout(io.StringIO())` 把这块 stdout 吞掉。出错
时把吞下的内容转 `logger.error("modelscope_stdout_at_error", ...)` 留
诊断。tqdm 进度条走 stderr,真下载时仍可见。

### 开发内容

| 文件 | 改动 |
|---|---|
| `src/daobidao/updater.py` | `_STALE_AFTER_SECONDS=3600` 常量 + `is_stale()` / `trigger_if_stale()` 方法 |
| `src/daobidao/settings_server.py` | `_handle_update_check` 改 `trigger_if_stale()`;新增 `_handle_update_check_force` + `POST /api/update/check/force` 路由 |
| `src/daobidao/assets/settings.html` | 高级设置加「手动检查更新」一行 + 「检查」按钮 + `forceCheckUpdate()` JS |
| `src/daobidao/assets/locales/{zh,en,fr}.json` | 加 `settings.update_check_force*` / `update.checking` / `update.no_new_version` / `cli.verbose_help` 共 6 个 key |
| `src/daobidao/logger.py` | `configure_logging` 加 `stderr=False` keyword 参数,默认不挂 stderr handler |
| `src/daobidao/__main__.py` | 加 `--verbose` flag,在配置加载后用 `configure_logging(level, stderr=args.verbose)` re-init |
| `src/daobidao/stt/qwen3/qwen3_asr.py` | `load()` 用 `contextlib.redirect_stdout` 吞 modelscope 的 print 杂讯 |
| `tests/test_updater.py` | 加 6 条 TTL 单测 + 2 条 configure_logging 默认/显式 stderr 单测 |
| `tests/test_settings_server.py` | 加 2 条 force endpoint 端到端测试(force 触发 fetch / 总开关关时不 fetch) |
| `tests/test_logger.py` | `test_configure_logging_idempotent` 期望从 2 handler 改为 1(默认)/2(stderr=True) |

测试 367 全过(原 357 + 10 新加)。

### 额外产物

- `docs/34-更新检测TTL/PROMPT.md` + `PLAN.md`
- 顺手两条改动(terminal log 静默 + modelscope print 吞)在 SUMMARY 里记,
  不上 DEVTREE 节点 —— 它们是顺带做的小体验改善,不值得单独占一个开发轮

## 局限性

1. **TTL 选 1 小时是拍脑袋值**,没暴露成可配置。觉得太长 / 太短的用户得改源
   码 —— 但实际不太会有人想改,真改就改
2. **「立即检查」按钮放在「高级设置」里,普通用户找不到**。这是有意为之
   (避免污染默认 UX),但如果用户经常找不到,下一轮可考虑把按钮挪到 update
   banner 旁边作为常驻入口 —— 那会让「无新版本」状态下也露按钮,需要再设计
3. **`--verbose` 没有短形式**(`-v` 被 `--version` 占了)。改 `-v` 成
   verbose 会破坏现有用户脚本,故保守留长形式。新装用户不受影响
4. **modelscope 出错时 stdout 被吞了**,虽然有 `logger.error("modelscope_stdout_at_error", ...)`
   兜底,但只有真发生 `Exception` 时才打。如果 modelscope 正常返回但有
   warning print 也会被吞 —— 不过现在没观察到这种情况
5. **前端「立即检查」按钮 poll 上限 5s**,如果网络奇慢(>5s)会提前进入
   「已是最新」提示而 banner 实际还在等。极端情况,可接受

## 后续 TODO

- 观察一段时间,看 1 小时 TTL 是否合理。如果用户反馈「装了 1 小时 PyPI 出
  了新版本,我没看到」很普遍,缩短到 30 分钟或加个手动按钮 hint
- macOS launchd 跑 daobidao 时 stderr 被 launchd 接到 `daobidao-launchd.log`,
  现在默认 `stderr=False` 后这个文件应该是空的(因为没挂 stderr handler)。
  这是预期行为吗?如果 launchd 路径需要看 logger 输出,可在 macOS 后端
  显式传 `stderr=True`。**先观察,出问题再改**
- 「手动检查」按钮当前不显示「上次检查时间」,用户不知道距离 PyPI 现在
  数据有多新。可在 desc 里加「上次检查:5 分钟前」提示,纯前端时间格式化
