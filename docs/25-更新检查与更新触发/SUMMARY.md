# 25 轮 - 更新检查与更新触发 - 总结

> 备注：本轮最初编号为 24，中途发现并修复了退出阶段 CoreAudio 死锁，
> 占用 24 轮编号，本开发项顺延到 25 轮。

## 开发项背景

- 14 轮把 whisper-input 发到了 PyPI 作为唯一官方分发通道，但**用户怎么
  知道有新版本？** 完全没机制。绝大多数用户不知道 `uv tool upgrade
  whisper-input` 命令的存在，"被动升级"路径等于没更新。
- 另外发现一个设置页面脚的小 bug：commit hash 链接指向 `/commit/<sha>`
  （GitHub diff 页），更符合"我现在跑的代码长什么样"意图的是 `/tree/<sha>`。
  顺手修掉。

## 实现方案

### 关键设计

**核心价值：让 PyPI 发版的价值真正触达用户**。之前 PyPI 上版本号不断在涨，
但用户跑的副本永远停在第一次 `uv tool install` 的版本 —— 我们发 release
的努力被这层"用户不知道 upgrade 命令"的信息差吃掉了。本轮就是把"检查
+ 一键升级"塞进设置页顶部横幅，让升级路径零学习成本。

**本轮在 scope 上走了一次重要的简化迭代**（由用户驱动）：

1. **初版设计**：写了 `detect_install_method()` 探测 `sys.prefix` 路径，
   区分 `uv-tool` / `pipx` / `pip` 三种安装方式，分别生成对应 upgrade 命令
2. **第一次收紧**：用户指出项目哲学是 uv first，连 pipx 都不支持了 ——
   删掉 PIPX / PIP 分支，只保留 UV_TOOL 和 UNKNOWN
3. **第二次收紧**：用户指出 install_method 探测本身就是画蛇添足 ——
   项目只支持 uv tool，探测不探测没有意义。删掉整个 `detect_install_method`
   概念，`apply_upgrade` 直接跑 `uv tool upgrade whisper-input`
4. **最后一层**：`is_dev()` 也被删 —— `__version__ == "dev"` 不是合法
   PEP 440 版本号，`is_newer()` 里 `Version("dev")` 会抛 `InvalidVersion`
   天然返 False，横幅在 dev 模式下不会出现，不需要显式判断

最终 [updater.py](src/whisper_input/updater.py) 只有 ~85 行，从"三分支
探测 + 三份升级命令 + dev 短路 + install_method 贯穿整条数据流"收敛成
"查 PyPI + 跑一条固定命令"。这轮最大的教训是：**先写最朴素的实现，
别预先给未来的"可能需要"留钩子**。

**其它几个有意识的决定**：

- **不自动重启** —— `uv tool upgrade` 覆盖完 venv 里的 `.py` 文件后，
  Python 进程已经 import 的模块不受影响（只在 import 时读文件）。老进程
  继续正常跑，用户按 UI 提示显式点"重启程序"按钮加载新版。这比强制重启
  健壮 —— 录音中 / 焦点在某个编辑器里打字时被自动重启打断会很糟糕
- **后台线程 + 2 秒超时**：`fetch_latest_version` 同步调 `urllib.request`，
  外面 `UpdateChecker._run_check` 包一个 daemon thread。和整个项目
  threading + blocking IO 的一致（`recorder` / `stt` / `settings_server` 同构）
- **dev 模式多花一次 PyPI 请求也接受**：删掉 `is_dev` 后，dev 模式启动
  时仍会发一次 PyPI 请求。每次 dev 启动多 200ms + 打一次 pypi.org，
  不值得为此单独留一行条件分支
- **全域禁 pip / pipx 落实到产品哲学**：这轮之后的 CLAUDE.md + README
  已经全面只保留 `uv tool install`，不再列 pipx / pip install 作为候选
  路径。一并扩写了全局 memory 规则：禁止 pip 不只在开发工作流，也延伸到
  代码里不得生成 pip / pipx 子进程命令

### 开发内容概括

**新模块** [src/whisper_input/updater.py](src/whisper_input/updater.py)（~85 行）：

- `fetch_latest_version(timeout=3.0)` —— 查
  `https://pypi.org/pypi/whisper-input/json` 拿 `info.version`，失败返 None
- `is_newer(latest, current)` —— 用 `packaging.version.Version` 做 PEP 440
  合规比较，非法版本号返 False
- `get_upgrade_command()` —— 用 `shutil.which("uv")` 找 uv 可执行文件，
  返 `[uv, "tool", "upgrade", "whisper-input"]`；找不到 uv 返 None
- `apply_upgrade(timeout=180)` —— 跑 upgrade 子进程，合并 stdout/stderr
  返回
- `UpdateChecker` —— 缓存最近一次检查结果，`trigger_async()` 启动
  daemon thread 去查、`snapshot` 暴露结果

**设置服务器** [settings_server.py](src/whisper_input/settings_server.py)：

- 两条新端点：`GET /api/update/check` 返回 UpdateChecker snapshot（配合
  `update.check_enabled=false` 时直接返 `has_update:false`）、
  `POST /api/update/apply` 同步跑 upgrade
