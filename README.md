# Whisper Input

跨平台语音输入工具 —— 按住快捷键说话，松开后自动将识别结果输入到当前焦点窗口。

使用本地 [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) 模型，无需联网，支持中英日韩粤语混合识别。

支持 **Linux (X11)** 和 **macOS**。

## 功能特性

- 本地语音识别，离线可用
- 中英文等多语种混合输入
- 可配置快捷键（支持区分左右修饰键）
- 浏览器设置界面 + 系统托盘
- 支持开机自启动
- 自动识别平台，选择对应后端

## 系统要求

### Linux
- Ubuntu / Debian（X11 桌面环境）
- Python 3.12+
- NVIDIA GPU（推荐，CPU 也可运行）
- [uv](https://docs.astral.sh/uv/) 包管理器

### macOS
- macOS 12+ (Monterey 或更高)
- Python 3.12+
- Apple Silicon（推荐，MPS 加速）或 Intel Mac（CPU 推理）
- [Homebrew](https://brew.sh) + [uv](https://docs.astral.sh/uv/)

## 快速开始

### macOS

```bash
git clone <repo-url>
cd whisper-input
bash setup_macos.sh
uv run python main.py
```

首次运行需要在「系统设置 > 隐私与安全性」中授予：
1. **辅助功能**权限（热键监听和文字输入）—— 添加你使用的终端应用
2. **麦克风**权限 —— 首次录音时系统会弹出授权对话框

### Linux

```bash
git clone <repo-url>
cd whisper-input
bash setup.sh
```

`setup.sh` 会自动检查并安装系统依赖（xdotool、xclip、libportaudio2 等），将当前用户加入 `input` 组，并通过 `uv sync` 安装 Python 依赖。

#### DEB 安装包

```bash
bash build_deb.sh
sudo dpkg -i build/deb/whisper-input_0.1.0.deb
sudo apt-get -f install  # 补全系统依赖
```

### 运行

```bash
uv run python main.py

# 指定快捷键
uv run python main.py -k KEY_FN          # macOS: Fn/Globe 键
uv run python main.py -k KEY_RIGHTALT    # Linux: 右 Alt 键

# 更多选项
uv run python main.py --help
```

启动后会自动打开浏览器设置页面，也可通过系统托盘图标访问。

## 使用方法

1. 启动程序后，按住快捷键开始录音
   - macOS 默认：Fn (Globe) 键
   - Linux 默认：右 Ctrl 键
2. 对着麦克风说话
3. 松开快捷键，等待识别完成
4. 识别结果自动输入到当前光标位置

## 配置

配置文件 `config.yaml`，也可通过浏览器设置界面修改：

| 配置项 | 说明 | macOS 默认 | Linux 默认 |
|--------|------|-----------|-----------|
| `hotkey` | 触发快捷键 | `KEY_FN` | `KEY_RIGHTCTRL` |
| `sensevoice.device` | 推理设备 | `mps` | `cuda` |
| `sensevoice.language` | 识别语种 | `auto` | `auto` |
| `input_method` | 输入方式 | `clipboard` | `clipboard` |
| `sound.enabled` | 录音提示音 | `true` | `true` |

## 已知限制

- Linux 仅支持 X11，暂不支持 Wayland
- Super/Win 键在 GNOME 下会被桌面拦截，不建议使用
- macOS 需要辅助功能权限才能监听全局热键
- 首次运行需下载 SenseVoice 模型（约 500MB）

## 技术架构

```
按住快捷键 → HotkeyListener (backends/) → AudioRecorder (sounddevice)
松开快捷键 → SenseVoiceSTT (FunASR)     → InputMethod (backends/)
                                          → 文本输入到焦点窗口
```

平台后端（`backends/`）运行时按 `sys.platform` 自动选择：
- **Linux**: evdev 读键盘事件 + xclip/xdotool 剪贴板粘贴
- **macOS**: pynput 全局键盘监听 + pbcopy/pbpaste + Cmd+V 粘贴

共同特性：
- 修饰键按下后有 300ms 延迟，用于区分组合键（如 Ctrl+C）和单独触发
- 剪贴板粘贴而非模拟按键，避免中文输入乱码
- 设备不可用时自动回退（cuda/mps → cpu）

## License

MIT
