# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Daobidao is a cross-platform desktop voice input tool (Linux + macOS): hold a hotkey, speak, release to have speech transcribed and typed into the focused window. Uses Qwen3-ASR (Alibaba Qwen team's encoder-decoder ASR) ONNX **fp16** quantization for local STT, loaded via Microsoft's `onnxruntime` (no PyTorch, no transformers), and clipboard-based paste for text input.

Project uses **src layout**: all Python code lives under `src/daobidao/` as a single installable distribution. `uv sync` installs it as an editable wheel; the `daobidao` console script (or `python -m daobidao`) is the only entry point. Dev setup scripts live in `scripts/`.

**Distribution is PyPI only, installed via `uv tool install daobidao`**. We don't document or support pipx / bare `pip install` paths — the in-app auto-updater only recognizes uv tool installs and shows a "please upgrade via uv tool" hint otherwise. No `.app` bundle as a release artifact, no `.deb`, no `python-build-standalone` bootstrap. If you see anything about `packaging/` / `scripts/build.sh` / `setup_window.py` in old docs, those were deleted in round 14 (see `docs/14-PyPI分发/`).

**Round 26 replaced SenseVoice with Qwen3-ASR**. SenseVoice recognized only keywords on many real utterances; Qwen3-ASR-0.6B produces exact-text matches on the same audio. Migration details in `docs/26-Qwen3-ASR替换SenseVoice/`. If you see anything about `stt/sense_voice.py` / `_wav_frontend.py` / `kaldi-native-fbank` / `sentencepiece` in old docs, those are gone — the STT stack is now `onnxruntime + tokenizers + modelscope + numpy`.

