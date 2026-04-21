# 25 轮 - 更新检查与更新触发

## 背景

14 轮发到 PyPI 之后，用户怎么知道有新版本？目前完全没机制。
绝大多数用户不知道 `uv tool upgrade whisper-input` 这个命令的存在，被动路径等于没更新。

另外发现一个设置页的小 bug：页脚 commit 链接指向 `/commit/<sha>`（是 diff 页），应该指向
`/tree/<sha>`（是该 commit 时刻的文件浏览页），更符合"我现在跑的代码长什么样"的意图。

## 要做的两件事

### 1. 设置页面的更新检查 + 更新触发（主项）

**希望达到的效果**：

- 启动后在后台静默查一次 PyPI，看看是否有新版本
- 有新版时，设置页顶部（或版本号附近）出现一个小横幅："发现新版本 v0.x.y"
- 横幅旁边一个"更新"按钮，点击后应用自动跑 `uv tool upgrade whisper-input`
  （或 pipx 对应命令），完成后提示用户重启
- dev 模式（`__version__ == "dev"`）不弹横幅、不做检查
- 检查是**可关闭**的 —— 设置项里给一个开关，关掉就完全不查

**技术点**：

- **查版本**：`GET https://pypi.org/pypi/whisper-input/json`，读 `info.version` 字段。
  PyPI 不要求鉴权、也不限频
- **区分安装方式**：看 `sys.prefix` 路径
  - `.../uv/tools/whisper-input/...` → 用 `uv tool upgrade whisper-input`
  - `.../pipx/venvs/whisper-input/...` → 用 `pipx upgrade whisper-input`
  - 其他 → 回退到 `pip install --upgrade whisper-input`（bare venv / 系统 pip）
- **后台线程 + 短超时**：所有网络请求都要放到后台线程里，配 2-3 秒超时，
  失败静默，不要拖启动和设置页响应

**风险 / 注意点**：

- 检查路径加网络请求会增加启动时的感知开销 —— 必须放到后台线程 + 支持开关关闭
- 更新触发期间 whisper-input 自己在跑，upgrade 命令覆盖 venv 里的文件，
  subprocess 体验怎样不确定（可能需要"更新完 → 提示用户手动重启"流程，
  而不是自动重启，因为热键监听线程仍在老代码里跑）
- 用户点按钮但网络断了 / PyPI 临时挂了 / upgrade 子进程失败，错误提示要友好

**scope 预估**：中。~200 行 Python 代码 + 2-3 处 Web UI 改动（横幅 + 按钮 + 开关）。

### 2. 设置页 commit 链接指向 tree 而非 commit（顺手修）

**现状**：`settings_server.py:86` 生成的链接是
`https://github.com/pkulijing/whisper-input/commit/<sha>`。

**期望**：改成 `https://github.com/pkulijing/whisper-input/tree/<sha>`。

**scope**：一行改动。搭车这轮一起修掉。
