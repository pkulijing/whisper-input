# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Whisper Input is a cross-platform desktop voice input tool (Linux + macOS): hold a hotkey, speak, release to have speech transcribed and typed into the focused window. Uses SenseVoice-Small ONNX (DAMO Academy's official quantized release on ModelScope, loaded via Microsoft's `onnxruntime`, no PyTorch, no sherpa-onnx) for local STT, and clipboard-based paste for text input.

Project uses **src layout**: all Python code lives under `src/whisper_input/` as a single installable distribution. `uv sync` installs it as an editable wheel; the `whisper-input` console script (or `python -m whisper_input`) is the only entry point. Dev setup scripts live in `scripts/`.

**Distribution is PyPI only**: end users install with `uv tool install whisper-input` / `pipx install whisper-input` / `pip install whisper-input`. No `.app` bundle, no `.deb`, no `python-build-standalone` bootstrap. If you see anything about `packaging/` / `scripts/build.sh` / `setup_window.py` in old docs, those were deleted in round 14 (see `docs/14-PyPI分发/`).

Platform-specific backends in `src/whisper_input/backends/`:
- **Linux**: evdev for keyboard events, xclip+xdotool for text input, XDG autostart
- **macOS**: pynput for keyboard events and text input, LaunchAgents for autostart

## Commands

```bash
# Dev env setup (contributor clones repo; handles portaudio, uv, input group on Linux)
bash scripts/setup_macos.sh    # or setup_linux.sh
# manual equivalent:
uv sync

# Run (dev mode)
uv run whisper-input
uv run whisper-input -k KEY_FN           # custom hotkey (macOS Fn key)
uv run whisper-input -k KEY_RIGHTALT     # custom hotkey
uv run whisper-input --no-tray           # no system tray
uv run whisper-input --no-preload        # skip model preload
uv run whisper-input -c /path/config.yaml
# Equivalent invocation (bypasses the console script wrapper):
uv run python -m whisper_input

# Lint (ruff)
uv run ruff check .

# Build wheel locally (for testing, not for release)
uv build
# Releases are cut by pushing a git tag v<version> — see .github/workflows/release.yml
```

No automated test suite exists. For STT sanity check, instantiate `whisper_input.stt.sense_voice.SenseVoiceSTT` and feed it a 16 kHz mono WAV.

## Architecture

Event-driven pipeline orchestrated by `WhisperInput` in `src/whisper_input/__main__.py`:

```
HotkeyListener (whisper_input.backends) → AudioRecorder (sounddevice, 16kHz mono)
                                        → whisper_input.stt.SenseVoiceSTT (onnxruntime)
                                        → InputMethod (whisper_input.backends, clipboard paste)
```