- `SettingsServer.__init__` 实例化 `UpdateChecker`，`start()` 按开关
  triggers async 一次
- 顺手把 commit hash 链接从 `/commit/<sha>` 改成 `/tree/<sha>`

**设置页** [settings.html](src/whisper_input/assets/settings.html)：

- 顶部 `update-banner` 卡片（默认隐藏，横幅样式：橙色底色，左文本"发现
  新版本 vX.Y.Z → vA.B.C" + 右"立即更新"按钮）
- 高级设置区加"自动检查更新" toggle，绑 `update.check_enabled`
- JS `checkUpdate()` 页面加载时打一次 `/api/update/check`，`has_update`
  为 true 就 display 横幅；`applyUpdate()` 点按钮后 disable + 文案切
  "更新中..."，成功显示"已更新"并保持 disabled（防用户反复点）

**默认配置** [config_manager.py](src/whisper_input/config_manager.py)：
`DEFAULT_CONFIG` 新增 `update: {check_enabled: True}`。
`config.example.yaml` 同步。

**国际化** 三份 locale（zh / en / fr）各加 9 条：
`settings.update_check` / `settings.update_check_desc` / `update.new_version` /
`update.apply_btn` / `update.applying` / `update.done` / `update.done_btn` /
`update.failed` / `update.hint`。

**依赖** pyproject.toml 显式声明 `packaging>=23.0`（transitive 已有，
显式化避免未来 modelscope 改了隐式依赖链）。

### 额外产物

- **单测** [tests/test_updater.py](tests/test_updater.py) **22 条**：
  `is_newer` 的 7 条参数化 + `fetch_latest_version` 的 ok/非 200 /
  bad json / missing field / network error 5 条 + `get_upgrade_command`
  的 3 条（ok / uv missing / **防回归：cmd 不含 pip/pipx/python**）+
  `apply_upgrade` 的 4 条（missing uv / success / nonzero / timeout）+
  `UpdateChecker` 的 3 条（fetches and flags / no update / network failure）。
  `updater.py` 覆盖率 95%
- **集成测** [test_settings_server.py](tests/test_settings_server.py) 扩
  5 条：`commit` 链接防回归（`/tree/<sha>` 出现、`/commit/<sha>` 不出现）、
  `/api/update/check` 开关关闭不触发网络、开关打开返回正确 shape + 不含
  `install_method` 字段（防回归）、`/api/update/apply` 成功 / 失败路径
- **BACKLOG 新条目** "并发模型迁移到 asyncio"：这轮期间讨论出的未来方向，
  当前 threading 够用但方向上倾向 asyncio
- **全局 memory 新规则** [feedback_no_pip_anywhere.md](~/.claude/projects/-Users-jing-Developer-whisper-input/memory/feedback_no_pip_anywhere.md)：
  把"禁止 pip"从开发工作流扩展到"代码里不生成 pip/pipx 子进程 + 文档
  不列 pipx 作为安装方式"
- **文档清理** CLAUDE.md、README.md、README.zh-CN.md 删掉所有 pipx / pip
  install 的引用，只留 uv tool

## 局限性

- **自动升级按钮的端到端真实跑没有验证**。当前所有测试都是单测 + 集成
  mock；真正"点按钮 → uv tool upgrade 真跑完 → PyPI 装上新版"的链路
  要等下一次 PyPI 发版后，用 `uv tool install whisper-input==<当前版>`
  装一份旧版本让它去检测新版本才能跑通。这是这类功能的本质限制 ——
  除非发两个版本，否则自己验不了自己
- **dev 场景下横幅 UI 无法"真实"看到**：开发者 `uv run whisper-input`
  跑时 `__version__ == "dev"`，`is_newer` 返 False 所以横幅天然不显示。
  这是设计的（dev 就不该看到 prompt 升级），但代价是开发者想肉眼看
  横幅视觉效果只能临时 hack `__version__` 或临时把 pyproject.toml
  降版本做本地 wheel —— 目前靠开发者自觉
- **非 uv tool 装的用户**（比如直接 `pip install`）横幅仍然会显示，但点
  "立即更新"按钮只会得到"请在终端手动运行 `uv tool upgrade
  whisper-input`" 的提示。这是 **有意** 的折中：完全隐藏横幅意味着这类
  用户永远不知道有新版本，显示横幅 + 手动命令提示至少把信息差消除了

## 后续 TODO

- **端到端真实验证**：本轮收尾后 bump 到 0.7.3 发版，下一版（0.7.4 /
  0.8.0）发版前先 `uv tool install whisper-input==0.7.3` 装一份旧版，
  启动 → 应该能看到横幅 → 点按钮 → 看到 `uv tool upgrade` 真跑完 →
  重启看到新版本。这条链路跑通之后才算特性真正"上线"
- **升级结果缺少页面反馈以外的持久化**：现在升级完用户只看到一个
  toast，如果 toast 错过了就完全看不到升级成功与否。未来可以在设置页
  版本号旁边加"上次检查时间 / 上次升级结果"的 UI 展示
- **asyncio 迁移**：参见 BACKLOG，updater 是"临时还用 threading + 阻塞
  IO"的典型代表，届时会一并改造
