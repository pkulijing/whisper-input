# 开发总结：架构整理 + overlay 视觉统一

## 开发项背景

### 针对正向开发

项目的 `backends/` 目录已经对 hotkey、input_method、overlay、autostart 做了平台分离，但 `__main__.py` 里的 `run_tray()` 函数（170 行，大量 `is_mac` / `sys.platform` 分支）是唯一没拆进 `backends/` 的平台相关模块。

同时两个平台的 overlay 视觉不一致：Linux 用 50 行矢量 Cairo 路径画的复古麦克风，macOS 用 emoji；都是 120×120 的黑色大方块配波纹弧线——不够现代、不够克制。

此外 `settings_server.py` 内嵌了 ~460 行 HTML 模板字符串（`SETTINGS_HTML`），用 8 个 placeholder 字符串替换做模板渲染，代码观感差。

## 实现方案

### 关键设计

1. **tray 拆分**：完全遵循现有 dispatcher 模式（同 hotkey.py / input_method.py），`_draw_mic` / `create_icon` 等共享代码选择在两个后端文件中各复制一份（~50 行），不抽 `_tray_common.py`——两个平台的图标行为已经不同（品牌色 vs 模板图、红点 badge），分开更易独立演化

2. **overlay 视觉**：参考微信输入法的浮窗风格——深蓝 `#1E3A8A` 药丸底 + 居中白色 🎙 emoji + 两侧各 3 根白色竖条随音量随机跳动。窗口从 120×120 缩至 120×34，画法从三角函数弧线简化为矩形填充，代码量反而减少

3. **HTML 模板外置**：用 `string.Template`（stdlib）替代 placeholder 字符串替换，模板文件通过 `importlib.resources` 加载并缓存。讨论后决定不引入 Jinja2——只有 2 处 if 逻辑（`IS_MACOS` 和 `__commit__`），在 Python 侧预计算后传入模板即可

### 讨论中排除的方案

本轮在正式动手前有过较长的方向讨论：

- **Tkinter 跨平台统一**：Tk 必须占主线程，但 macOS 主线程被 pystray（AppKit NSApp.run）占死、Linux 被 shutdown_event 占死。两个平台都没空位给 Tk mainloop
- **子进程方案**：overlay 作为独立子进程跑 Tk mainloop，父进程通过 stdin JSON Lines 驱动。技术上可行，但用户担心退出清理问题（虽然有 stdin EOF 自动检测等三层防御）
- **Tauri 全面重写**：评估了 3466 行全部用 Rust+Tauri 重写的方案，结论是 4-6 周工期、完全推翻 PyPI 分发路线、STT 翻 Rust 风险大，ROI 不合理
- **Jinja2 模板**：只有 2 处 if 分支，不值得加依赖

最终选择最务实的路径：保留各平台原生工具链，只统一视觉 + 整理架构。

### 开发内容概括

| 文件 | 操作 | 说明 |
|---|---|---|
| `backends/tray_linux.py` | 新增 | Linux tray 后端（~100 行） |
| `backends/tray_macos.py` | 新增 | macOS tray 后端（~130 行） |
| `tray.py` | 新增 | 平台 dispatcher（~10 行） |
| `__main__.py` | 改 | 删除 `run_tray()`（-170 行），改为 import dispatcher |
| `backends/overlay_linux.py` | 重写 | 深蓝药丸 + 跳动长条（190→173 行） |
| `backends/overlay_macos.py` | 重写 | 同上视觉（188→209 行） |
| `assets/settings.html` | 新增 | 设置页 HTML 模板（`string.Template` 语法） |
| `settings_server.py` | 改 | 删除 ~460 行内联 HTML，改为加载模板 + `substitute()` |
| `tests/test_dispatchers.py` | 改 | 新增 tray dispatcher smoke test |
| `BACKLOG.md` | 改 | 更新"跨平台 Pythonic overlay"条目 |

### 额外产物

- `docs/16-架构整理与浮窗视觉统一/PROMPT.md` — 需求文档
- `docs/16-架构整理与浮窗视觉统一/PLAN.md` — 实现计划

## 局限性

1. **overlay 仍是双份代码**：Linux 用 GTK3+Cairo，macOS 用 AppKit，视觉一致但代码不统一。真正的单一代码路径需要更大架构变更（子进程/Tauri），不在本轮范围
2. **Linux overlay 无法做真正的圆角药丸**：GTK3 + RGBA visual 需要 compositor 支持才能透出窗口角落。有 compositor 时药丸圆角正常；无 compositor（如纯 X11 无桌面特效）时是矩形蓝块。macOS 无此问题
3. **tray 共享代码复制了两份**：`_draw_mic` 和 `_create_icon` 在两个后端文件中各一份（~50 行），如果将来改图标样式需要改两处

## 后续 TODO

- overlay 的长条跳动节奏和幅度可能需要根据实际使用体验微调（`_BAR_REST_H`、`_BAR_MAX_H`、`random.uniform` 的范围参数）
- BACKLOG 中已标注：代码统一（Tkinter/单文件）留到后续，最可行方向是 Tauri 全面接管 UI 层
- `settings.html` 现在作为 package data 打进 wheel，编辑后需要重新 `uv sync` 才能生效（dev 模式下 editable install 会自动同步，不影响日常开发）
