# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Whisper Input is a cross-platform desktop voice input tool (Linux + macOS): hold a hotkey, speak, release to have speech transcribed and typed into the focused window. Uses SenseVoice-Small ONNX (DAMO Academy's official quantized release on ModelScope, loaded via Microsoft's `onnxruntime`, no PyTorch, no sherpa-onnx) for local STT, and clipboard-based paste for text input.

Platform-specific backends in `backends/`:
- **Linux**: evdev for keyboard events, xclip+xdotool for text input, XDG autostart
- **macOS**: pynput for keyboard events and text input, LaunchAgents for autostart

## Commands

```bash
# Install dependencies (macOS)
bash setup_macos.sh
# or manually:
uv sync

# Install dependencies (Linux)
bash setup_linux.sh
# or manually:
uv sync

# Run
uv run python main.py
uv run python main.py -k KEY_FN           # custom hotkey (macOS Fn key)
uv run python main.py -k KEY_RIGHTALT     # custom hotkey
uv run python main.py --no-tray           # no system tray
uv run python main.py --no-preload        # skip model preload
uv run python main.py -c /path/config.yaml

# Lint (ruff)
uv run ruff check .

# Build package (macOS .app / Linux DEB — auto-detects platform)
bash build.sh
```

No automated test suite exists. For STT sanity check, run the five official test wavs (zh/en/ja/ko/yue) under `<user-data-dir>/models/sherpa-onnx-sense-voice-*/test_wavs/` through `stt.sense_voice.SenseVoiceSTT` after first download.

## Architecture

Event-driven pipeline orchestrated by `WhisperInput` in `main.py`:

