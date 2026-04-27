"""
Spike: compare streaming strategies vs offline baseline for Qwen3-ASR.

Decision goal: pick between
  - Path A: pre-allocated zero audio_features buffer, single prompt prefill,
    subsequent chunks fill buffer, committed KV reused (cheapest, staleness risk)
  - Path E: chat prefix KV cached, audio_pad + suffix + committed re-prefilled
    each chunk with real audio_features (zero staleness, higher per-chunk cost)
  - baseline: offline `Qwen3ASRSTT.transcribe()` as ground truth

Runs against tests/fixtures/zh.wav (10.6s 出师表) with ROLLBACK_TOKENS=10,
CHUNK_SIZE_SEC=2.0, MAX_NEW_TOKENS_PER_CHUNK=32.

Emitted metrics per path:
  - Final transcript + char-level edit distance vs baseline
  - Prefix stability rate (committed tokens that were later changed in
    subsequent chunks — always 0 here because we never rewrite committed)
  - Per-chunk wall-clock latency
  - Rollback hit rate (pending tokens that got different ids in next chunk)

Usage:
    uv run python scripts/spike_qwen3_streaming.py

One-shot tooling, deleted after SUMMARY.md captures the decision.
"""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from daobidao.stt.qwen3._feature import (
    SAMPLE_RATE,
    log_mel_spectrogram,
    pad_or_trim,
)
from daobidao.stt.qwen3._onnx_runner import Qwen3ONNXRunner
from daobidao.stt.qwen3._postprocess import (
    parse_asr_output,
)
from daobidao.stt.qwen3._prompt import (
    AUDIO_END,
    AUDIO_START,
    IM_END,
    IM_START,
    build_prompt,
)
from daobidao.stt.qwen3._tokenizer import Qwen3Tokenizer

ROLLBACK_TOKENS = 10
CHUNK_SIZE_SEC = 2.0
CHUNK_SIZE_SAMPLES = int(CHUNK_SIZE_SEC * SAMPLE_RATE)
MAX_NEW_TOKENS_PER_CHUNK = 32
OFFLINE_MAX_TOKENS = 400

WAV_PATH = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "zh.wav"
)
MODEL_ROOT = (
    Path.home()
    / ".cache"
    / "modelscope"
    / "hub"
    / "models"
    / "zengshuishui"
    / "Qwen3-ASR-onnx"
)
MODEL_DIR = MODEL_ROOT / "model_0.6B"
TOKENIZER_DIR = MODEL_ROOT / "tokenizer"


# --------------------------------------------------------------------------
# utils
# --------------------------------------------------------------------------


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE, (
            f"expected 16 kHz, got {wf.getframerate()}"
        )
        assert wf.getnchannels() == 1, "expected mono"
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def edit_distance(a: str, b: str) -> int:
    """Char-level Levenshtein."""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            curr[j] = min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (ca != cb),
            )
        prev = curr
    return prev[-1]


def chunk_audio(audio: np.ndarray) -> list[np.ndarray]:
    """Split audio into 2s chunks; last chunk may be shorter."""
    chunks = []
    for i in range(0, len(audio), CHUNK_SIZE_SAMPLES):
        chunks.append(audio[i : i + CHUNK_SIZE_SAMPLES])
    return chunks


# --------------------------------------------------------------------------
# offline baseline (= current production path)
# --------------------------------------------------------------------------


def run_baseline(
    runner: Qwen3ONNXRunner, tok: Qwen3Tokenizer, audio: np.ndarray
) -> tuple[str, list[int], float, int]:
    """Single-pass offline decode. Returns (text, token_ids, elapsed, n_af)."""
    t0 = time.perf_counter()
    padded = pad_or_trim(audio)
    mel = log_mel_spectrogram(padded)
    audio_features = runner.encode_audio(mel)
    n_af = audio_features.shape[1]
    prompt_ids = tok.encode(build_prompt(n_af))

    caches = runner.alloc_decoder_caches()
    logits = runner.decoder_step(
        np.array([prompt_ids], dtype=np.int64), audio_features, caches, 0
    )
    cur_len = len(prompt_ids)

    generated: list[int] = []
    for _ in range(OFFLINE_MAX_TOKENS):
        nid = int(np.argmax(logits[0, -1]))
        if nid == tok.eos_id:
            break
        generated.append(nid)
        logits = runner.decoder_step(
            np.array([[nid]], dtype=np.int64),
            audio_features,
            caches,
            cur_len,
        )
        cur_len += 1

    elapsed = time.perf_counter() - t0
    text = parse_asr_output(tok.decode(generated, skip_special_tokens=True))
    return text, generated, elapsed, n_af


# --------------------------------------------------------------------------
# streaming paths
# --------------------------------------------------------------------------


