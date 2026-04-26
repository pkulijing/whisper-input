# 实现计划

## 关键设计

### 1. TTL 值:1 小时

理由:用户打开设置页通常是「想看看版本/想升级」的语义,1 小时内重复打开
当作短交互,直接走缓存;超过 1 小时算「另一个会话」,值得重查。

PyPI JSON API 是公共的、无 rate limit 痛点,1 小时拉一次完全无负担。

### 2. UpdateChecker API

把 TTL + 强制检查行为都收进 `UpdateChecker` 自己,不让 `settings_server`
关心时间逻辑。

```python
class UpdateChecker:
    _STALE_AFTER_SECONDS: ClassVar[float] = 3600.0  # 1 小时

    def is_stale(self) -> bool:
        """从未检查过 / 上次检查超过 TTL 都算 stale。"""
        with self._lock:
            if self._checked_at is None:
                return True
            return (time.time() - self._checked_at) > self._STALE_AFTER_SECONDS

    def trigger_if_stale(self) -> bool:
        """缓存过期就触发后台检查,新鲜或正在检查中跳过。
        返回 True 表示新启动了一次,False 表示没启动。"""
        if self._checking:
            return False
        if not self.is_stale():
            return False
        return self.trigger_async()

    # force_check_async 不需要单独实现 —— trigger_async 本身就是
    # "无视 TTL,但已在检查中则跳过",直接复用即可。settings_server 的
    # 强制检查端点直接调 trigger_async()。
```

### 3. Stale-while-revalidate 语义

`GET /api/update/check` 在 stale 时**不阻塞**等待新结果,直接返回缓存的
旧 snapshot + 后台启动新检查。下次设置页轮询 / 重新打开时拿到新结果。

`POST /api/update/check/force`(新):无视 TTL 直接 `trigger_async()`,然后
返回当前 snapshot。跟 stale 路径一样,不阻塞。

跟现有 first-check 流程一致(`trigger_async` + 立刻返 snapshot,JS 端 poll
`checking` 状态再刷新),不需要改前端 polling 逻辑。

### 4. 设置页:「立即检查」按钮

