**中文** | [English](README.en.md)

# 叨逼叨 (Daobidao)

[![Release](https://github.com/pkulijing/daobidao/actions/workflows/release.yml/badge.svg)](https://github.com/pkulijing/daobidao/actions/workflows/release.yml)
[![codecov](https://codecov.io/gh/pkulijing/daobidao/branch/master/graph/badge.svg)](https://codecov.io/gh/pkulijing/daobidao)
[![PyPI](https://img.shields.io/pypi/v/daobidao.svg)](https://pypi.org/project/daobidao/)

跨平台本地语音输入工具 —— 按住快捷键说话，松开后自动识别并输入到当前窗口。完全离线，无需联网。

基于 [Qwen3-ASR](https://www.modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx) + `onnxruntime` 推理，支持中/英/日/韩/粤等多语种，自带标点和大小写。支持 **macOS** 和 **Linux (X11)**。

## 安装

```bash
curl -LsSf https://raw.githubusercontent.com/pkulijing/daobidao/master/install.sh | sh
```

脚本自动安装所有依赖、下载模型(~990 MB)，装完即用。重复执行安全。

<details>
<summary>手动安装</summary>

**macOS:**

```bash
brew install portaudio
uv tool install daobidao
daobidao --init   # 下载模型 + 安装 .app bundle
```

首次运行需在「系统设置 > 隐私与安全性」中授予**辅助功能**和**麦克风**权限。

**Linux (Ubuntu 24.04+ / Debian 13+):**

```bash
sudo apt install xdotool xclip pulseaudio-utils libportaudio2 \
                 libgirepository-2.0-dev libcairo2-dev gir1.2-gtk-3.0 \
                 gir1.2-ayatanaappindicator3-0.1
sudo usermod -aG input $USER && newgrp input
uv tool install daobidao
daobidao --init   # 下载模型
```

**从源码:**

```bash
git clone https://github.com/pkulijing/daobidao && cd daobidao
bash scripts/setup.sh
uv run daobidao
```

</details>

## 使用

1. 启动后按住快捷键开始录音（macOS 默认右 Command，Linux 默认右 Ctrl）
2. 说话，松开快捷键
3. 识别结果自动输入到光标位置

```bash
daobidao -k KEY_FN        # 自定义快捷键
daobidao --help           # 更多选项
```

启动后自动打开浏览器设置页，也可通过系统托盘访问。支持在设置页切换模型大小（0.6B / 1.7B）、快捷键、界面语言等。

## License

MIT