**Round 37 switched ONNX export from `zengshuishui/Qwen3-ASR-onnx` (int8, 3-session) to `baicai1145/Qwen3-ASR-{0.6B,1.7B}-ONNX` (fp16, 2-session)**. Motivation: the int8 export had 1.7B `transcribe()` confidently returning empty strings on certain audio numerics (issue #7) — confirmed via spike that 0.6B int8 is robust on the same inputs but 1.7B int8 prefill logits collapse to EOS. fp16 is a tier higher numerically and stable on both variants. Cost: CPU inference is ~2-3x slower than int8 (RTF goes from ~0.2 → 0.5 for 1.7B on 10s audio; offline acceptable, streaming kept working). Architecture changes encapsulated in `_onnx_runner.py` — encoder.onnx absorbs conv_frontend, KV cache axis order is now `(B, H, T, D)` with full `present_*` arrays returned each step (no delta scatter), decoder logits shape is `(B, vocab)` (last position only) but runner unsqueezes to `(B, 1, vocab)` so callers see the same `[0, -1]` interface as before. Double EOS `[151645, 151643]` exposed via `runner.eos_ids`. See `docs/37-换fp16-ONNX修1.7B-offline/` for full spike data + decision rationale.

**Future work / backlog** is tracked in **GitHub Issues** ([open issues](https://github.com/pkulijing/daobidao/issues)). Each issue carries three labels: `type:*` / `area:*` / `priority:*` (P0 必须做、P1 重大新功能、P2 一般小功能小修复). [docs/BACKLOG.md](docs/BACKLOG.md) is a flat **index of currently-open items** for at-a-glance triage — body / discussion / cross-round context all live in the issue itself. When closing an issue via `Closes #N` in a PR, also delete its line from BACKLOG.md (don't tick, just remove — closed-issue history stays permanently in GitHub). New ideas during a round go straight to `gh issue create` (templates in `.github/ISSUE_TEMPLATE/`); per-round `SUMMARY.md` "后续 TODO" sections should reference an issue # rather than re-describing the idea inline.

Platform-specific backends in `src/daobidao/backends/`:

- **Linux**: evdev for keyboard events, xclip+xdotool for text input, XDG autostart
- **macOS**: pynput for keyboard events and text input, LaunchAgents for autostart

## Commands

```bash
# Dev env setup (contributor clones repo; handles portaudio, uv, input group on Linux)
bash scripts/setup.sh          # auto-detects macOS / Linux
# manual equivalent:
uv sync

# Run (dev mode)
uv run daobidao
uv run daobidao -k KEY_FN           # custom hotkey (macOS Fn key)
uv run daobidao -k KEY_RIGHTALT     # custom hotkey
uv run daobidao --no-tray           # no system tray
uv run daobidao --no-preload        # skip model preload
uv run daobidao --allow-multiple    # skip single-instance kill (devs running 2+ in parallel)
uv run daobidao -c /path/config.yaml
# Equivalent invocation (bypasses the console script wrapper):
uv run python -m daobidao

# Lint (ruff)
uv run ruff check .

# Tests (round 15 framework, expanded in round 26)
uv run pytest                                # full suite, 239 cases, ~20s (incl. real Qwen3-0.6B smoke), prints coverage
uv run pytest tests/test_qwen3_asr.py -v     # one file
uv run pytest --cov-report=term-missing      # show un-hit lines
uv run pytest --cov-report=html              # generate htmlcov/index.html
uv run pytest --no-cov                       # turn off coverage (faster for single-test debug)
uv run pytest --deselect tests/test_qwen3_asr.py --deselect tests/test_qwen3_runner.py  # skip STT tests if model isn't cached yet

# Build wheel locally (for testing, not for release)
uv build
# Releases are cut by pushing a git tag v<version> — see .github/workflows/release.yml
```

Test scope (`tests/`): `config_manager` (including the sensevoice→qwen3 auto-migration), `version`, `settings_server` (full HTTP roundtrip on a real server bound to a tmp port, including the `/api/stt/switch_status` polling endpoint), the `backends/hotkey_*` 300ms combo state machine (parametrized over both backends), `backends/autostart_*` (plist / .desktop file generation), `backends/input_*` shell-out order, `stt/` factory, `stt/qwen3/*` (every module: `_feature` with a Whisper golden fixture, `_tokenizer`, `_prompt`, `_postprocess`, `_onnx_runner` against the real 0.6B + 1.7B models, and `qwen3_asr.py` end-to-end on `tests/fixtures/zh.wav` — a 10.6s self-recorded sample of 出师表 — also parametrized over both variants), `updater`, `__main__` shutdown + STT hot-switch worker. **Not tested**: `recorder.py` (mic), overlays (GTK / Cocoa), `__main__.main()` CLI/orchestration, real keyboard / TCC permission paths — those still need manual sanity checks. `tests/conftest.py` injects fake `pynput` / `evdev` modules into `sys.modules` so platform-specific backends import on either OS, and exposes session-scoped `stt_0_6b` / `stt_1_7b` fixtures (each calls `Qwen3ASRSTT.load()` once → modelscope cache hit on warm runs, network fetch on cold runs); path fixtures `qwen3_0_6b_dir` / `qwen3_1_7b_dir` 直接 = `stt.cache_root`(round 37 baicai1145 layout 把 model + tokenizer + metadata 全平铺在 repo 根)。CI runs ubuntu-only with `actions/cache@v5` caching `~/.cache/modelscope/hub`.

Coverage (round 26): overall ~61% line coverage (baseline before round 26 was 51%). The `stt/qwen3/` subpackage is 100% covered across all 8 modules. `config_manager` / `autostart_*` 90-100%, `settings_server` ~90%, `hotkey_*` state machine 54% (only listen loops / `start` / `stop` missing). The uncovered remainder is mostly `__main__.main()` CLI wiring (lines 429-665), `recorder.py`, `overlay_*.py` — all deliberate gaps, predating round 26.

The STT smoke tests download ~1.6 GB of ONNX + tokenizer for the 0.6B variant (and optionally ~4.7 GB for 1.7B) to `~/.cache/modelscope/hub/models/baicai1145/Qwen3-ASR-{0.6B,1.7B}-ONNX/` on first run. CI caches via `actions/cache@v5` keyed on `modelscope-qwen3-asr-v4` (bump to invalidate). Locally the model is usually already cached from running `daobidao` itself; if not, expect the first `pytest` invocation to be slow. fp16 推理比 int8 慢 ~2-3x,套件总耗时也跟着变长 (~8min on Apple Silicon for the full suite, vs ~20s pre-round-37) — 后续会单独优化(性能不在 round 37 scope)。

For STT sanity check, instantiate `daobidao.stt.qwen3.Qwen3ASRSTT(variant="0.6B")` and feed it a 16 kHz mono WAV.

## Architecture

Event-driven pipeline orchestrated by `WhisperInput` in `src/daobidao/__main__.py`:

```
HotkeyListener (daobidao.backends) → AudioRecorder (sounddevice, 16kHz mono)
                                        → daobidao.stt.Qwen3ASRSTT (onnxruntime)
                                        → InputMethod (daobidao.backends, clipboard paste)
```

Key modules (all paths relative to `src/daobidao/`):

- **`__main__.py`** — Entry point, CLI args, `WhisperInput` controller, system tray setup. Exposes `main()` for the console script. Also owns the STT variant hot-switch worker (background thread + atomic `self.stt` swap + `gc.collect` to free the old ONNX session). Startup序列在创建 `WhisperInput` 之前调用 `single_instance.kill_stale_instance(settings_port)`：发现有老实例占着 settings_port → HTTP `GET /api/pid` 验证身份 → SIGTERM → SIGKILL；`--allow-multiple` 跳过整个检测。
- **`single_instance.py`** — 单实例守门：`kill_stale_instance(port)` 用 stdlib socket 探端口、urllib 调老实例 `/api/pid` 拿 PID、`os.kill` 升级链 SIGTERM → SIGKILL。无新依赖（不引入 psutil）。详见 `docs/31-启动时清理已有实例/`。
- **`hotkey.py`** — Dispatcher: imports `HotkeyListener` from platform backend
- **`input_method.py`** — Dispatcher: imports `type_text` from platform backend
- **`overlay.py`** — Dispatcher: imports `RecordingOverlay` from platform backend
- **`backends/__init__.py`** — Platform detection: `IS_LINUX`, `IS_MACOS`
- **`backends/hotkey_linux.py`** — evdev keyboard monitoring with 300ms combo-key detection
- **`backends/hotkey_macos.py`** — pynput global keyboard listener with same combo-key logic
- **`backends/input_linux.py`** — xclip + xdotool Ctrl+V paste
- **`backends/input_macos.py`** — pbcopy/pbpaste + pynput Cmd+V paste
- **`backends/autostart_linux.py`** — XDG .desktop file autostart (template read via `importlib.resources` from `daobidao.assets`)
- **`backends/autostart_macos.py`** — LaunchAgents plist autostart; `ProgramArguments` points at `sys.prefix/bin/daobidao` (works for dev venv and uv tool installs), falls back to `[sys.executable, "-m", "daobidao"]`
- **`recorder.py`** — `AudioRecorder`: sounddevice capture → WAV bytes
- **`stt/`** — STT backend package (pluggable):
  - `stt/base.py` — `BaseSTT` abstract class (`load` + `transcribe`)
  - `stt/__init__.py` — `create_stt(engine, config)` factory (lazy imports so `--help` / tests don't pay the numpy/onnxruntime/modelscope import cost). Only `engine="qwen3"` is wired; anything else (including the legacy `"sensevoice"` string) raises `ValueError` — `ConfigManager._migrate_legacy` rewrites old configs on load, so this only fires if a user hand-edits `config.yaml`.
  - `stt/qwen3/` — Qwen3-ASR backend:
    - `qwen3_asr.py` — `Qwen3ASRSTT(BaseSTT)`: top-level class, `variant` ∈ `{"0.6B", "1.7B"}`, greedy decode loop with `_MAX_NEW_TOKENS=400`, breaks on **either** EOS in `runner.eos_ids`(round 37 起双 EOS)。`load()` is idempotent, warms up with 1s 高斯噪声(无需 30s pad,baicai1145 encoder 接受任意长度 mel),and inlines `modelscope.snapshot_download(REPO_ID_BY_VARIANT[variant], allow_patterns=...)`(per-variant repo,baicai1145 layout 把 model + tokenizer + metadata 全平铺在 repo 根)。After `load()` the `cache_root: Path` attribute points at the modelscope cache root — public, used by tests / settings UI / debugging。
    - `_feature.py` — Whisper-style log-mel spectrogram (N_MELS=128, N_FFT=400, HOP=160, N_SAMPLES=480000 = 30s @ 16 kHz). Slaney mel scale, periodic Hann window, reflect-pad STFT. Bit-aligned with `transformers.WhisperFeatureExtractor` to rtol=1e-4 — the golden fixture lives at `tests/fixtures/qwen3_log_mel_golden.npy`.
    - `_tokenizer.py` — Thin wrapper over HuggingFace `tokenizers` (Rust BPE, ~10 MB) loading Qwen3-ASR's `vocab.json` + `merges.txt` + 62 added tokens from `tokenizer_config.json`. No `transformers` dependency. Exposes common IDs: `eos_id=151645` (`<|im_end|>`), `im_start_id`, `audio_start_id`, `audio_end_id`, `audio_pad_id`, `asr_text_id`.
    - `_prompt.py` — Builds the chat-template prompt: `<|im_start|>system...<|im_end|>\n<|im_start|>user\n<|audio_start|>{audio_pad}*N<|audio_end|><|im_end|>\n<|im_start|>assistant\n`. N is the number of audio tokens from the encoder.
    - `_postprocess.py` — `parse_asr_output(raw)`: extract content after the last `<asr_text>` marker and strip any stray `<|...|>` chat tokens that leak through greedy decode.
    - `_onnx_runner.py` — `Qwen3ONNXRunner`: 2 ONNX sessions (`encoder.onnx` + `decoder.onnx`, baicai1145 fp16 export, conv_frontend 焊进 encoder)。28 decoder layers with KV cache shape `(1, 8 kv_heads, max_total_len=1664, 128 head_dim)` fp16 — note time axis is `axis=2` (HF transformers 风格), not `axis=1` like the old int8 export. `decoder_step` 接收 `past_key/value_XX` 输入,decoder 输出整段 `present_key/value_XX`(含历史 + 新内容),runner 直接覆盖 `caches` list 元素 —— 不用 scatter delta。decoder logits 输出只有最后一位置 `(B, vocab)`,runner 内部 `unsqueeze(axis=1)` 成 `(B, 1, vocab)` 让 callers `logits[0, -1]` 接口跟老 zengshuishui 兼容。`num_layers` / `max_total_len` / `audio_feature_dim` / `eos_ids` 都从 `metadata.json` 读,不 hardcode。`audio_feature_dim`: 0.6B → 1024, 1.7B → 2048。Double EOS 通过 `runner.eos_ids = (151645, 151643)`(`<|im_end|>` + `<|endoftext|>`)暴露。`encoder.encode_audio(mel)` 接受任意长度 mel — 不需要 30s pad,内部按 100 帧 chunk-align。
- **`config_manager.py`** — YAML config with platform-aware paths and defaults; dev mode detects repo root via `.git` + `pyproject.toml` marker, reads example config from `daobidao.assets` via `importlib.resources`. `_migrate_legacy(cfg)` rewrites `engine=sensevoice` + any `sensevoice.*` block into `engine=qwen3` + `qwen3.variant="0.6B"` on load, then auto-persists if changed. `_deep_merge` uses `copy.deepcopy` (not shallow) so `DEFAULT_CONFIG` can't be mutated through a returned dict — previously a latent bug.
- **`settings_server.py`** — Built-in HTTP server serving web UI + REST API for settings. Exposes `GET /api/stt/switch_status` so the "识别模型" dropdown can poll during a variant switch (every 500ms while `switching=true`).
- **`version.py`** — `__version__` from `importlib.metadata.version("daobidao")`, `__commit__` from package-data `_commit.txt` if present (release flow may write it) or `git rev-parse HEAD` fallback in dev mode
- **`assets/`** — Package data: `daobidao.png` (tray icon), `daobidao.desktop` (Linux autostart template, `Exec=daobidao` relies on PATH), `config.example.yaml`, `settings.html`, `locales/{zh,en,fr}.json`. Accessed via `importlib.resources.files("daobidao.assets")`.

## Key Technical Decisions

- **Platform abstraction via `backends/`**: runtime dispatch based on `sys.platform`, no abstract base classes
- **Clipboard paste** over direct typing: avoids CJK encoding issues on both platforms
- **Web UI settings** over native GUI: cross-platform, uses stdlib `http.server`
- **300ms delay** on modifier key press: detects combo (e.g., Ctrl+C) vs single trigger
- **CPU-only ONNX runtime, unified across platforms**: no cuda/cpu/mps dispatch; `onnxruntime` CPU is fast enough for short utterances (0.6B: ~1.5s for 10s audio on Apple Silicon)
- **Qwen3-ASR over SenseVoice-Small**: SenseVoice recognized only keywords / rough shape on many real utterances (we wrote keyword-match assertions, not exact-text assertions, in its tests). Qwen3-ASR-0.6B produces exact-text matches on the same audio — the quality gap is large enough to justify the model-size jump. 0.6B baicai1145 fp16 包是 ~1.6 GB,1.7B 是 ~4.7 GB(round 37 之前 zengshuishui int8 分别是 ~990 MB / ~2.4 GB,但 1.7B int8 在 offline path 上有 issue #7 那个确定性翻车,弃用)。1.7B variant 仍通过 settings-page dropdown 暴露给追求最大准确率的用户。
- **ModelScope distribution via `baicai1145/Qwen3-ASR-{0.6B,1.7B}-ONNX`** (round 37 起): community-maintained ONNX export of the official Qwen3-ASR weights, fp16 量化。两个 variant 是**独立 repo**(不像 round 26 用的 zengshuishui 单 repo 内嵌子目录的 layout),per-variant `snapshot_download` 各拉各的。`allow_patterns` 控制下载范围(`encoder.onnx` + `encoder.onnx.data` + `decoder.onnx` + `decoder.onnx.data` + tokenizer/metadata/chat_template 小文件)。每个 repo 自带 `metadata.json` 暴露 `num_layers` / `static_cache_len` / `audio_output_dim` / `eos_token_ids` / `n_window` 等关键参数,runner 全从 metadata 读不 hardcode。base `modelscope` pip 包仍只 ~36 MB,torch/transformers/scipy 仍在 `[framework]` extra 不装。
- **Log-mel feature extraction handwritten to match Whisper exactly**: ~100 lines of pure numpy in `stt/qwen3/_feature.py`, bit-aligned with `transformers.WhisperFeatureExtractor` (rtol=1e-4 golden test). Rolling our own avoids `librosa` / `scipy` / `transformers` as dependencies and keeps the STT stack at `onnxruntime + tokenizers + modelscope + numpy`.
- **Tokenization via HF `tokenizers` (Rust), not `transformers`**: Qwen3-ASR ships `vocab.json` + `merges.txt` + `tokenizer_config.json` (no `tokenizer.json` fast snapshot), so we rebuild a byte-level BPE tokenizer at load time with the 62 added tokens. The `tokenizers` wheel is ~10 MB vs `transformers`'s ~100 MB and a much heavier transitive graph.
- **Absolute-position KV cache + `cache_position` input**: the ONNX decoder takes `cache_position` as an explicit input, so we allocate a single fixed-size cache buffer (`(1, 1200, 8, 128)` per layer, both K and V, × 28 layers) and rewrite slices in place as generation advances. Round 27 streaming reuses this directly — no cache-shape renegotiation between chunks.
- **Hot-switch STT variant via background thread + atomic swap**: user picks 0.6B/1.7B in the settings-page dropdown → `WhisperInput._switch_stt_variant` runs in a background thread, builds a new `Qwen3ASRSTT`, calls `.load()` (download + warmup), then atomically assigns `self.stt = new_stt` and `gc.collect()`s the old ONNX session. The `/api/stt/switch_status` endpoint + dropdown polling gives the user progress feedback; in-flight transcriptions keep pointing at the old session because the atomic swap doesn't interrupt them.
- **macOS uses pynput**: requires only Accessibility permission for global key monitoring (Input Monitoring is NOT needed — that's for `kCGHIDEventTap`, we use `kCGSessionEventTap` + listen-only). First run installs `~/Applications/Daobidao.app` — a minimal Objective-C launcher that `dlopen`s libpython and runs `daobidao` in-process. TCC attributes the permission to "Daobidao" rather than the Python interpreter.
- **PyPI distribution only**: no `.app` / `.deb` / `.dmg` bundles. Round 14 deleted all of `packaging/`, `scripts/build.sh`, `scripts/run_macos.sh`, and the self-rolled `stt/downloader.py` / `stt/model_paths.py`. The premise is: in an immature project, chasing "one-click installer for non-technical users" was premature optimization; PyPI is the right baseline, fancy installers can come later once the foundation is proven

## Ruff Configuration

Configured in `pyproject.toml` with rules: I (isort), N (pep8-naming), UP (pyupgrade), B (flake8-bugbear), SIM (flake8-simplify), RUF. Ignores RUF001/RUF002/RUF003 (Unicode punctuation). Line length: 80.

## Dependencies

Managed with `uv`. All packages come from the Tsinghua PyPI mirror in dev; `uv.lock` pins everything. The STT runtime stack is:

- `onnxruntime` (~16 MB, Microsoft official)
- `tokenizers` (~10 MB, HuggingFace Rust BPE; replaces round-25's `sentencepiece`)
- `modelscope` (~36 MB base install — `filelock / packaging / requests / tqdm / urllib3` + modelscope itself; torch / transformers / scipy are hidden behind extras like `[framework]` which we do NOT install)
- `numpy` + `pyyaml` (shared)
- `soundfile` — WAV/FLAC decode in `tests/test_qwen3_runner.py` (also shipped as a runtime dep; cheap)

No torch, no torchaudio, no transformers, no funasr, no sherpa-onnx, no kaldi-native-fbank, no sentencepiece.

Model files for the 0.6B variant (~1.6 GB: encoder.onnx + decoder.onnx + .data shards + tokenizer + metadata) are downloaded on first `Qwen3ASRSTT.load()` via `modelscope.snapshot_download` and land in `~/.cache/modelscope/hub/models/baicai1145/Qwen3-ASR-0___6B-ONNX/`. Switching to 1.7B downloads the second bundle (~4.7 GB) into a separate per-variant cache dir. After one successful download the app is fully offline. Cache is managed by the `modelscope` library itself (content addressing + validity metadata).

## Upgrading the Qwen3-ASR model

When the upstream ModelScope repos push a new ONNX export:

1. Test manually in a dev venv whether the new revision still works (snapshot_download defaults to the repo's default branch, so pulling fresh automatically picks up the latest)
2. If you want to pin to a specific revision, pass `revision="<tag-or-commit>"` to the `snapshot_download` call inside `Qwen3ASRSTT.load()` in `src/daobidao/stt/qwen3/qwen3_asr.py`(注意要 per-variant 改 — `REPO_ID_BY_VARIANT` 是个 dict)
3. No SHA256 lock to update — `modelscope` verifies file integrity via its own metadata (content-length + per-file hash from the repo manifest)
4. If the ONNX graph changes its IO schema (new inputs/outputs, renamed tensors), 跑 `docs/37-换fp16-ONNX修1.7B-offline/spike.py` 拿新 bundle dump schema,跟 round 37 documented schema 对比;runner 从 metadata.json 读 num_layers / cache_len / audio_output_dim / eos_token_ids,可以兼容这些数字变化,但 input/output 名字变(`padded_feature` → 别的)就要改代码

## Distribution & Release

End users install from PyPI:

```bash
uv tool install daobidao
```

This is the **only** supported install path. The in-app auto-updater only
recognizes uv-tool installs; other installation methods (pipx, bare pip) will
see the updater banner but the "Update now" button only prints a manual upgrade
hint instead of shelling out to pip/pipx.

Release flow (maintainer):

1. Bump `version` in `pyproject.toml`, commit, push to master
2. `git tag v<version> && git push --tags`
3. `.github/workflows/release.yml` is triggered by the tag, verifies tag matches pyproject version, runs `uv build`, publishes to PyPI via OIDC Trusted Publishing (no API token), and creates a GitHub Release with the dist artifacts attached
