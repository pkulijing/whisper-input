# Whisper Input

[![Build](https://github.com/pkulijing/whisper-input/actions/workflows/build.yml/badge.svg)](https://github.com/pkulijing/whisper-input/actions/workflows/build.yml)

跨平台语音输入工具 —— 按住快捷键说话，松开后自动将识别结果输入到当前焦点窗口。

使用达摩院官方 [SenseVoice-Small ONNX 量化版](https://www.modelscope.cn/models/iic/SenseVoiceSmall-onnx)（通过 Microsoft `onnxruntime` 直接推理），无需联网，支持中英日韩粤语混合识别，**自带标点 / 反向文本规范化 / 大小写**。**首次安装依赖只需几十秒**（不再需要下载 torch），模型从 ModelScope 国内 CDN 直连拉取。

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
- 任意 x86_64 CPU 即可（推理用 `onnxruntime` CPU，RTF ≈ 0.1，短句识别延迟 < 1 秒）
- **[uv](https://docs.astral.sh/uv/) 包管理器（必须预装）**：

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

  Python 运行时由 uv 管理（python-build-standalone），无需依赖系统 `python3`。

### macOS
- macOS 12+ (Monterey 或更高)
- Apple Silicon（推荐）或 Intel Mac 均可，都走 CPU ONNX 推理
- [Homebrew](https://brew.sh) + [uv](https://docs.astral.sh/uv/)

## 下载安装包

从 [Releases](https://github.com/pkulijing/whisper-input/releases) 页面下载最新版本：

- **macOS (Apple Silicon)**：`WhisperInput_<version>.dmg`
- **Linux (x86_64, Ubuntu 24.04+ / Debian 13+)**：`whisper-input_<version>.deb`

每次 push 到 master 的构建产物也会上传到 [Actions](https://github.com/pkulijing/whisper-input/actions) 页面的 Artifacts（保留 30 天，需登录 GitHub 下载）。

## 发版流程（维护者）

1. 在 `pyproject.toml` 中 bump `version` 字段
2. commit + push 到 master
3. CI 自动构建 → 打 `v<version>` tag → 创建 GitHub Release 并上传 `.dmg` / `.deb`

若 push 时源码有改动但忘了 bump 版本号，CI 会在 Actions 页面给出 warning 提醒。

调试 CI 本身：在 `ci-bootstrap` 分支上修改 workflow 并 push，CI 会构建但不发 release。

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
bash setup_linux.sh
```

`setup_linux.sh` 会自动检查并安装系统依赖（xdotool、xclip、libportaudio2 等），将当前用户加入 `input` 组，然后用 `uv sync` 安装 Python 依赖（~20 MB，全部走清华源，国内几十秒）。

#### DEB 安装包

```bash
bash build.sh
sudo apt install ./build/deb/whisper-input_<version>.deb
```

`apt install` 本身秒级完成（只做文件复制、加入 `input` 组、刷图标缓存）。
**首次启动**时会弹出一个初始化窗口，依次完成：

1. Python 运行环境（由 uv 拉取 python-build-standalone，约 30MB）
2. Python 依赖（`onnxruntime` + `kaldi-native-fbank` + `sentencepiece` + `numpy` 等，约 25MB）
3. SenseVoice ONNX 模型下载（约 231MB，5 个文件，从达摩院官方 ModelScope CDN 直连）
4. 模型加载到内存

全程约 1-2 分钟，首次之后每次启动只走第 4 步。请确保已预装 uv。

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

```
按住快捷键 → HotkeyListener (backends/) → AudioRecorder (sounddevice)
松开快捷键 → stt.SenseVoiceSTT (onnxruntime) → InputMethod (backends/)
                                               → 文本输入到焦点窗口
```

平台后端（`backends/`）运行时按 `sys.platform` 自动选择：
- **Linux**: evdev 读键盘事件 + xclip/xdotool 剪贴板粘贴
- **macOS**: pynput 全局键盘监听 + pbcopy/pbpaste + Cmd+V 粘贴

STT 推理层（`stt/`）：
- 模型：达摩院官方 `iic/SenseVoiceSmall-onnx`（量化版），从 ModelScope 国内 CDN 下载
- 运行时：Microsoft 官方 `onnxruntime`，不依赖 torch
- 特征提取、BPE 解码、meta 标签后处理：从达摩院官方 `funasr_onnx` 包移植（MIT 协议，~250 行纯 Python），和 FunASR 位对齐
- 依赖树只有 `onnxruntime + kaldi-native-fbank + sentencepiece + numpy`，保持干净

共同特性：
- 修饰键按下后有 300ms 延迟，用于区分组合键（如 Ctrl+C）和单独触发
- 剪贴板粘贴而非模拟按键，避免中文输入乱码
- 统一 CPU 推理路径，macOS/Linux 代码零差异

## License

MIT