Key modules (all paths relative to `src/whisper_input/`):
- **`__main__.py`** — Entry point, CLI args, `WhisperInput` controller, system tray setup. Exposes `main()` for the console script.
- **`hotkey.py`** — Dispatcher: imports `HotkeyListener` from platform backend
- **`input_method.py`** — Dispatcher: imports `type_text` from platform backend
- **`overlay.py`** — Dispatcher: imports `RecordingOverlay` from platform backend
- **`backends/__init__.py`** — Platform detection: `IS_LINUX`, `IS_MACOS`
- **`backends/hotkey_linux.py`** — evdev keyboard monitoring with 300ms combo-key detection
- **`backends/hotkey_macos.py`** — pynput global keyboard listener with same combo-key logic
- **`backends/input_linux.py`** — xclip + xdotool Ctrl+V paste
- **`backends/input_macos.py`** — pbcopy/pbpaste + pynput Cmd+V paste
- **`backends/autostart_linux.py`** — XDG .desktop file autostart (template read via `importlib.resources` from `whisper_input.assets`)
- **`backends/autostart_macos.py`** — LaunchAgents plist autostart; `ProgramArguments` points at `sys.prefix/bin/whisper-input` (works for venv / uv tool / pipx equally), falls back to `[sys.executable, "-m", "whisper_input"]`
- **`recorder.py`** — `AudioRecorder`: sounddevice capture → WAV bytes
- **`stt/`** — STT backend package (pluggable):
  - `stt/base.py` — `BaseSTT` abstract class (`load` + `transcribe`)
  - `stt/sense_voice.py` — SenseVoice-Small ONNX inference via `onnxruntime` + the ported `WavFrontend` / `SentencepiecesTokenizer` / `rich_transcription_postprocess` classes. Calls `modelscope.snapshot_download` on first `load()` to fetch model files from ModelScope
  - `stt/_wav_frontend.py` — MIT-licensed port of `funasr_onnx/utils/frontend.py` (DAMO Speech Lab), the bit-aligned feature extraction pipeline (fbank + LFR + CMVN) used at SenseVoice training time
  - `stt/_tokenizer.py` — MIT-licensed port of `funasr_onnx/utils/sentencepiece_tokenizer.py`, thin wrapper over Google's `sentencepiece` SentencePieceProcessor
  - `stt/_postprocess.py` — MIT-licensed port of `funasr_onnx/utils/postprocess_utils.py` `rich_transcription_postprocess` (cleans SenseVoice meta tags `<|zh|>`/`<|HAPPY|>`/... into final text + emoji)
  - `stt/__init__.py` — `create_stt(engine, config)` factory (lazy imports so `--help` / tests don't pay the numpy/onnxruntime/modelscope import cost)
- **`config_manager.py`** — YAML config with platform-aware paths and defaults; dev mode detects repo root via `.git` + `pyproject.toml` marker, reads example config from `whisper_input.assets` via `importlib.resources`
- **`settings_server.py`** — Built-in HTTP server serving web UI + REST API for settings
- **`version.py`** — `__version__` from `importlib.metadata.version("whisper-input")`, `__commit__` from package-data `_commit.txt` if present (release flow may write it) or `git rev-parse HEAD` fallback in dev mode
- **`assets/`** — Package data: `whisper-input.png` (tray icon), `whisper-input.desktop` (Linux autostart template, `Exec=whisper-input` relies on PATH), `config.example.yaml`. Accessed via `importlib.resources.files("whisper_input.assets")`.

## Key Technical Decisions

- **Platform abstraction via `backends/`**: runtime dispatch based on `sys.platform`, no abstract base classes
- **Clipboard paste** over direct typing: avoids CJK encoding issues on both platforms
- **Web UI settings** over native GUI: cross-platform, uses stdlib `http.server`
- **300ms delay** on modifier key press: detects combo (e.g., Ctrl+C) vs single trigger
- **CPU-only ONNX runtime, unified across platforms**: no more cuda/cpu/mps dispatch; `onnxruntime` CPU RTF ≈ 0.1 is already more than fast enough for short utterances
- **DAMO Academy's official `iic/SenseVoiceSmall-onnx` (ModelScope) over third-party repackagings**: k2-fsa's sherpa-onnx int8 variant is a weight-only dynamic quantization that drops punctuation / ITN / English casing / language detection on real audio; the iic official `model_quant.onnx` is a properly calibrated quantization maintained by the same team that trained SenseVoice, shipped as FunASR's own production runtime, and is bit-aligned with the fp32 baseline. Direct inference via Microsoft's `onnxruntime`, no PyTorch, no sherpa-onnx
- **Feature extraction ported from `funasr_onnx`**: the 100-line `WavFrontend` class lives verbatim in `stt/_wav_frontend.py` (MIT, attribution preserved). Only `numpy + kaldi-native-fbank` deps, none of `funasr_onnx`'s heavier transitive deps (`librosa` / `scipy` / `jieba` are only needed for other FunASR models like Paraformer + CT-Transformer punctuation, not SenseVoice). Decoding and post-processing are also ported (`_tokenizer.py`, `_postprocess.py`)
- **Model distribution via the official `modelscope` library**: `stt/sense_voice.py`'s `load()` calls `modelscope.snapshot_download("iic/SenseVoiceSmall-onnx")` for the 4 ONNX files (~231 MB) and a second `snapshot_download("iic/SenseVoiceSmall", allow_patterns=["chn_jpn_yue_eng_ko_spectok.bpe.model"])` for the BPE tokenizer file alone (avoids pulling ~900 MB of PyTorch weights from the sister repo). Cache lands in `~/.cache/modelscope/hub/` (the library's default). The base `modelscope` pip package is only ~36 MB with minimal transitive deps — torch / transformers / scipy are hidden behind extras like `[framework]`, which we don't install
- **macOS uses pynput**: requires Accessibility permission for global key monitoring. **Under PyPI install (`uv tool install` / `pipx install`) the TCC dialog will be attributed to the Python interpreter at `sys.executable`, NOT to "whisper-input.app"** — there's no bundle. User has to authorize that Python binary in System Settings → Privacy & Security → Accessibility + Input Monitoring
- **PyPI distribution only**: no `.app` / `.deb` / `.dmg` bundles. Round 14 deleted all of `packaging/`, `scripts/build.sh`, `scripts/run_macos.sh`, and the self-rolled `stt/downloader.py` / `stt/model_paths.py`. The premise is: in an immature project, chasing "one-click installer for non-technical users" was premature optimization; PyPI is the right baseline, fancy installers can come later once the foundation is proven

