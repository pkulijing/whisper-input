[English](README.md) | **中文**

# Whisper Input

[![Build](https://github.com/pkulijing/whisper-input/actions/workflows/build.yml/badge.svg)](https://github.com/pkulijing/whisper-input/actions/workflows/build.yml)
[![codecov](https://codecov.io/gh/pkulijing/whisper-input/branch/master/graph/badge.svg)](https://codecov.io/gh/pkulijing/whisper-input)
[![PyPI](https://img.shields.io/pypi/v/whisper-input.svg)](https://pypi.org/project/whisper-input/)

跨平台语音输入工具 —— 按住快捷键说话，松开后自动将识别结果输入到当前焦点窗口。

使用达摩院官方 [SenseVoice-Small ONNX 量化版](https://www.modelscope.cn/models/iic/SenseVoiceSmall-onnx)（通过 Microsoft `onnxruntime` 直接推理），本地离线可用，支持中英日韩粤语混合识别，**自带标点 / 反向文本规范化 / 大小写**。模型首次启动从 ModelScope 国内 CDN 拉取（~231 MB），之后永久离线。

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
- **Ubuntu 24.04+ / Debian 13+**（X11 桌面环境，较老发行版因缺少 `libgirepository-2.0-dev` 无法安装）
- 任意 x86_64 CPU（推理用 `onnxruntime` CPU，RTF ≈ 0.1，短句识别延迟 < 1 秒）

### macOS
- macOS 12+ (Monterey 或更高)
- Apple Silicon（推荐）或 Intel Mac 均可，都走 CPU ONNX 推理

## 安装

### 一键安装（推荐）

在 macOS 或 Linux 上执行：

```bash
curl -LsSf https://raw.githubusercontent.com/pkulijing/whisper-input/master/install.sh | sh
```

脚本会交互式选择语言，然后自动装好 `uv` / Python 3.12 / 系统依赖 / `whisper-input` 本身，跑 `whisper-input --init`（预下载约 231 MB 的 SenseVoice ONNX 模型，macOS 下同时安装 `~/Applications/Whisper Input.app`），最后询问是否立即启动。重复执行安全，已装好的步骤会自动跳过，`uv tool install --upgrade` 会把 `whisper-input` 升级到最新。

Linux 首次运行还会引导把当前用户加入 `input` 组（需 `sudo`，需注销重新登录后生效）。

> **注意**：`curl | sh` 模式需要你信任脚本来源（本仓库）。可以先 `curl -LsSf <上述 URL> -o install.sh` 下载到本地看一眼再执行。

### 手动安装

#### macOS

```bash
# 装系统依赖
brew install portaudio

# 装工具本体（--compile-bytecode 跳过首次运行时的 .pyc 编译，启动更快）
uv tool install --compile-bytecode whisper-input

# 一次性初始化：安装 .app bundle + 下载 STT 模型（约 231 MB）
whisper-input --init

# 运行
whisper-input
```

**首次运行需要在「系统设置 > 隐私与安全性」中授予权限：**

1. **辅助功能**（全局热键监听和文字输入）
2. **麦克风**（语音录制，首次录音时系统会弹出授权对话框）

> **注意**：首次运行（或执行 `whisper-input --init`）时，工具会在 `~/Applications/Whisper Input.app` 安装一个极简 `.app` bundle。macOS 系统权限对话框和系统设置里显示的都是 "Whisper Input"，直接给这个条目授权即可。完整卸载请先运行 `whisper-input --uninstall`，再执行 `uv tool uninstall whisper-input`。

#### Linux

```bash
# 装系统依赖（各包用途见下方表格）
sudo apt install xdotool xclip pulseaudio-utils libportaudio2 \
                 libgirepository-2.0-dev libcairo2-dev gir1.2-gtk-3.0 \
                 gir1.2-ayatanaappindicator3-0.1

# 把自己加进 input 组(evdev 读 /dev/input/* 需要)
sudo usermod -aG input $USER && newgrp input

# 装工具本体（--compile-bytecode 跳过首次运行时的 .pyc 编译，启动更快）
uv tool install --compile-bytecode whisper-input

# 一次性初始化：下载 STT 模型（约 231 MB）
whisper-input --init

# 运行
whisper-input
```

**系统依赖说明：**

| 包名 | 项目功能 | 说明 |
|------|----------|------|
| `xdotool`、`xclip` | 文字输入 | xclip 读写 X11 剪贴板，xdotool 模拟 Shift+Insert 触发粘贴 |
| `libportaudio2` | 语音录制 | PortAudio 音频库，Python 包 `sounddevice` 的运行时依赖 |
| `pulseaudio-utils` | 提示音 | 提供 `paplay` 命令，播放录音开始/结束提示音 |
| `libgirepository-2.0-dev`、`libcairo2-dev` | 编译依赖 | `pygobject`（Python 的 GTK 绑定，录音浮窗用）和 `pycairo`（pygobject 的底层依赖）编译 C 扩展时需要的头文件，安装完成后不再使用 |
| `gir1.2-gtk-3.0` | 录音浮窗 | GTK 3 类型库，`pygobject` 通过它调用 GTK 绘制录音状态浮窗 |
| `gir1.2-ayatanaappindicator3-0.1` | 系统托盘图标 | AppIndicator 类型库，Python 包 `pystray` 在 Linux 上绘制托盘图标的运行时依赖 |

首次运行 `whisper-input` 会通过 `modelscope.snapshot_download` 自动从达摩院 ModelScope CDN 拉取 SenseVoice ONNX 模型（~231 MB），缓存到 `~/.cache/modelscope/hub/`。一次成功后永久离线。

#### 从源码安装（贡献者）

```bash
git clone https://github.com/pkulijing/whisper-input
cd whisper-input
bash scripts/setup.sh
uv run whisper-input
```

## 运行选项

```bash
# 指定快捷键
whisper-input -k KEY_FN          # macOS: Fn/Globe 键
whisper-input -k KEY_RIGHTALT    # Linux: 右 Alt 键

# 更多选项
whisper-input --help
```

启动后会自动打开浏览器设置页面，也可通过系统托盘图标访问。

## 发版流程（维护者）

PyPI 分发走 GitHub Actions tag 触发 + Trusted Publishing (OIDC)：

1. 在 `pyproject.toml` 中 bump `version` 字段
2. `git commit -am "release: v0.5.1"` 并 push 到 master
3. `git tag v0.5.1 && git push --tags`
4. [`.github/workflows/release.yml`](.github/workflows/release.yml) 自动触发：校验 tag 和 version 一致 → `uv build` → `pypa/gh-action-pypi-publish` 发到 PyPI → 创建 GitHub Release

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
| `hotkey` | 触发快捷键 | `KEY_RIGHTMETA` | `KEY_RIGHTCTRL` |
| `sensevoice.language` | 识别语种 | `auto` | `auto` |
| `sensevoice.use_itn` | 反向文本规范化 | `true` | `true` |
| `input_method` | 输入方式 | `clipboard` | `clipboard` |
| `sound.enabled` | 录音提示音 | `true` | `true` |

## 已知限制

- Linux 仅支持 X11，暂不支持 Wayland
- Super/Win 键在 GNOME 下会被桌面拦截，不建议使用
- macOS 需要辅助功能权限才能监听全局热键
- 首次运行需下载 SenseVoice ONNX 模型（约 231MB，从达摩院 ModelScope 官方仓库直连）

## 技术架构

整个项目采用 src layout,所有 Python 代码在 `src/whisper_input/` 下,是一个
可 `uv sync` 编辑式安装的真 package。入口点是 console script
`whisper-input`(等价于 `uv run python -m whisper_input`)。

```
按住快捷键 → HotkeyListener (whisper_input.backends) → AudioRecorder (sounddevice)
松开快捷键 → stt.SenseVoiceSTT (onnxruntime) → InputMethod → 文本输入到焦点窗口
```

平台后端（`whisper_input.backends`）运行时按 `sys.platform` 自动选择：
- **Linux**: evdev 读键盘事件 + xclip/xdotool 剪贴板粘贴
- **macOS**: pynput 全局键盘监听 + pbcopy/pbpaste + Cmd+V 粘贴

STT 推理层（`whisper_input.stt`）：
- 模型：达摩院官方 `iic/SenseVoiceSmall-onnx`（量化版），通过 `modelscope.snapshot_download` 从 ModelScope 国内 CDN 下载，缓存到 `~/.cache/modelscope/hub/`
- 运行时：Microsoft 官方 `onnxruntime`，不依赖 torch
- 特征提取、BPE 解码、meta 标签后处理：从达摩院官方 `funasr_onnx` 包移植（MIT 协议，~250 行纯 Python），和 FunASR 位对齐
- 依赖树：`onnxruntime + kaldi-native-fbank + sentencepiece + numpy + modelscope`（modelscope base 仅 36 MB，不含 torch/transformers）

共同特性：
- 修饰键按下后有 300ms 延迟，用于区分组合键（如 Ctrl+C）和单独触发
- 剪贴板粘贴而非模拟按键，避免中文输入乱码
- 统一 CPU 推理路径，macOS/Linux 代码零差异

## License

MIT
