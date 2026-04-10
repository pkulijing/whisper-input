# macOS 平台支持 - 实现计划

## Context

Whisper Input 当前是 Linux 专用工具，依赖 evdev、xdotool、xclip、paplay 等 Linux/X11 组件。用户希望在 macOS 上使用，因为 macOS 上现有语音输入方案效果不佳。项目约 70% 代码已经是跨平台的（sounddevice、FunASR、pystray、HTTP settings server），需要改造的主要是热键监听、文字输入、声音播放、配置路径和自启动机制。

## 架构设计

### 平台抽象方案

创建 `backends/` 包，按平台拆分实现。顶层模块（`hotkey.py`、`input_method.py`）变为轻量调度器，运行时按 `sys.platform` 选择后端。

```
backends/
  __init__.py            # IS_LINUX, IS_MACOS 常量
  hotkey_linux.py        # 现有 evdev 实现（从 hotkey.py 搬入）
  hotkey_macos.py        # 新：pynput 实现
  input_linux.py         # 现有 xclip/xdotool 实现（从 input_method.py 搬入）
  input_macos.py         # 新：pbcopy/pbpaste + pynput 实现
  autostart_linux.py     # 从 settings_server.py 提取的 .desktop 逻辑
  autostart_macos.py     # 新：LaunchAgents plist
```

声音播放很简单（paplay vs afplay），直接在 `main.py` 里 inline 判断，不单独建文件。

### 各模块公共 API 契约

- **HotkeyListener**：`__init__(hotkey, on_press, on_release)` / `start()` / `stop()`
- **SUPPORTED_KEYS**：`dict[str, Any]`，key 是字符串标识符，value 是平台内部按键对象
- **type_text(text, method)**：签名不变，macOS 忽略 method 参数（只用 clipboard）
- **autostart**：`is_autostart_enabled() -> bool` / `set_autostart(enabled: bool) -> None`

## 分步实现计划

### Step 1: 创建 backends/ 包和平台检测

新建 `backends/__init__.py`，导出 `IS_LINUX`、`IS_MACOS` 常量。

### Step 2: 提取 Linux 实现到 backends/

- `backends/hotkey_linux.py`：`hotkey.py` 全部内容搬入，零逻辑修改
- `backends/input_linux.py`：`input_method.py` 全部内容搬入，零逻辑修改
- `backends/autostart_linux.py`：从 `settings_server.py` 提取自启动相关代码

### Step 3: 将顶层模块改为调度器

`hotkey.py` 和 `input_method.py` 各约 5 行，按 `sys.platform` 从对应后端导入。此时 Linux 功能应完全不受影响。

### Step 4: 实现 macOS 热键监听（backends/hotkey_macos.py）

- 使用 `pynput.keyboard.Listener` 全局监听
- SUPPORTED_KEYS 增加 Fn/Globe 键和 Command 键，保留与 Linux 兼容的 key 标识符
- 保留 300ms 组合键延迟逻辑
- 启动时捕获异常并引导用户授予辅助功能权限

### Step 5: 实现 macOS 文字输入（backends/input_macos.py）

- 使用 macOS 原生 `pbcopy`/`pbpaste` 处理剪贴板（无额外依赖）
- 使用 `pynput.keyboard.Controller` 模拟 Cmd+V
- 流程：pbpaste 保存 → pbcopy 写入 → Cmd+V → 恢复

### Step 6: 实现 macOS 自启动（backends/autostart_macos.py）

- 写入/删除 `~/Library/LaunchAgents/com.whisper-input.plist`
- ProgramArguments 使用 `sys.executable` + `main.py` 路径

### Step 7: 修改 settings_server.py

- SUPPORTED_KEYS 按平台切换
- autostart 委托给 backends
- HTML 设备选项动态注入（macOS 加 MPS，去 CUDA）
- 输入方式：macOS 隐藏 xdotool 选项

### Step 8: 修改 config_manager.py

- 配置目录：macOS → `~/Library/Application Support/Whisper Input/`
- DEFAULT_CONFIG 按平台：hotkey 默认 `KEY_FN`、device 默认 `mps`、sound 路径用系统声音

### Step 9: 修改 main.py

- GI_TYPELIB_PATH 包在 Linux 守卫中
- play_sound：macOS 用 `afplay`
- banner 去掉 "Linux"

### Step 10: 修改 stt_sensevoice.py

- 设备可用性检测：cuda/mps 不可用时回退 cpu

### Step 11: 修改 pyproject.toml

- 依赖用环境标记：`evdev; sys_platform == 'linux'`、`pynput; sys_platform == 'darwin'`
- PyTorch 通过 dependency-groups 区分 linux-cuda 和 macos

### Step 12: 创建 setup_macos.sh

- 检查 portaudio、uv
- `uv sync --group macos`
- 打印权限提示

### Step 13: 更新文档

- CLAUDE.md、README.md 增加 macOS 说明

## 关键文件清单

| 文件 | 操作 |
|------|------|
| `backends/__init__.py` | 新建 |
| `backends/hotkey_linux.py` | 新建（从 hotkey.py 提取） |
| `backends/hotkey_macos.py` | 新建 |
| `backends/input_linux.py` | 新建（从 input_method.py 提取） |
| `backends/input_macos.py` | 新建 |
| `backends/autostart_linux.py` | 新建（从 settings_server.py 提取） |
| `backends/autostart_macos.py` | 新建 |
| `hotkey.py` | 改为调度器 |
| `input_method.py` | 改为调度器 |
| `main.py` | 修改 |
| `config_manager.py` | 修改 |
| `settings_server.py` | 修改 |
| `stt_sensevoice.py` | 修改 |
| `pyproject.toml` | 修改 |
| `setup_macos.sh` | 新建 |

## 验证方案

1. **Ruff 检查**：`uv run ruff check .` 通过
2. **macOS 导入测试**：`python -c "from hotkey import HotkeyListener, SUPPORTED_KEYS; print(SUPPORTED_KEYS)"`
3. **macOS 热键测试**：验证按键事件回调和组合键延迟
4. **macOS 输入测试**：`type_text("测试中文输入")` 验证文字输出
5. **Settings UI**：验证热键选项、设备选项、自启动开关
6. **完整流程**：按住热键说话，松开后验证文字输入