```
HotkeyListener (backends/) → AudioRecorder (sounddevice, 16kHz mono)
                            → stt.SenseVoiceSTT (onnxruntime, SenseVoice ONNX)
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
- **stt/** — STT backend package (pluggable):
  - `stt/base.py` — `BaseSTT` abstract class (`load` + `transcribe`)
  - `stt/sense_voice.py` — SenseVoice-Small ONNX inference via `onnxruntime` + the ported `WavFrontend` / `SentencepiecesTokenizer` / `rich_transcription_postprocess` classes
  - `stt/_wav_frontend.py` — MIT-licensed port of `funasr_onnx/utils/frontend.py` (DAMO Speech Lab), the bit-aligned feature extraction pipeline (fbank + LFR + CMVN) used at SenseVoice training time
  - `stt/_tokenizer.py` — MIT-licensed port of `funasr_onnx/utils/sentencepiece_tokenizer.py`, thin wrapper over Google's `sentencepiece` SentencePieceProcessor
  - `stt/_postprocess.py` — MIT-licensed port of `funasr_onnx/utils/postprocess_utils.py` `rich_transcription_postprocess` (cleans SenseVoice meta tags `<|zh|>`/`<|HAPPY|>`/... into final text + emoji)
  - `stt/model_paths.py` — model version lock, ModelScope URLs for 5 files, SHA256 list, cache paths, manifest (stdlib-only)
  - `stt/downloader.py` — sequential ModelScope downloader + per-file SHA256 verification (stdlib-only, callable from setup_window bootstrap)
  - `stt/__init__.py` — `create_stt(engine, config)` factory (lazy imports so `stt.downloader` and `stt.model_paths` can be imported without numpy/onnxruntime)
- **model_state.py** — Compatibility shim that re-exports `find_local_model` / `save_state` from `stt.model_paths` for legacy call sites
- **config_manager.py** — YAML config with platform-aware paths and defaults
- **settings_server.py** — Built-in HTTP server serving web UI + REST API for settings

## Key Technical Decisions

- **Platform abstraction via `backends/`**: runtime dispatch based on `sys.platform`, no abstract base classes
- **Clipboard paste** over direct typing: avoids CJK encoding issues on both platforms
- **Web UI settings** over native GUI: cross-platform, uses stdlib `http.server`
- **300ms delay** on modifier key press: detects combo (e.g., Ctrl+C) vs single trigger
- **CPU-only ONNX runtime, unified across platforms**: no more cuda/cpu/mps dispatch; `onnxruntime` CPU RTF ≈ 0.1 is already more than fast enough for short utterances
- **DAMO Academy's official `iic/SenseVoiceSmall-onnx` (ModelScope) over third-party repackagings**: k2-fsa's sherpa-onnx int8 variant is a weight-only dynamic quantization that drops punctuation / ITN / English casing / language detection on real audio; the iic official `model_quant.onnx` is a properly calibrated quantization maintained by the same team that trained SenseVoice, shipped as FunASR's own production runtime, and is bit-aligned with the fp32 baseline. Direct inference via Microsoft's `onnxruntime`, no PyTorch, no sherpa-onnx
- **Feature extraction ported from `funasr_onnx`**: the 100-line `WavFrontend` class lives verbatim in `stt/_wav_frontend.py` (MIT, attribution preserved). Only `numpy + kaldi-native-fbank` deps, none of `funasr_onnx`'s heavier transitive deps (`librosa` / `scipy` / `jieba` are only needed for other FunASR models like Paraformer + CT-Transformer punctuation, not SenseVoice). Decoding and post-processing are also ported (`_tokenizer.py`, `_postprocess.py`)
- **Model distribution via ModelScope direct download**: first-launch fetches 5 files (`model_quant.onnx`, `tokens.json`, `am.mvn`, `config.yaml` from `iic/SenseVoiceSmall-onnx` + `chn_jpn_yue_eng_ko_spectok.bpe.model` from the sister repo `iic/SenseVoiceSmall`) via ModelScope's anonymous repo API. China-native CDN, no GitHub / ghproxy / HuggingFace / VPN involved. SHA256 verified per file
- **macOS uses pynput**: requires Accessibility permission for global key monitoring

## Ruff Configuration

Configured in `pyproject.toml` with rules: I (isort), N (pep8-naming), UP (pyupgrade), B (flake8-bugbear), SIM (flake8-simplify), RUF. Ignores RUF001/RUF002/RUF003 (Unicode punctuation). Line length: 80.

## Dependencies

Managed with `uv`. All packages come from the Tsinghua PyPI mirror — first-time `uv sync` takes seconds, not minutes. The STT runtime stack is:

- `onnxruntime` (~16 MB, Microsoft official)
- `kaldi-native-fbank` (~230 KB, `funasr_onnx`'s recommended fbank backend)
- `sentencepiece` (~1.5 MB, Google official BPE tokenizer)
- `numpy` + `pyyaml` (already used elsewhere in the project)

No torch, no torchaudio, no funasr, no sherpa-onnx, no cuda/cpu extras. Linux does not distinguish GPU/CPU variants anymore.

Model files (~231 MB total) are downloaded at first launch directly from ModelScope via `stt/downloader.py`. Five files land in `<user-data-dir>/models/iic-SenseVoiceSmall-onnx/`:

| File | Source repo | Size |
| --- | --- | --- |
| `model_quant.onnx` | `iic/SenseVoiceSmall-onnx` | 230 MB |
| `tokens.json` | `iic/SenseVoiceSmall-onnx` | 344 KB |
| `am.mvn` | `iic/SenseVoiceSmall-onnx` | 11 KB |
| `config.yaml` | `iic/SenseVoiceSmall-onnx` | 1.8 KB |
| `chn_jpn_yue_eng_ko_spectok.bpe.model` | `iic/SenseVoiceSmall` (sister PyTorch repo) | 368 KB |

After one successful download the app is fully offline. `find_local_model()` keeps a manifest in `<user-data-dir>/.model_state.json`.

## Upgrading the SenseVoice model

When DAMO pushes a new ONNX release:
1. Note the new revision / SHA256 of every file (ModelScope repo files API returns them)
2. Update `stt/model_paths.py` — `MODEL_VERSION` (new version tag, decides local cache dir name via `MODEL_DIR_NAME`) and each entry in `MODEL_FILES` (size + SHA256)
3. Old clients on old code keep using the old cached dir; new clients download the new version into a separate directory, preserving upgrade safety
