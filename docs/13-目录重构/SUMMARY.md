# 目录重构总结

## 开发背景

项目从 `0-初始灵感` 一路长到 0.4.0,代码结构是"长出来的":11 个 `.py` 文件堆在仓库根目录,和 `backends/` / `stt/` 两个子包、`.sh` 脚本、`macos/` / `debian/` 分发目录、`assets/` / `config.example.yaml` 全部混在同一层。不只是"不好看":

- **`whisper_input` 根本不是真 package**。`pyproject.toml` 没有 `[build-system]`,`uv sync` 只装依赖不装项目,`from backends import ...` 能 work 纯粹靠 CWD 恰好在 `sys.path` 上。一旦脱离"从仓库根 `uv run` 启动"这个前提,所有 import 都会炸。
- **`__file__` 路径拼接有 6 处**,分散在 `config_manager.py` / `version.py` / `backends/autostart_*.py`,靠 "`__file__` 往上跳 N 级" 定位 `assets/` / `config.example.yaml` / `pyproject.toml` / `main.py`。`.app` bundle 里的目录深度和 dev 不同、DEB flat 装到 `/opt/` 又是另一种形状,这堆写法每次重构都得重新对一遍,docs/7 的 TCC 权限事故里就摔过一次。
- **运行期 / 打包期 / 开发期资源没分层**。PyInstaller 模板、DEB control 文件、dev 期 shell 脚本全堆在仓库根同一层,读代码的人一眼看不出谁是谁。

本轮目标是一次性收敛成 **src layout + 单 distribution**,让:
1. 所有 Python 代码在 `src/whisper_input/` 下,`uv sync` 装成 editable wheel
2. 对外入口统一为 `whisper-input` console script(也支持 `python -m whisper_input`)
3. 运行期资源走 `importlib.resources`,打包产物走 `importlib.metadata`,彻底干掉 `__file__` 路径魔法
4. 开发期脚本进 `scripts/`,分发期模板进 `packaging/{macos,debian}/`,目录分层和语义一致

明确不做的事:不拆 `main.py` → `app.py+cli.py`、不加 `tests/`、不改业务逻辑、不升级依赖、不做 monorepo、不发 PyPI。

## 实现方案

### 关键设计

1. **src layout + hatchling build backend**。`pyproject.toml` 新增 `[build-system]`(hatchling)和 `[tool.hatch.build.targets.wheel] packages = ["src/whisper_input"]`,让 `uv sync` 把项目自己作为 editable wheel 安装进 venv。重构前 `uv.lock` 里 `whisper-input` 的 `source` 字段是 `virtual`(application 模式,项目不被安装),现在是 `editable`(项目作为真 package 安装)。

2. **console script 入口**。新增 `[project.scripts] whisper-input = "whisper_input.__main__:main"`,`__main__.py` 直接用原来 `main.py` 里已有的 `def main()`,两种调用方式都支持:
   - `uv run whisper-input`(venv 里生成的 binary)
   - `uv run python -m whisper_input`(标准 Python 模块入口)

3. **`__file__` → `importlib.resources` / `importlib.metadata`**。一共 6 处:
   - `version.py`:`__version__` 改用 `importlib.metadata.version("whisper-input")`,`__commit__` 从 package data `_commit.txt` 读取(`importlib.resources.files("whisper_input") / "_commit.txt"`),失败 fallback `git rev-parse HEAD`。
   - `config_manager.py`:新增 `_find_project_root()` helper,用 `.git/` + `pyproject.toml` 双 marker 从 package 位置往上探测 dev 仓库根。探测到即 dev 模式(用仓库根的 `config.yaml`),探测不到即 installed/bundled 模式(用平台 `CONFIG_DIR`)。example 配置永远从 `whisper_input.assets` 通过 `importlib.resources` 读,不再依赖 `INSTALL_DIR` 常量(已删除)。
   - `backends/autostart_linux.py`:`.desktop` 模板查找优先级变成"`/usr/share/applications/whisper-input.desktop`(DEB 装的) → `whisper_input.assets` package data(fallback)",移除 `__file__` 向上拼接。
   - `backends/autostart_macos.py`:`_bundle_trampoline()` 的 marker 检测不变(仍然靠 `__file__` 匹配 `/Contents/Resources/app/`,src layout 下匹配照样成立),dev 模式 fallback 从 `[sys.executable, main.py 绝对路径]` 改成 `[sys.prefix/bin/whisper-input]` 或 `[sys.executable, -m, whisper_input]`。

