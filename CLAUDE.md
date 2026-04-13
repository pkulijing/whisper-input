# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Whisper Input is a cross-platform desktop voice input tool (Linux + macOS): hold a hotkey, speak, release to have speech transcribed and typed into the focused window. Uses SenseVoice (FunASR) for local STT, and clipboard-based paste for text input.

Platform-specific backends in `backends/`:
- **Linux**: evdev for keyboard events, xclip+xdotool for text input, XDG autostart
- **macOS**: pynput for keyboard events and text input, LaunchAgents for autostart

## Commands

```bash
# Install dependencies (macOS)
bash setup_macos.sh
# or manually:
uv sync --group macos

# Install dependencies (Linux)
bash setup.sh
# or manually:
uv sync --group linux-cuda

# Run
uv run python main.py
uv run python main.py -k KEY_FN           # custom hotkey (macOS Fn key)
uv run python main.py -k KEY_RIGHTALT     # custom hotkey
uv run python main.py --no-tray           # no system tray
uv run python main.py --no-preload        # skip model preload
uv run python main.py -c /path/config.yaml

# Lint (ruff)
uv run ruff check .

# Build DEB package (Linux only)
bash build_deb.sh
```

No automated test suite exists.

## Architecture

Event-driven pipeline orchestrated by `WhisperInput` in `main.py`:

```
HotkeyListener (backends/) → AudioRecorder (sounddevice, 16kHz mono)
                            → SenseVoiceSTT (FunASR, local model)
                            → InputMethod (backends/, clipboard paste)
```

Key modules:
- **main.py** — Entry point, CLI args, `WhisperInput` controller, system tray setup
- **hotkey.py** — Dispatcher: imports `HotkeyListener` from platform backend
- **input_method.py** — Dispatcher: imports `type_text` from platform backend
- **backends/__init__.py** — Platform detection: `IS_LINUX`, `IS_MACOS`
- **backends/hotkey_linux.py** — evdev keyboard monitoring with 300ms combo-key detection
- **backends/hotkey_macos.py** — pynput global keyboard listener with same combo-key logic
- **backends/input_linux.py** — xclip + xdotool Ctrl+V paste
- **backends/input_macos.py** — pbcopy/pbpaste + pynput Cmd+V paste
- **backends/autostart_linux.py** — XDG .desktop file autostart
- **backends/autostart_macos.py** — LaunchAgents plist autostart
- **recorder.py** — `AudioRecorder`: sounddevice capture → WAV bytes
- **stt_sensevoice.py** — `SenseVoiceSTT`: FunASR SenseVoice-Small, preloaded at startup by `main.py` via `preload_model()` (default; `--no-preload` to skip), device fallback
- **config_manager.py** — YAML config with platform-aware paths and defaults
- **settings_server.py** — Built-in HTTP server serving web UI + REST API for settings

## Key Technical Decisions

- **Platform abstraction via `backends/`**: runtime dispatch based on `sys.platform`, no abstract base classes
- **Clipboard paste** over direct typing: avoids CJK encoding issues on both platforms
- **Web UI settings** over native GUI: cross-platform, uses stdlib `http.server`
- **300ms delay** on modifier key press: detects combo (e.g., Ctrl+C) vs single trigger
- **Device auto-fallback**: cuda → cpu (Linux), mps → cpu (macOS)
- **macOS uses pynput**: requires Accessibility permission for global key monitoring

## Ruff Configuration

Configured in `pyproject.toml` with rules: I (isort), N (pep8-naming), UP (pyupgrade), B (flake8-bugbear), SIM (flake8-simplify), RUF. Ignores RUF001/RUF002/RUF003 (Unicode punctuation). Line length: 80.

## Dependencies

Managed with `uv`. Platform-specific deps use environment markers in `pyproject.toml`. PyTorch installed via dependency groups: `linux-cuda` (CUDA 12.1 from SJTU mirror) or `macos` (standard PyPI).