放在 [settings.html:317](src/daobidao/assets/settings.html#L317) 的「自动
检查更新」开关**正下方**,跟它一组(同属「高级设置」section)。

设计(对仗上方「自动检查更新」toggle):

```html
<div class="setting-row">
  <div>
    <div class="setting-label" data-i18n="settings.update_check_force">
      手动检查更新
    </div>
    <div class="setting-desc" data-i18n="settings.update_check_force_desc">
      立即查询 PyPI 是否有新版本
    </div>
  </div>
  <button
    class="btn"
    id="update_check_force_btn"
    onclick="forceCheckUpdate()"
    data-i18n="settings.update_check_force_btn"
  >
    检查
  </button>
</div>
```

文案不暴露「一小时缓存」实现细节;按钮文字简化为「检查」。

按钮**只触发检测,不触发升级**。检测完发现新版本走现有顶部 update banner
(`#update_banner`)的展示路径,「立即升级」按钮在 banner 上,这里不复制
任何升级 UI。

JS:`forceCheckUpdate()` POST `/api/update/check/force`,然后立即调一次
现有的 `checkUpdate()`(已经会拉 snapshot 并刷 banner 显示)。按钮在请求
期间 disable,完成后恢复。

i18n key 加到 zh / en / fr 三份 locale。

## 测试计划

### Backend (TDD)

新增测试 `tests/test_updater.py`:

| 测试                                                   | 期望                                                      |
| ------------------------------------------------------ | --------------------------------------------------------- |
| `test_is_stale_when_never_checked`                     | 新建 UpdateChecker,is_stale() 返 True                     |
| `test_is_stale_when_recently_checked`                  | mock time,checked_at = now - 60s,is_stale() 返 False      |
| `test_is_stale_when_old_check`                         | mock time,checked_at = now - 7200s(2h),is_stale() 返 True |
| `test_trigger_if_stale_first_call`                     | 新建实例 → 调 → 真启动了线程,返 True                      |
| `test_trigger_if_stale_returns_false_when_fresh`       | 已有 fresh checked_at → 调 → 不启动,返 False              |
| `test_trigger_if_stale_returns_false_when_in_progress` | `_checking=True` → 调 → 不启动,返 False                   |

新增测试 `tests/test_settings_server.py`:

| 测试                                              | 期望                                                      |
| ------------------------------------------------- | --------------------------------------------------------- |
| `test_update_check_force_endpoint_triggers_check` | POST /api/update/check/force → 200 + 启动了 trigger_async |
| `test_update_check_force_returns_snapshot`        | POST → 返回 snapshot(含 checking 字段)                    |

时间相关 mock:`monkeypatch.setattr("daobidao.updater.time.time", ...)`。
注意 `_run_check` 内部也调 `time.time()`,mock 范围要保持一致。

### Frontend

UI 改动手测(无 JS 单元测试基础设施):

- 启动 dev daobidao,打开设置页
- 看到「手动检查更新」按钮在「自动检查更新」开关下方
- 点击 → 1-2s 内 banner 出现「v1.0.3 可升级」(或保持 hidden 如果已是最新)
- 期间按钮 disable
- 中英法三语切换正常显示

### 5. 顺手:terminal log 静默(默认关 stderr handler)

`configure_logging(level)` 现在无条件加 stderr handler。改成默认**不**加,
只有显式 opt-in 才挂:

```python
def configure_logging(level: str = "INFO", *, stderr: bool = False) -> None:
    ...
    file_handler = ...
    root.addHandler(file_handler)
    if stderr:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(stderr_formatter)
        root.addHandler(stderr_handler)
    ...
```

`main()` 加 CLI flag:

```python
parser.add_argument("-v", "--verbose", action="store_true",
                    help=t("cli.verbose_help"))  # i18n 加 key
...
configure_logging(level=..., stderr=args.verbose)
```

**影响范围**:

- 现有 `test_configure_logging_idempotent` 默认期望 2 handlers(file + stderr),
  改成 `terminal=False` 时 1,`terminal=True` 时 2
- 现有 macOS launchd 流程不受影响 —— launchd 用 `StandardErrorPath` 直接
  捕获 stderr,跟 Python logger 的 stderr handler 是两回事;launchd 路径
  跑的是 daemon 模式,不传 `--verbose`,Python 这层不输出,launchd 的 stderr
  捕获文件大概率是空的,但 daobidao-launchd.log 本来就是给 pre-logger 阶段
  崩溃用的,空文件无害

不上 DEVTREE 节点 —— 跟 TTL 同一轮顺手做,SUMMARY 提一下即可。

## 落地顺序

1. 跑一次基线 ruff + pytest 确认绿
2. 写 8 条新后端测试(6 个 TTL + 2 个 force endpoint,先红)
3. 实现 UpdateChecker `is_stale()` / `trigger_if_stale()`
4. `_handle_update_check` 切换到 `trigger_if_stale()`
5. 加 `_handle_update_check_force` + endpoint 路由
6. 加 settings.html 按钮 + JS + i18n key (zh/en/fr)
7. **顺手 terminal log 静默**:`configure_logging(stderr=...)` + `--verbose` flag,
   更新现有 `test_configure_logging_idempotent`
8. 全套测试绿 + 手测 UI(打开设置页验「立即检查」+ 命令行启动验默认无 log
   spam + 加 `--verbose` 验恢复输出)
9. /commit + /pybump patch + tag

## 风险 / 局限

- TTL 写死 1 小时,没暴露成可配置 —— 没人会想动它,真要动直接改源码
- 单元测试 mock time 容易出错(忘记同时 mock checker 内部的 time 调用)
- 「立即检查」按钮是 power-user 功能,放在「高级设置」section 不显眼。
  如果用户找不到,可以下一轮挪到 update banner 旁边作为常驻按钮 —— 但
  那会让「无新版本」状态下也露按钮,污染默认 UX,**本轮先放高级设置**
- 没改前端 polling:JS `checkUpdate()` 是单次 GET,不 poll `checking` 状态。
  这意味着 force 按钮点击后,如果 `trigger_async()` 还没返回,banner 不会
  立刻出。验证下来如果体验差,顺手加个 1s 后重 fetch 一次的小逻辑
