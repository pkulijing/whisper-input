# 开发总结：产品正规化

## 开发项背景

Whisper Input 功能已经成熟，但缺乏视觉状态反馈和基本的产品信息展示，用户体验不够正规：启动加载模型时无提示，录音过程中无法确认软件是否在工作，托盘菜单缺少版本号等信息。

## 实现方案

### 关键设计

1. **状态驱动的 UI 更新**：在 `WhisperInput` 中引入 `_notify_status(status)` 统一回调机制，一个状态变更同时驱动托盘图标和录音浮窗，避免分散的 UI 调用。

2. **平台浮窗抽象**：沿用 `backends/` 的运行时调度模式，`overlay.py` 作为调度器，macOS 使用 PyObjC（pynput 已间接依赖），Linux 使用 GTK3（pygobject 已是依赖），无需新增任何依赖。

3. **实时音量波纹**：`AudioRecorder` 在 `_audio_callback` 中实时计算 RMS 音量并通过 `on_level` 回调通知浮窗。浮窗根据音量级别动态绘制弧形波纹——无声音时仅显示静态 emoji 麦克风图标，有声音时两侧出现 3 层波纹，透明度随音量衰减。

4. **macOS 主线程调度**：使用 `NSObject.performSelectorOnMainThread` 替代 `AppHelper.callAfter`，与 pystray 的 AppKit 运行循环兼容。

5. **版本号单一数据源**：`version.py` 优先从 `importlib.metadata` 读取（已安装时），回退到解析 `pyproject.toml`（开发模式），确保版本号始终从 `pyproject.toml` 这一个地方管理。

### 开发内容概括

| 模块 | 变更 |
|------|------|
| `version.py` | 新建，统一版本号读取 |
| `overlay.py` | 新建，浮窗调度器 |
| `backends/overlay_macos.py` | 新建，PyObjC 浮窗：emoji 麦克风 + 动态波纹 |
| `backends/overlay_linux.py` | 新建，GTK3 浮窗：emoji 麦克风 + 动态波纹 |
| `recorder.py` | 新增 `on_level` 实时音量回调 |
| `main.py` | 状态回调、托盘图标四色状态、版本号菜单项、浮窗集成、音量回调连接 |
| `config_manager.py` | 新增 `overlay.enabled` 和 `tray_status.enabled` 配置项 |
| `settings_server.py` | 浮窗/图标状态开关、页面底部版本号和 GitHub 链接 |
| `build.sh` | 文件列表更新 |

### 额外产物

- `docs/6-产品正规化/PROMPT.md` — 需求文档
- `docs/6-产品正规化/PLAN.md` — 实现计划

## 局限性

- **macOS 主线程时序**：PyObjC 的 UI 操作通过 `performSelectorOnMainThread` 调度，在 pystray 的 AppKit 运行循环尚未完全初始化时可能有时序问题。
- **Linux Wayland 兼容性**：GTK POPUP 窗口在部分 Wayland 合成器上可能无法正确定位或置顶。
- **托盘图标更新频率**：pystray 更新图标需重新生成 PIL Image，频繁状态切换可能有微小延迟。
- **VS Code 扩展 hooks 不生效**：项目配置了 PostToolUse hook 自动构建，但 VS Code 版 Claude Code 存在已知的 hooks 触发问题。

## 后续 TODO

- 打包分发优化：PyInstaller 打包，模型首次使用时下载
- Wayland 浮窗兼容性测试和适配
- 考虑使用系统通知（macOS Notification Center / libnotify）作为浮窗的轻量替代方案
