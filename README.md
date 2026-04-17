**English** | [中文](README.zh-CN.md)

# Whisper Input

[![Build](https://github.com/pkulijing/whisper-input/actions/workflows/build.yml/badge.svg)](https://github.com/pkulijing/whisper-input/actions/workflows/build.yml)
[![codecov](https://codecov.io/gh/pkulijing/whisper-input/branch/master/graph/badge.svg)](https://codecov.io/gh/pkulijing/whisper-input)
[![PyPI](https://img.shields.io/pypi/v/whisper-input.svg)](https://pypi.org/project/whisper-input/)

Cross-platform voice input tool — hold a hotkey, speak, release to have speech transcribed and typed into the focused window.

Uses the official DAMO Academy [SenseVoice-Small ONNX quantized model](https://www.modelscope.cn/models/iic/SenseVoiceSmall-onnx) (direct inference via Microsoft `onnxruntime`), fully offline after first download. Supports Chinese, English, Japanese, Korean, and Cantonese with **built-in punctuation, inverse text normalization, and casing**. The model is downloaded from ModelScope CDN (~231 MB) on first launch, then works permanently offline.

Supports **Linux (X11)** and **macOS**.

## Features

- Local speech recognition, works offline
- Multi-language mixed input (Chinese, English, etc.)
- Configurable hotkey (distinguishes left/right modifier keys)
- Browser-based settings UI + system tray
- Auto-start on login
- Automatic platform detection with matching backend

## System Requirements

### Linux
- **Ubuntu 24.04+ / Debian 13+** (X11 desktop environment)
- Any x86_64 CPU (`onnxruntime` CPU inference, RTF ~ 0.1, latency < 1s for short utterances)

### macOS
- macOS 12+ (Monterey or later)
- Apple Silicon (recommended) or Intel Mac, both use CPU ONNX inference

## Installation

### One-liner (recommended)

On macOS or Linux:

```bash
curl -LsSf https://raw.githubusercontent.com/pkulijing/whisper-input/master/install.sh | sh
```

The script interactively picks a language (中文 / English), then installs `uv`, Python 3.12, required system libraries, and `whisper-input` itself. It runs `whisper-input --init` (pre-downloads the ~231 MB SenseVoice ONNX model; on macOS also installs `~/Applications/Whisper Input.app`) and finally asks whether to launch the app immediately. It's safe to re-run — already-installed pieces are skipped, and `uv tool install --upgrade` upgrades `whisper-input` to the latest version.

On Linux the script will offer to add the current user to the `input` group (requires `sudo`; takes effect after a logout/login cycle).

> **Note**: `curl | sh` trusts this repo. If you want to review the script first, download it with `curl -LsSf <URL> -o install.sh` and inspect it before running.

### Manual installation

#### macOS

```bash
# Install system dependency
brew install portaudio

# Install the tool (--compile-bytecode skips the first-run .pyc compile step)
uv tool install --compile-bytecode whisper-input
# or: pipx install whisper-input

# One-time setup: install .app bundle + download STT model (~231 MB)
whisper-input --init

# Run
whisper-input
```

**First-run permissions required in System Settings > Privacy & Security:**

1. **Accessibility** (for global hotkey listening and text input)
2. **Microphone** (for voice recording; the system will prompt on first recording)

> **Note**: On first run (or via `whisper-input --init`), the tool installs a minimal `.app` bundle at `~/Applications/Whisper Input.app`. macOS permission dialogs and System Settings entries will show "Whisper Input" — grant Accessibility to that entry. To fully uninstall, run `whisper-input --uninstall` before `uv tool uninstall whisper-input`.

#### Linux

```bash
# Install system dependencies (see table below for details)
sudo apt install xdotool xclip pulseaudio-utils libportaudio2 \
                 libgirepository-2.0-dev libcairo2-dev gir1.2-gtk-3.0 \
                 gir1.2-ayatanaappindicator3-0.1

# Add yourself to the input group (evdev needs /dev/input/* access)
sudo usermod -aG input $USER && newgrp input

# Install the tool (--compile-bytecode skips the first-run .pyc compile step)
uv tool install --compile-bytecode whisper-input
# or: pipx install whisper-input

# One-time setup: download STT model (~231 MB)
whisper-input --init

# Run
whisper-input
```

**System dependency reference:**

| Package | Purpose | Notes |
|---------|---------|-------|
| `xdotool`, `xclip` | Text input | xclip for X11 clipboard, xdotool to simulate Shift+Insert paste |
| `libportaudio2` | Audio recording | PortAudio library, runtime dependency of Python `sounddevice` |
| `pulseaudio-utils` | Sound notifications | Provides `paplay` for start/stop recording sounds |
| `libgirepository-2.0-dev`, `libcairo2-dev` | Build dependencies | Headers for compiling `pygobject` and `pycairo` C extensions |
| `gir1.2-gtk-3.0` | Recording overlay | GTK 3 typelib for the recording status overlay |
| `gir1.2-ayatanaappindicator3-0.1` | System tray icon | AppIndicator typelib, runtime dependency of `pystray` on Linux |

On first run, `whisper-input` downloads the SenseVoice ONNX model (~231 MB) via `modelscope.snapshot_download` to `~/.cache/modelscope/hub/`. After one successful download, the app is fully offline.

#### From Source (Contributors)

```bash
git clone https://github.com/pkulijing/whisper-input
cd whisper-input
bash scripts/setup.sh
uv run whisper-input
```

## Usage

```bash
# Specify hotkey
whisper-input -k KEY_FN          # macOS: Fn/Globe key
whisper-input -k KEY_RIGHTALT    # Linux: Right Alt key

# More options
whisper-input --help
```

A browser settings page opens automatically on startup; you can also access it via the system tray icon.

### How to use

1. Start the app, then hold the hotkey to begin recording
   - macOS default: Right Command key
   - Linux default: Right Ctrl key
2. Speak into the microphone
3. Release the hotkey, wait for recognition
4. The recognized text is automatically typed at the cursor position

## Release Flow (Maintainers)

PyPI distribution via GitHub Actions tag trigger + Trusted Publishing (OIDC):

1. Bump `version` in `pyproject.toml`
2. `git commit -am "release: v0.5.1"` and push to master
3. `git tag v0.5.1 && git push --tags`
4. [`.github/workflows/release.yml`](.github/workflows/release.yml) triggers automatically: verify tag matches version -> `uv build` -> publish to PyPI via `pypa/gh-action-pypi-publish` -> create GitHub Release

## Configuration

Config file `config.yaml`, also editable via the browser settings UI:

| Setting | Description | macOS Default | Linux Default |
|---------|-------------|--------------|--------------|
| `hotkey` | Trigger hotkey | `KEY_RIGHTMETA` | `KEY_RIGHTCTRL` |
| `sensevoice.use_itn` | Inverse text normalization | `true` | `true` |
| `sound.enabled` | Recording sound notification | `true` | `true` |
| `ui.language` | Interface language (zh/en/fr) | `zh` | `zh` |

## Known Limitations

- Linux supports X11 only; Wayland is not yet supported
- Super/Win key is intercepted by GNOME desktop, not recommended as hotkey
- macOS requires Accessibility permission for global hotkey monitoring
- First run downloads the SenseVoice ONNX model (~231 MB from DAMO Academy ModelScope)

## Technical Architecture

The project uses src layout with all Python code under `src/whisper_input/`, installable as a standard package. The entry point is the `whisper-input` console script (equivalent to `python -m whisper_input`).

```
Hold hotkey -> HotkeyListener (whisper_input.backends) -> AudioRecorder (sounddevice)
Release     -> stt.SenseVoiceSTT (onnxruntime) -> InputMethod -> Text typed into focused window
```

Platform backends (`whisper_input.backends`) auto-select at runtime via `sys.platform`:
- **Linux**: evdev for keyboard events + xclip/xdotool clipboard paste
- **macOS**: pynput global keyboard listener + pbcopy/pbpaste + Cmd+V paste

STT inference (`whisper_input.stt`):
- Model: DAMO Academy official `iic/SenseVoiceSmall-onnx` (quantized), downloaded via `modelscope.snapshot_download` to `~/.cache/modelscope/hub/`
- Runtime: Microsoft official `onnxruntime`, no torch dependency
- Feature extraction, BPE decoding, meta-tag post-processing: ported from DAMO's `funasr_onnx` (MIT license, ~250 lines pure Python), bit-aligned with FunASR
- Dependency tree: `onnxruntime + kaldi-native-fbank + sentencepiece + numpy + modelscope` (modelscope base is only 36 MB, no torch/transformers)

Common features:
- 300ms delay on modifier key press to distinguish combos (e.g., Ctrl+C) from single triggers
- Clipboard paste instead of key simulation, avoiding CJK encoding issues
- Unified CPU inference path, zero code difference between macOS/Linux

## License

MIT