class StreamResult:
    def __init__(self) -> None:
        self.text: str = ""
        self.committed_tokens: list[int] = []
        self.per_chunk_latency_s: list[float] = []
        self.pending_history: list[list[int]] = []  # one per chunk
        self.committed_history: list[list[int]] = []  # snapshot per chunk
        # overflow flag
        self.overflowed: bool = False


def _greedy_generate(
    runner: Qwen3ONNXRunner,
    tok: Qwen3Tokenizer,
    first_logits: np.ndarray,
    caches: list[np.ndarray],
    audio_features: np.ndarray,
    cur_len: int,
    max_new: int,
) -> tuple[list[int], np.ndarray, int]:
    """Greedy decode loop: argmax logits, feed back, stop on EOS or cap.

    Returns (new_token_ids, last_logits, new_cur_len).
    """
    generated: list[int] = []
    logits = first_logits
    for _ in range(max_new):
        nid = int(np.argmax(logits[0, -1]))
        if nid == tok.eos_id:
            break
        generated.append(nid)
        logits = runner.decoder_step(
            np.array([[nid]], dtype=np.int64),
            audio_features,
            caches,
            cur_len,
        )
        cur_len += 1
    return generated, logits, cur_len


def run_path_a(
    runner: Qwen3ONNXRunner,
    tok: Qwen3Tokenizer,
    audio: np.ndarray,
    n_max: int,
) -> StreamResult:
    """Path A: pre-allocated zero audio_features buffer, single prompt prefill.

    - prompt built with audio_token_count=n_max (all audio_pads up front)
    - initial decoder prefill against zero buffer caches KV for all audio_pads
    - each chunk: encode new audio, fill next slice of buffer; re-run last
      committed token to get fresh logits (with current audio_features);
      greedy-generate up to ROLLBACK_TOKENS+spare and split into committed/pending
    """
    audio_features_buf = np.zeros((1, n_max, 1024), dtype=np.float32)
    af_len = 0
    prompt_ids = tok.encode(build_prompt(n_max))
    caches = runner.alloc_decoder_caches()

    # Initial prefill: all audio_pads + chat template at cur_len=0
    _init_logits = runner.decoder_step(
        np.array([prompt_ids], dtype=np.int64),
        audio_features_buf,
        caches,
        0,
    )

    committed: list[int] = []
    pending: list[int] = []
    result = StreamResult()
    chunks = chunk_audio(audio)

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        t0 = time.perf_counter()

        # --- encode new audio ---
        mel = log_mel_spectrogram(chunk.astype(np.float32))
        new_af = runner.encode_audio(mel)
        n_new = new_af.shape[1]
        if af_len + n_new > n_max:
            result.overflowed = True
            break
        audio_features_buf[:, af_len : af_len + n_new, :] = new_af
        af_len += n_new

        # --- rollback: refresh last committed (or last prompt) token's KV,
        # so its logits reflect the now-extended audio_features buffer.
        # Positions [len(prompt)+len(committed), ...) are about to be
        # regenerated; pending tokens are discarded.
        if committed:
            anchor_tok = committed[-1]
            anchor_pos = len(prompt_ids) + len(committed) - 1
        else:
            anchor_tok = prompt_ids[-1]
            anchor_pos = len(prompt_ids) - 1

        refresh_logits = runner.decoder_step(
            np.array([[anchor_tok]], dtype=np.int64),
            audio_features_buf,
            caches,
            anchor_pos,
        )
        cur_len = anchor_pos + 1  # = len(prompt_ids) + len(committed)

        # --- generate new tokens ---
        new_generated, _last_logits, cur_len = _greedy_generate(
            runner,
            tok,
            refresh_logits,
            caches,
            audio_features_buf,
            cur_len,
            MAX_NEW_TOKENS_PER_CHUNK,
        )

        # --- commit / pending split ---
        if is_last:
            committed.extend(new_generated)
            pending_now: list[int] = []
        elif len(new_generated) <= ROLLBACK_TOKENS:
            pending_now = new_generated
        else:
            committed.extend(new_generated[:-ROLLBACK_TOKENS])
            pending_now = new_generated[-ROLLBACK_TOKENS:]

        pending = pending_now
        result.per_chunk_latency_s.append(time.perf_counter() - t0)
        result.pending_history.append(list(pending))
        result.committed_history.append(list(committed))

    result.committed_tokens = committed
    result.text = parse_asr_output(
        tok.decode(committed, skip_special_tokens=True)
    )
    return result


