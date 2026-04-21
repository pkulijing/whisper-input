# 25 轮实现计划 - 更新检查与更新触发

## 目标概览

两件事合并在一轮里做：

1. **主项**：设置页加 "检查更新 + 一键触发更新" 能力
2. **顺手**：页脚 commit 链接从 `/commit/<sha>` 改成 `/tree/<sha>`

## 一、commit 链接修复（5 分钟）

单点改动：[src/whisper_input/settings_server.py:86](src/whisper_input/settings_server.py#L86)
把 `f'commit/{__commit__}'` 替换成 `f'tree/{__commit__}'`。就这一行。

## 二、更新检查与触发（主体）

### 架构概览

- 新增 `src/whisper_input/updater.py` —— 所有更新相关逻辑的集中模块
- `settings_server.py` 新增两个 REST 端点：`GET /api/update/check`、
  `POST /api/update/apply`
- `settings.html` 新增：顶部横幅（有新版时可见）、"自动检查更新" 开关
- `config_manager.py` 新增默认键 `update.check_enabled = True`

### 模块设计：`updater.py`

暴露以下顶层函数：

```python
def detect_install_method() -> str:
    """返回 "uv-tool" | "pipx" | "pip" | "dev"。
    基于 sys.prefix 路径匹配:
    - 含 /uv/tools/whisper-input/  → uv-tool
    - 含 /pipx/venvs/whisper-input/ → pipx
    - __version__ == "dev"          → dev
    - 否则                          → pip
    """


def fetch_latest_version(timeout: float = 3.0) -> str | None:
    """同步查 PyPI 最新版本号。失败返回 None，不抛异常。
    用 stdlib urllib.request 直接 GET
    https://pypi.org/pypi/whisper-input/json 解析 info.version。
    """


def is_newer(latest: str, current: str) -> bool:
    """packaging.version.Version 比较；任一不合法则 False。"""


def get_upgrade_command(install_method: str) -> list[str] | None:
    """返回 subprocess 可执行的 argv。dev 模式返回 None。
    - uv-tool → ["uv", "tool", "upgrade", "whisper-input"]
    - pipx    → ["pipx", "upgrade", "whisper-input"]
    - pip     → [sys.executable, "-m", "pip", "install", "--upgrade",
                "whisper-input"]
    """


def apply_upgrade(install_method: str, timeout: float = 180.0)
        -> tuple[bool, str]:
    """同步执行 upgrade 命令，返回 (ok, combined_stdout_stderr)。
    stdout+stderr 合并成一个字符串，便于前端展示。
    """


class UpdateChecker:
    """后台线程持有最近一次检查结果，供 HTTP handler 读取。"""

    def __init__(self, current_version: str):
        # 缓存: latest / checked_at / has_update / install_method
        ...

    def trigger_async(self) -> None:
        """启动后台线程去查一次，已在查的跳过。dev 模式不查。"""

    @property
    def snapshot(self) -> dict:
        """返回可序列化的 dict：
        {
          "current": str,
          "latest": str | None,
          "has_update": bool,
          "install_method": str,
          "checking": bool,
          "checked_at": float | None,    # unix 秒
          "error": str | None,
        }
        """
```

**关键设计取舍**：

- **同步 HTTP + 外面包线程**，不用 asyncio。保持整个项目"后台用 threading"的一致性
  （recorder / stt / settings server 都这么写）
- **失败静默**：网络异常 / 非 200 响应 / JSON 结构变了，一律把 `error` 写进
  snapshot 返回。前端看到 `error` 就什么横幅都不显示
- **版本比较用 `packaging.version.Version`**：标准库里 `setuptools` 和
  `modelscope` 都依赖它，但保险起见在 `pyproject.toml` 里显式声明
- **`apply_upgrade` 超时 180s**：upgrade 实际一般 10-30s，留足冗余。超时后
  subprocess 被 kill，前端收到错误提示
- **dev 模式**（`__version__ == "dev"`）所有函数早返：
  - `detect_install_method()` → `"dev"`
  - `UpdateChecker.trigger_async()` → 不跑
  - `snapshot` 永远 `has_update=False`，前端天然不显示横幅

### `settings_server.py` 改动

1. 顶层引入 `UpdateChecker`，在 `SettingsServer.__init__` 里实例化并存到 server
   属性上（和现在的 `config_manager` / `on_config_changed` 同一套路）
2. `SettingsServer.start()` 里判断 `config.update.check_enabled`：为 True
   才调 `checker.trigger_async()`
3. 新增两个路由：

```python
# GET /api/update/check
# 返回 checker.snapshot。如果 snapshot.checked_at 为 None 且不在检查中,
# 顺手 trigger_async() 一次(应对"启动时检查关了后来又开了"场景)。
# 注意:config.update.check_enabled=False 时直接返回 {"has_update": False}
# 不访问网络

# POST /api/update/apply
# 同步调 apply_upgrade()，返回 {"ok": bool, "output": str}。
# 不要在这里自动重启 —— 让用户显式点"重启程序"按钮
```

4. commit 链接 `/commit/` → `/tree/` 改 1 行

### `config_manager.py` 改动

`DEFAULT_CONFIG` 加一层：

```python
"update": {
    "check_enabled": True,
},
```

### `settings.html` 改动

1. **顶部横幅**：在 `<div class="container">` 第一个 card 前插入一个
   `update-banner` 卡片，默认 `display:none`，内部结构：

   ```html
   <div class="card update-banner" id="update_banner" style="display:none;">
     <div class="update-banner-body">
       <div class="update-text">
         <strong data-i18n="update.new_version">发现新版本</strong>
         v<span id="update_current"></span> → v<span id="update_latest"></span>
       </div>
       <button class="btn btn-primary" id="update_apply_btn"
               onclick="applyUpdate()" data-i18n="update.apply_btn">
         立即更新
       </button>
     </div>
     <div class="setting-desc update-banner-hint" data-i18n="update.hint">
       更新完成后需要手动重启 Whisper Input
     </div>
   </div>
   ```

2. **高级设置卡片**里加一条 "自动检查更新" 开关 —— 和
   `sound_enabled` / `overlay_enabled` 同样的 toggle 样式，绑 `update.check_enabled`

3. **JS**：

   ```js
   async function checkUpdate() {
     try {
       var res = await fetch('/api/update/check');
       var data = await res.json();
       if (data.has_update) {
         document.getElementById('update_current').textContent = data.current;
         document.getElementById('update_latest').textContent = data.latest;
         document.getElementById('update_banner').style.display = 'block';
       }
     } catch (_) {}
   }

   async function applyUpdate() {
     var btn = document.getElementById('update_apply_btn');
     btn.disabled = true;
     btn.textContent = i18n('update.applying');
     try {
       var res = await fetch('/api/update/apply', {method: 'POST'});
       var data = await res.json();
       if (data.ok) {
         showToast(i18n('update.done'), 4000);
         btn.textContent = i18n('update.done_btn');
       } else {
         showToast(i18n('update.failed') + ': ' + (data.output || ''), 5000);
         btn.disabled = false;
         btn.textContent = i18n('update.apply_btn');
       }
     } catch (e) {
       showToast(i18n('update.failed') + ': ' + e.message, 5000);
       btn.disabled = false;
     }
   }
   ```

   在 `loadConfig().then(...)` 链末尾加 `checkUpdate()`

### 国际化文案

三个 locale 文件各加这几条：

- `update.new_version` 发现新版本 / New version available / Nouvelle version disponible
- `update.apply_btn` 立即更新 / Update now / Mettre à jour
- `update.applying` 更新中... / Updating... / Mise à jour...
- `update.done` 更新完成，请手动重启 Whisper Input / Update finished, please restart / ...
- `update.done_btn` 已更新 / Updated / Mis à jour
- `update.failed` 更新失败 / Update failed / Échec
- `update.hint` 更新完成后需要手动重启 Whisper Input / ... / ...
- `settings.update_check` 自动检查更新 / Auto check for updates / ...
- `settings.update_check_desc` 启动时查询 PyPI 是否有新版本 / Query PyPI on startup / ...

### 依赖

`pyproject.toml` 加一行 `packaging>=23.0`（transitive 已有，显式声明更稳）。

### 测试（`tests/test_updater.py` + 扩 `test_settings_server.py`）

新文件 `tests/test_updater.py`：

- `detect_install_method` 用 monkeypatch 改 `sys.prefix` 走三条分支
- `is_newer` 几种组合（"0.7.2" vs "0.7.3" / 相等 / 非法）
- `fetch_latest_version` monkeypatch `urllib.request.urlopen` 返回假 JSON
- `apply_upgrade` monkeypatch `subprocess.run` 返回 fake CompletedProcess
- `UpdateChecker.trigger_async` —— 触发一次后等线程结束，验证 snapshot 字段

扩 `test_settings_server.py`：

- `/api/update/check` 开关关 → 返回 has_update=False 且不打网络
- `/api/update/check` 开关开 → 走 fake fetcher，返回正确 shape
- `/api/update/apply` → 走 fake upgrader，返回 ok
- HTML 渲染里 `/tree/` 链接存在 且 `/commit/` 不再出现（保护 bug 回归）

Coverage 目标：`updater.py` ≥ 85%（主要分支都测到）。

## 执行顺序

1. `updater.py` 新模块 + 单测
2. `config_manager.py` 加 `update.check_enabled` 默认值
3. `settings_server.py` 接入 UpdateChecker + 两个新端点 + commit 链接修复
4. `settings.html` 加横幅 + 开关 + JS
5. 三个 locale 文件加新 key
6. `pyproject.toml` 加 `packaging` 依赖
7. 扩 `test_settings_server.py`
8. `uv run pytest` / `uv run ruff check .` 全绿
9. `uv run whisper-input` 手动验证：横幅不显示（dev 模式）→ 手动改 `__version__` 打桩验证横幅出现 + 按钮点击流程

## 遗留风险

- **upgrade 子进程覆盖正在运行的 venv 文件**：macOS / Linux 下 Python
  进程持有的 `.py` 文件被替换不会崩（Python 只在 import 时读）。老代码继续跑，
  用户点重启后加载新版本。这就是"提示用户手动重启"而不是自动重启的原因
- **`uv` / `pipx` 二进制可能不在 PATH 里**：macOS 用户如果用 `app bundle`
  启动，GUI 继承的 PATH 不一定有 `~/.local/bin`。用 `shutil.which("uv")`
  退化时，若找不到 uv，回退到 `uv tool` 的 pipx 路径探测；再找不到就在前端
  提示 "请在终端手动运行 `uv tool upgrade whisper-input`"
- **PyPI 返回结构变化**：我们只依赖 `info.version` 字段，这是 PyPI JSON API
  10 年没变过的稳定契约，风险极低

## 预估工作量

- 代码：`updater.py` ~150 行 + `settings_server.py` +40 行 + HTML +80 行
- 测试：~120 行
- 预估 2-3 小时完成全部代码，加手动验证 30 分钟

## 完成标准

- [ ] `uv run pytest` 全绿，新增用例 ≥ 10 条
- [ ] `uv run ruff check .` 0 warnings
- [ ] dev 模式启动无网络请求（代码检查 + 手动 `tcpdump` 观察）
- [ ] 手动模拟低版本：设置页顶部出现横幅，点更新后终端可见 uv 执行日志
- [ ] commit 链接点进去是 tree 页
- [ ] `SUMMARY.md` 按模板写好
