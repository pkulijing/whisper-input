# 需求：架构整理 + overlay 视觉统一

## 背景

1. `__main__.py` 里的 `run_tray()` 函数有 170 行平台分支代码，是唯一没拆进 `backends/` 的平台相关模块
2. 两个平台的 overlay 视觉不一致（Linux 矢量麦克风 vs macOS emoji 麦克风），风格也偏旧（大方块 120×120 + 波纹弧线）
3. `settings_server.py` 内嵌了 ~460 行 HTML 模板字符串，用 placeholder 字符串替换做模板渲染，不够规范

## 需求

### 1. tray 拆到 backends

将 `run_tray()` 从 `__main__.py` 拆分到 `backends/tray_linux.py` + `backends/tray_macos.py`，通过 `tray.py` dispatcher 导出，和 hotkey / input_method / overlay 保持一致的架构模式。

### 2. overlay 视觉统一

参考微信输入法的小浮窗风格，两个平台统一改为：

- 窗口尺寸 120×34 的小药丸（原 120×120 大方块）
- 深蓝 `#1E3A8A` 纯色背景（原半透明黑）
- 居中白色麦克风 emoji
- 两侧各 3 根白色竖向长条，随音量随机跳动产生波纹感（原 3 环弧形波纹）
- 保留各平台原生工具链（Linux GTK3+Cairo，macOS AppKit），只改绘制逻辑

### 3. 设置页 HTML 模板外置

- 将 `SETTINGS_HTML` 内联字符串抽取为 `assets/settings.html`
- 使用 `string.Template`（stdlib）做模板替换，通过 `importlib.resources` 加载
- 8 个 placeholder 变为 `$variable` 语法

## 不做的事

- 不做 Tkinter 迁移（讨论后发现 Tk 无法与 pystray 共享主线程）
- 不引入 Jinja2（只有 2 处 if 逻辑，不值得加依赖）
- 不改依赖（pygobject、pyobjc-framework-cocoa 都还需要）