def run_path_e(
    runner: Qwen3ONNXRunner,
    tok: Qwen3Tokenizer,
    audio: np.ndarray,
) -> StreamResult:
    """Path E: prefix-cached re-prefill.

    - prefix = chat template up to <|audio_start|> (cached at stream start)
    - each chunk re-prefills [audio_pad * current_n_af + chat_suffix +
      committed_tokens] with real audio_features; zero staleness.
    """
    chat_prefix = f"{IM_START}system\n{IM_END}\n{IM_START}user\n{AUDIO_START}"
    chat_suffix = f"{AUDIO_END}{IM_END}\n{IM_START}assistant\n"
    prefix_ids = tok.encode(chat_prefix)
    suffix_ids = tok.encode(chat_suffix)

    caches = runner.alloc_decoder_caches()
    # Initial prefill of chat prefix only.
    # Cross-attn on these tokens uses a dummy 1-slot audio_features buffer:
    # prefix tokens are chat template (not audio_pads), their cross-attn
    # output matters little because they don't carry audio-semantic content.
    dummy_af = np.zeros((1, 1, 1024), dtype=np.float32)
    _ = runner.decoder_step(
        np.array([prefix_ids], dtype=np.int64),
        dummy_af,
        caches,
        0,
    )

    af_pieces: list[np.ndarray] = []
    committed: list[int] = []
    pending: list[int] = []
    result = StreamResult()
    chunks = chunk_audio(audio)

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        t0 = time.perf_counter()

        # --- encode new audio + extend audio_features ---
        mel = log_mel_spectrogram(chunk.astype(np.float32))
        new_af = runner.encode_audio(mel)
        af_pieces.append(new_af)
        af = np.concatenate(af_pieces, axis=1)
        n_af = af.shape[1]

        # --- Re-prefill mid section: audio_pads + chat_suffix + committed ---
        # (pending is discarded entirely at start of each chunk)
        mid_ids = [tok.audio_pad_id] * n_af + suffix_ids + list(committed)
        cur_len = len(prefix_ids)
        if cur_len + len(mid_ids) > runner.max_total_len:
            result.overflowed = True
            break
        prefill_logits = runner.decoder_step(
            np.array([mid_ids], dtype=np.int64), af, caches, cur_len
        )
        cur_len += len(mid_ids)

        # --- generate new tokens ---
        new_generated, _, cur_len = _greedy_generate(
            runner,
            tok,
            prefill_logits,
            caches,
            af,
            cur_len,
            MAX_NEW_TOKENS_PER_CHUNK,
        )

        if is_last:
            committed.extend(new_generated)
            pending_now: list[int] = []
        elif len(new_generated) <= ROLLBACK_TOKENS:
            pending_now = new_generated
        else:
            committed.extend(new_generated[:-ROLLBACK_TOKENS])
            pending_now = new_generated[-ROLLBACK_TOKENS:]

        pending = pending_now
        result.per_chunk_latency_s.append(time.perf_counter() - t0)
        result.pending_history.append(list(pending))
        result.committed_history.append(list(committed))

    result.committed_tokens = committed
    result.text = parse_asr_output(
        tok.decode(committed, skip_special_tokens=True)
    )
    return result


# --------------------------------------------------------------------------
# metric computation
# --------------------------------------------------------------------------


def compute_rollback_hit_rate(
    pending_history: list[list[int]],
    committed_history: list[list[int]],
) -> float:
    """For each pending[i] list, measure what fraction became different in
    the next chunk's regenerated output.

    Approximation: after chunk i, pending[i] would have produced some tokens
    at positions after committed[i]. After chunk i+1, committed[i+1] contains
    regenerated tokens in those same positions. Compare token-by-token: count
    mismatches / len(pending[i]).
    """
    if len(pending_history) < 2:
        return 0.0
    total = 0
    mismatches = 0
    for i in range(len(pending_history) - 1):
        pending_i = pending_history[i]
        committed_i = committed_history[i]
        committed_next = committed_history[i + 1]
        # Regenerated tokens in chunk i+1 occupy positions
        # committed_i .. committed_next (the growth)
        regen = committed_next[len(committed_i) :]
        compare_len = min(len(pending_i), len(regen))
        for j in range(compare_len):
            total += 1
            if pending_i[j] != regen[j]:
                mismatches += 1
    return mismatches / total if total else 0.0