4. **dev / installed / bundled 三种模式的语义切分**
   - **Dev**:仓库根 `config.yaml` + `git rev-parse HEAD` + `.venv/bin/whisper-input`。通过 `.git + pyproject.toml` 双 marker 识别。
   - **Installed (DEB)**:`/opt/whisper-input/` flat 放 `src/whisper_input/` + `pyproject.toml`,`whisper-input.sh` trampoline → `setup_window.py` → `uv sync`(在 user venv) → `python -m whisper_input`。用户配置走 `CONFIG_DIR`。
   - **Bundled (macOS .app)**:`Contents/Resources/app/src/whisper_input/` + `pyproject.toml`,trampoline 同样经过三阶段 bootstrap,最后 `python -m whisper_input`。

5. **`setup_window.py` 适配**。这两个 bootstrap 文件运行在 bundled `python-build-standalone` 里,只有 stdlib。关键改动:
   - Stage B 的 `sys.path.insert(0, str(APP_SRC))` 改成 `sys.path.insert(0, str(APP_SRC / "src"))`,`from stt.downloader import ...` 改成 `from whisper_input.stt.downloader import ...`。这时 bundled python 虽然没装 `whisper_input`,但 `whisper_input.stt.downloader` 及其依赖链(`whisper_input.__init__` → `whisper_input.stt.__init__` → `whisper_input.stt.base` → `whisper_input.stt.model_paths`)全是纯 stdlib,import 得动。
   - Stage C 的 `[USER_VENV_PYTHON, APP_SRC / "main.py"]` 改成 `[USER_VENV_PYTHON, "-m", "whisper_input"]`。user venv 里 `uv sync` 已经把 whisper-input 作为 editable wheel 装好,`-m` 走 editable install 找到 `APP_SRC/src/whisper_input/` 下的代码。

6. **`build.sh` 整体瘦身**。原来 `SOURCE_PY` / `SOURCE_BACKENDS` / `SOURCE_STT` / `SOURCE_OTHER` 四个数组逐文件枚举,每次加文件都要记得同步。现在提取 `copy_src_tree()` 函数,直接 `cp -R src "$dest/src"` 整棵搬,外加 `pyproject.toml` / `uv.lock` / `.python-version` 三件套,最后 `find ... __pycache__ -exec rm -rf {} +` 清缓存。macOS 和 Linux 分支共用这个函数。Commit hash 从 `$DEST/commit.txt` 改成 `$DEST/src/whisper_input/_commit.txt`。脚本开头加 `cd "$REPO_ROOT"`,支持从任意 CWD 调用。

### 开发内容概括

按开发宪法的"分阶段 commit"原则,重构拆成 7 个独立 commit(详细理由是便于单独回滚,尤其是动核心路径解析的 Phase 4):

| Phase | Commit | 内容 | 文件数 |
|---|---|---|---|
| 1 | `d130966` | `git mv` 骨架搬运(无代码改动) | 46 |
| 2 | `383d979` | 所有 import 改为 `whisper_input.X` 绝对路径 | 9 |
| 3 | `62c01b9` | `pyproject.toml` 启用 src layout + console script | 4 |
| 4 | `0eb76be` | `__file__` 路径拼接改用 `importlib.resources` | 4 |
| 5 | `2c5c7c7` | `build/setup` 脚本适配 src layout 和 console script 入口 | 8 |
| 6 | `26c8018` | `.gitignore` commit.txt 路径更新 | 1 |
| 7 | `a5477d9` | `README` / `CLAUDE.md` / `__main__.py` 同步 src layout 结构 | 3 |

每个 Phase 结束 `uv run ruff check .` 都通过。

### 额外产物

