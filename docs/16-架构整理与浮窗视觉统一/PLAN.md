# 实现计划：架构整理 + overlay 视觉统一

## 任务 1：tray 拆到 backends

### 新文件

- `src/whisper_input/backends/tray_linux.py` — Linux tray：ASCII 英文 status tips、品牌色图标、daemon 线程运行
- `src/whisper_input/backends/tray_macos.py` — macOS tray：中文 tips、模板图、Retina 补丁、返回 icon 供主线程 .run()
- `src/whisper_input/tray.py` — 平台 dispatcher

### 修改

- `src/whisper_input/__main__.py` — 删除 `run_tray()` 定义（~170 行），改为 try/except import dispatcher

### 设计决策

- `_draw_mic` / `create_icon` 共享代码各复制一份，不抽 `_tray_common.py`（两平台行为已不同）
- 返回值约定不变：macOS 返回 icon，Linux 返回 None

## 任务 2：overlay 视觉统一

### 新视觉

- 窗口 120×34，深蓝 `#1E3A8A` 圆角药丸（r=17）
- 居中白色 🎙 emoji（15pt）
- 每侧 3 根白色竖条（3px 宽，5px 间距），高度 4-15px
- `set_level()` 中计算 bar_heights（random jitter），redraw 直接读取

### 改写

- `overlay_linux.py`：GTK3+Cairo，删掉 `_draw_mic()` 矢量函数，换成药丸 + emoji + rectangles
- `overlay_macos.py`：AppKit，`_OverlayView.drawRect_` 改画药丸 + emoji + rectangles

## 任务 3：设置页 HTML 模板外置

- 新增 `src/whisper_input/assets/settings.html`（`string.Template` 语法，8 个 `$variable`）
- `settings_server.py`：删除 ~460 行 `SETTINGS_HTML` 字符串，用 `importlib.resources` + `Template.substitute()` 渲染
- 首次加载后缓存 Template 对象，避免重复 IO

## 清理

- `tests/test_dispatchers.py` 加 tray smoke test
- `BACKLOG.md` 更新"跨平台 Pythonic overlay"条目