## Ruff Configuration

Configured in `pyproject.toml` with rules: I (isort), N (pep8-naming), UP (pyupgrade), B (flake8-bugbear), SIM (flake8-simplify), RUF. Ignores RUF001/RUF002/RUF003 (Unicode punctuation). Line length: 80.

## Dependencies

Managed with `uv`. All packages come from the Tsinghua PyPI mirror in dev; `uv.lock` pins everything. The STT runtime stack is:

- `onnxruntime` (~16 MB, Microsoft official)
- `kaldi-native-fbank` (~230 KB, `funasr_onnx`'s recommended fbank backend)
- `sentencepiece` (~1.5 MB, Google official BPE tokenizer)
- `modelscope` (~36 MB base install — `filelock / packaging / requests / tqdm / urllib3` + modelscope itself; torch / transformers / scipy are hidden behind extras like `[framework]` which we do NOT install)
- `numpy` + `pyyaml` (shared)

No torch, no torchaudio, no funasr, no sherpa-onnx.

Model files (~231 MB total, 4 ONNX + 1 BPE tokenizer) are downloaded on first `SenseVoiceSTT.load()` via `modelscope.snapshot_download` and land in `~/.cache/modelscope/hub/iic/SenseVoiceSmall-onnx/` and `~/.cache/modelscope/hub/iic/SenseVoiceSmall/`. After one successful download the app is fully offline. Cache is managed by the `modelscope` library itself (content addressing + validity metadata).

## Upgrading the SenseVoice model

When DAMO pushes a new ONNX release:
1. Test manually in a dev venv whether the new revision still works (snapshot_download defaults to the repo's default branch — usually `master` — so pulling fresh automatically picks up the latest)
2. If you want to pin to a specific revision, pass `revision="<tag-or-commit>"` to the two `snapshot_download` calls in `src/whisper_input/stt/sense_voice.py:load()`
3. No SHA256 lock to update anymore — `modelscope` verifies file integrity via its own metadata (content-length + per-file hash from the repo manifest)

## Distribution & Release

End users install from PyPI:

```bash
uv tool install whisper-input    # recommended
pipx install whisper-input       # equivalent
pip install whisper-input        # if you really want a system-level install
```

Release flow (maintainer):

1. Bump `version` in `pyproject.toml`, commit, push to master
2. `git tag v<version> && git push --tags`
3. `.github/workflows/release.yml` is triggered by the tag, verifies tag matches pyproject version, runs `uv build`, publishes to PyPI via OIDC Trusted Publishing (no API token), and creates a GitHub Release with the dist artifacts attached

First-time setup (one-off): add a pending Trusted Publisher at `https://pypi.org/manage/account/publishing/` (owner=`pkulijing`, repo=`whisper-input`, workflow=`release.yml`, environment=`pypi`) and create a `pypi` environment under Settings → Environments in the GitHub repo.