def prefix_stability_rate(
    committed_history: list[list[int]],
) -> float:
    """Fraction of committed tokens that were NEVER rewritten.

    Our append-only design never rewrites committed, so this should be 100%
    by construction. Included as a sanity check / in case future variants
    allow retroactive edits.
    """
    if not committed_history:
        return 1.0
    final = committed_history[-1]
    rewritten = 0
    total = 0
    for c in committed_history:
        # Each prior snapshot's tokens should match final[:len(c)]
        total += len(c)
        for j, t in enumerate(c):
            if j < len(final) and final[j] != t:
                rewritten += 1
    return 1.0 - (rewritten / total) if total else 1.0


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def main() -> None:
    print("=" * 70)
    print("Qwen3-ASR Streaming Spike")
    print("=" * 70)
    assert WAV_PATH.exists(), f"missing fixture: {WAV_PATH}"
    assert MODEL_DIR.exists(), f"missing model dir: {MODEL_DIR}"
    assert TOKENIZER_DIR.exists(), f"missing tokenizer dir: {TOKENIZER_DIR}"

    audio = load_wav(WAV_PATH)
    duration = len(audio) / SAMPLE_RATE
    print(f"\nAudio: {WAV_PATH.name}  duration={duration:.2f}s")
    print(f"Chunks (2s each): {len(chunk_audio(audio))}")

    print("\nLoading runner + tokenizer ...")
    t0 = time.perf_counter()
    runner = Qwen3ONNXRunner(MODEL_DIR)
    tok = Qwen3Tokenizer(TOKENIZER_DIR)
    print(f"  loaded in {time.perf_counter() - t0:.2f}s")
    print(
        f"  decoder: {runner.num_layers} layers, "
        f"max_total_len={runner.max_total_len}"
    )

    # -- baseline --
    print("\n" + "-" * 70)
    print("BASELINE (offline transcribe)")
    print("-" * 70)
    base_text, base_tokens, base_elapsed, n_af_offline = run_baseline(
        runner, tok, audio
    )
    print(f"  text: {base_text!r}")
    print(f"  tokens: {len(base_tokens)}  elapsed: {base_elapsed:.2f}s")
    print(f"  audio_features len (offline pad-to-30s): {n_af_offline}")

    # N_MAX for Path A: use offline's n_af exactly (= what model saw in
    # training and at inference). Padding zero-buffer to the same size keeps
    # the prompt schema identical to baseline.
    n_max_a = n_af_offline

    # -- Path A --
    print("\n" + "-" * 70)
    print(f"PATH A (pre-allocated zero buffer, N_MAX={n_max_a})")
    print("-" * 70)
    res_a = run_path_a(runner, tok, audio, n_max_a)
    ed_a = edit_distance(res_a.text, base_text)
    wer_a = ed_a / max(len(base_text), 1)
    psr_a = prefix_stability_rate(res_a.committed_history)
    rhr_a = compute_rollback_hit_rate(
        res_a.pending_history, res_a.committed_history
    )
    print(f"  text: {res_a.text!r}")
    print(f"  final edit distance: {ed_a} ({wer_a:.1%} of baseline)")
    print(f"  prefix stability rate: {psr_a:.1%}")
    print(f"  rollback hit rate: {rhr_a:.1%}")
    print(
        "  per-chunk latency (s): "
        + ", ".join(f"{x:.3f}" for x in res_a.per_chunk_latency_s)
    )
    if res_a.overflowed:
        print("  WARNING: audio_features buffer overflow")

    # -- Path E --
    print("\n" + "-" * 70)
    print("PATH E (prefix-cached re-prefill)")
    print("-" * 70)
    res_e = run_path_e(runner, tok, audio)
    ed_e = edit_distance(res_e.text, base_text)
    wer_e = ed_e / max(len(base_text), 1)
    psr_e = prefix_stability_rate(res_e.committed_history)
    rhr_e = compute_rollback_hit_rate(
        res_e.pending_history, res_e.committed_history
    )
    print(f"  text: {res_e.text!r}")
    print(f"  final edit distance: {ed_e} ({wer_e:.1%} of baseline)")
    print(f"  prefix stability rate: {psr_e:.1%}")
    print(f"  rollback hit rate: {rhr_e:.1%}")
    print(
        "  per-chunk latency (s): "
        + ", ".join(f"{x:.3f}" for x in res_e.per_chunk_latency_s)
    )
    if res_e.overflowed:
        print("  WARNING: KV cache overflow")

    # -- summary --
    print("\n" + "=" * 70)
    print("DECISION")
    print("=" * 70)
    print(
        f"  PATH A:  edit_dist={wer_a:.1%}  stability={psr_a:.1%}  "
        f"rollback_hit={rhr_a:.1%}  "
        f"max_latency={max(res_a.per_chunk_latency_s):.3f}s"
    )
    print(
        f"  PATH E:  edit_dist={wer_e:.1%}  stability={psr_e:.1%}  "
        f"rollback_hit={rhr_e:.1%}  "
        f"max_latency={max(res_e.per_chunk_latency_s):.3f}s"
    )
    print("\n  Decision rule:")
    print("    A acceptable iff edit_dist_A ≤ 5% AND stability_A ≥ 92%")
    if wer_a <= 0.05 and psr_a >= 0.92:
        print("  → Choose PATH A (cheapest, quality acceptable)")
    elif wer_e <= 0.05:
        print("  → Choose PATH E (A failed quality, E recovers it)")
    else:
        print("  → Both A and E fail quality threshold; revisit design")


if __name__ == "__main__":
    main()
