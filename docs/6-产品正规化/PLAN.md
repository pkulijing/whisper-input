# 实现计划：产品正规化

## 开发内容概览

1. 版本号统一管理模块
2. 托盘图标状态变化（灰/绿/红/橙）
3. 录音浮窗（macOS PyObjC + Linux GTK）
4. 托盘菜单增加版本号
5. 设置页面增加版本号 + GitHub 链接
6. 设置页面增加浮窗/图标状态开关

## 详细设计

### 1. 版本号统一管理

新建 `version.py`，优先从 `importlib.metadata` 读取（已安装时），回退到解析 `pyproject.toml`（开发模式）。提供 `__version__` 变量供其他模块使用。

### 2. 托盘图标状态变化

在 `main.py` 扩展现有 `create_icon()` 函数，增加橙色。给 `WhisperInput` 添加 `set_status_callback`，在各关键节点触发状态变更：

| 事件 | 状态 | 图标颜色 |
|------|------|---------|
| 启动/加载模型 | loading | 灰色 |
| 模型加载完成 | ready | 绿色 |
| 按下热键 | recording | 红色 |
| 松开热键 | processing | 橙色 |
| 识别完成 | ready | 绿色 |

回调同时更新图标和 tooltip 文字。

### 3. 录音浮窗

**架构**：`overlay.py`（调度器）+ 平台实现，与 `hotkey.py` 模式一致。

**接口**：
```python
class RecordingOverlay:
    def show(self) -> None       # 显示浮窗（录音开始）
    def update(self, text) -> None  # 更新状态文字
    def hide(self) -> None       # 隐藏浮窗
```

**macOS** (`backends/overlay_macos.py`)：
- 使用 PyObjC 创建 NSWindow（pynput 已间接依赖 pyobjc）
- 浮动窗口层级，无标题栏，半透明圆角背景
- 屏幕居中偏下

**Linux** (`backends/overlay_linux.py`)：
- 使用 GTK3（pygobject 已是依赖）
- `set_keep_above(True)`，无装饰，RGBA 半透明

**集成**：在 `WhisperInput` 中通过状态回调统一驱动浮窗显示/隐藏。

### 4. 设置项

在 `config_manager.py` 的 `DEFAULT_CONFIG` 中新增：
- `overlay.enabled`: `true`
- `tray_status.enabled`: `true`

设置页面"高级设置"卡片中增加两个开关，支持热更新。

### 5. 产品信息

- **托盘菜单**：顶部添加 `Whisper Input v{version}`（不可点击）
- **设置页面**：底部 footer 显示版本号 + GitHub 链接

## 文件修改清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `version.py` | 新建 | 版本号读取 |
| `overlay.py` | 新建 | 浮窗调度器 |
| `backends/overlay_macos.py` | 新建 | macOS 浮窗 |
| `backends/overlay_linux.py` | 新建 | Linux 浮窗 |
| `main.py` | 修改 | 状态回调、版本号菜单、浮窗集成 |
| `config_manager.py` | 修改 | 新增配置项 |
| `settings_server.py` | 修改 | 开关、版本号、GitHub 链接 |
| `build.sh` | 修改 | 文件列表更新 |

## 开发顺序

1. `version.py`
2. 托盘菜单版本号 + 设置页面版本号/GitHub 链接
3. 托盘图标状态变化
4. 录音浮窗
5. 设置项开关
6. 更新 build.sh
