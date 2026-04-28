[中文](README.md) | **English**

# Daobidao

[![Release](https://github.com/pkulijing/daobidao/actions/workflows/release.yml/badge.svg)](https://github.com/pkulijing/daobidao/actions/workflows/release.yml)
[![codecov](https://codecov.io/gh/pkulijing/daobidao/branch/master/graph/badge.svg)](https://codecov.io/gh/pkulijing/daobidao)
[![PyPI](https://img.shields.io/pypi/v/daobidao.svg)](https://pypi.org/project/daobidao/)

Cross-platform local voice input tool — hold a hotkey, speak, release to have speech transcribed and typed into the focused window. Fully offline after first setup.

Powered by [Qwen3-ASR](https://www.modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx) + `onnxruntime`. Supports Chinese, English, Japanese, Korean, Cantonese, and more, with built-in punctuation and casing. Works on **macOS** and **Linux (X11)**.

## Install

```bash
curl -LsSf https://raw.githubusercontent.com/pkulijing/daobidao/master/install.sh | sh
```

The script installs all dependencies and downloads the model (~990 MB). Safe to re-run.

<details>
<summary>Manual install</summary>

**macOS:**

```bash
brew install portaudio
uv tool install daobidao
daobidao --init   # download model + install .app bundle
```

Grant **Accessibility** and **Microphone** permissions in System Settings > Privacy & Security on first run.

**Linux (Ubuntu 24.04+ / Debian 13+):**

```bash
sudo apt install xdotool xclip pulseaudio-utils libportaudio2 \
                 libgirepository-2.0-dev libcairo2-dev gir1.2-gtk-3.0 \
                 gir1.2-ayatanaappindicator3-0.1
sudo usermod -aG input $USER && newgrp input
uv tool install daobidao
daobidao --init   # download model
```

**From source:**

```bash
git clone https://github.com/pkulijing/daobidao && cd daobidao
bash scripts/setup.sh
uv run daobidao
```

</details>

## Usage

1. Hold the hotkey to record (macOS default: Right Command, Linux default: Right Ctrl)
2. Speak, then release
3. Transcribed text is typed at the cursor

```bash
daobidao -k KEY_FN        # custom hotkey
daobidao --help           # more options
```

A browser settings page opens on startup (also accessible via system tray). Switch model size (0.6B / 1.7B), hotkey, language, and more from there.

## License

MIT
