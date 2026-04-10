# macOS 平台支持

## 背景

Whisper Input 目前是一个 Linux 专用的桌面语音输入工具，依赖 evdev、xdotool、xclip 等 Linux/X11 特有组件。用户发现 macOS 上微信等输入法的语音转文字效果不佳，希望将 Whisper Input 移植到 macOS 平台，利用 SenseVoice 本地模型提供更好的语音识别体验。

## 需求描述

将 Whisper Input 扩展为跨平台应用，支持 macOS（同时保持 Linux 兼容性）。

### 核心要求

1. **自动平台识别**：开发、安装、使用的所有环节都要自动识别当前平台（Linux/macOS），用户无需手动配置
2. **功能对等**：macOS 版本应具备与 Linux 版本相同的核心功能：
   - 长按热键录音，松开后自动转文字并输入到当前焦点窗口
   - 系统托盘图标和菜单
   - Web UI 设置界面
   - 开机自启配置
3. **平台抽象层**：通过运行时平台检测，自动选择对应的实现模块，而非维护两套独立代码

### 需要改造的模块

| 模块 | 改造内容 |
|------|---------|
| hotkey.py | evdev → pynput/Quartz，创建平台抽象层 |
| input_method.py | xclip+xdotool → pynput+pyperclip（Cmd+V） |
| main.py | paplay → afplay，去除 PyGObject 硬依赖 |
| config_manager.py | 路径适配 ~/Library/Application Support/ |
| settings_server.py | 自启动改 LaunchAgent，热键标签适配 |
| stt_sensevoice.py | 设备默认值适配（CPU/MPS） |
| pyproject.toml | 依赖管理跨平台化 |

### macOS 特殊注意事项

- 麦克风权限和辅助功能权限的处理
- Apple Silicon MPS 加速支持
- macOS 应用打包（.app bundle 或 Homebrew）