- **删除了 `model_state.py`**。这是 12 轮重构时保留的向后兼容 shim,当时声称 `setup_window.py` 会通过它 import,但实际上 `setup_window.py` 早就直接引 `stt.model_paths` 了。grep 确认除 `model_state.py` 自己和若干 docs/build.sh 文本引用外,没有任何 `.py` 文件真的 import 它。顺手删了。
- **`_find_project_root()` helper**。`config_manager.py` 里新增的这个函数用 `.git` + `pyproject.toml` 双 marker 探测 dev 项目根,比原来的 `__file__` 启发式更稳。特意用 `.git/` 作为强信号——installed/bundled 产物永远不会有 `.git/`,误判概率极低。
- **`scripts/generate_icon.py` 定位改用 `repo_root` 相对**。从 `os.path.dirname(__file__)` 改成 `Path(__file__).resolve().parent.parent`,让输出路径正确落到 `src/whisper_input/assets/whisper-input.png`。

## 局限性

- **Phase 8 的手动测试没跑完**。自动化部分全绿(clean `uv sync` / console script / ruff / help 输出 / version / commit / `ConfigManager._resolve_path` / autostart plist 生成 / `.desktop` 模板读取 / shell 脚本 `bash -n` 语法 / setup_window.py 字节码编译),但涉及 UI / 权限 / 外部服务的部分需要人工验证:
  - [ ] `uv run whisper-input` 实际启动,托盘图标出现,热键录音 → STT → 粘贴链路通
  - [ ] Web 设置页能打开、改 config、改热键、开关自启
  - [ ] 自启开关触发后 `~/Library/LaunchAgents/com.whisper-input.plist` 内容正确
  - [ ] `bash scripts/build.sh` 在 macOS 上跑完,产出 `.app` 和 `.dmg`(~5 分钟,~250MB `python-build-standalone` 下载)
  - [ ] `.app` 冷启动跑完 `setup_window.py` 三阶段(uv sync / 模型下载 / `python -m whisper_input` 预加载),进托盘
  - [ ] Linux 机器上 `bash scripts/build.sh` → `sudo dpkg -i` → `whisper-input` trampoline 能一路跑通(我这边没 Linux 盒子)
- **Dev 模式 Linux autostart 会指向 `/usr/bin/whisper-input`**(`.desktop` 模板里硬编码)。dev 跑的时候 `/usr/bin/whisper-input` 不存在,开机自启会静默失败。不是本轮引入的问题,原来也一样,留着。
- **没加 `tests/`**。本就不在范围内,而且项目从头就没测试套,不是本轮造成的缺口。

## 后续 TODO

1. **手动 Phase 8 验证**——这是阻塞事项,在真正 push / 发版前必须跑完上面那张清单。打包 CI(GitHub Actions)的回归一并在 Phase 8 里验证,跑通后不需要额外 TODO。
2. **Linux dev 模式 autostart**——`.desktop` 模板当前硬编码 `Exec=/usr/bin/whisper-input`,dev 开发时切自启会失败。可以在 `set_autostart(True)` 里,如果探测到 dev 项目根,就动态 substitute `Exec=` 为 `.venv/bin/whisper-input` 的绝对路径。小改动,能让 dev 自启成为"真"可用。
3. **未来考虑 `tests/`**。目前没自动化测试的项目,后续做重大改动时踩坑概率是线性累积的。本轮不做只是因为 scope 要干净,不是说它不该做。

### 本轮内已清理的 TODO

- ✅ `config.example.yaml` 的过时描述已改为 ModelScope 231MB 直连
- ✅ `packaging/debian/postrm` cleanup 死代码已换成 `find ... __pycache__ -exec rm -rf {} +`

## 相关文档

- [PROMPT.md](PROMPT.md) — 需求文档
- [PLAN.md](PLAN.md) — 实施计划(带完整的 phase 分解和验证方案)
- 前置重构:[`docs/12-去torch-iic官方ONNX/`](../12-去torch-iic官方ONNX/) 为本轮提供了干净的 STT 依赖树(无 torch、无 funasr、5 个 stdlib only 的 `stt/*`),没有那轮的瘦身,本轮把 `whisper_input.stt.downloader` 塞进 bundled python stdlib 进程里跑的思路是不成立的
