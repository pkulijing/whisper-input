"""Benchmark 测量 harness —— 跑 transcribe + 计时 + median 聚合。

跟具体 backend 解耦:吃 ``Backend`` 协议(``backends/base.py``)+ tokenizer +
audio,产出一份 record dict。多次跑 → median。
"""

from __future__ import annotations

import statistics
import time
from typing import Any

import numpy as np

from benchmarks.backends.base import Backend
from benchmarks.config import MAX_NEW_TOKENS, N_REPEATS
from benchmarks.fixtures import AudioFixture, load_audio
from daobidao.stt.qwen3._postprocess import parse_asr_output
from daobidao.stt.qwen3._prompt import build_prompt


def transcribe_once(
    audio: np.ndarray,
    backend: Backend,
    tokenizer: Any,
    max_new_tokens: int = MAX_NEW_TOKENS,
) -> tuple[str, list[int], dict[str, Any]]:
    """单次 transcribe + 计时。返回 (text, generated_token_ids, timings)。

    Timings 字段:encode_s / prefill_s / decode_s / total_s / n_audio_tokens /
    generated_count。total_s = encode + prefill + decode,**不**含 tokenizer
    decode + 后处理(它们 < 1ms,跨 backend 一致,排除掉降噪)。
    """
    t0 = time.perf_counter()
    af = backend.encode_audio(audio)
    t_encode = time.perf_counter() - t0

    n_audio = int(af.shape[-2])
    prompt = build_prompt(n_audio)
    prompt_ids = tokenizer.encode(prompt)
    input_ids = np.array([prompt_ids], dtype=np.int64)

    caches = backend.alloc_caches()
    cur_len = 0
    seq = input_ids.shape[1]
    cache_position = np.arange(cur_len, cur_len + seq, dtype=np.int64)

    t1 = time.perf_counter()
    logits = backend.decoder_step(input_ids, af, caches, cache_position)
    t_prefill = time.perf_counter() - t1
    cur_len += seq

    generated: list[int] = []
    t2 = time.perf_counter()
    for _ in range(max_new_tokens):
        nid = int(np.argmax(logits[0, -1]))
        if nid in backend.eos_ids:
            break
        generated.append(nid)
        next_in = np.array([[nid]], dtype=np.int64)
        cache_position = np.array([cur_len], dtype=np.int64)
        logits = backend.decoder_step(next_in, af, caches, cache_position)
        cur_len += 1
    t_decode = time.perf_counter() - t2

    text = parse_asr_output(
        tokenizer.decode(generated, skip_special_tokens=True)
    )
    timings = {
        "encode_s": t_encode,
        "prefill_s": t_prefill,
        "decode_s": t_decode,
        "total_s": t_encode + t_prefill + t_decode,
        "n_audio_tokens": n_audio,
        "generated_count": len(generated),
    }
    return text, generated, timings


def measure_case(
    backend: Backend,
    fixture: AudioFixture,
    n_repeats: int = N_REPEATS,
    n_warmup: int = 1,
) -> dict[str, Any]:
    """warmup ``n_warmup`` 次 + 正式 ``n_repeats`` 次,取 median + 报 std/min/max。"""
    tokenizer = getattr(backend, "tokenizer", None)
    if tokenizer is None:
        raise RuntimeError(
            f"backend {backend.name} 没暴露 .tokenizer,harness 没法 build prompt"
        )

    audio = load_audio(fixture)
    audio_seconds = fixture.seconds
    case_id = f"{backend.name}/{fixture.slug}"
    print(f"\n  [{case_id}] audio_seconds={audio_seconds:.2f}")

    last_text = ""
    last_gen = 0
    for w in range(n_warmup):
        text_w, _, t_w = transcribe_once(audio, backend, tokenizer)
        last_text = text_w
        last_gen = t_w["generated_count"]
        print(
            f"    warmup {w + 1}/{n_warmup}: total={t_w['total_s']:.2f}s "
            f"gen={t_w['generated_count']} text={text_w[:30]!r}…"
        )

    runs: list[dict[str, Any]] = []
    for r in range(n_repeats):
        text, _, t = transcribe_once(audio, backend, tokenizer)
        runs.append(t)
        last_text = text
        last_gen = t["generated_count"]
        print(
            f"    run {r + 1}: encode={t['encode_s']:.2f}s "
            f"prefill={t['prefill_s']:.2f}s decode={t['decode_s']:.2f}s "
            f"total={t['total_s']:.2f}s rtf={t['total_s'] / audio_seconds:.2f}"
        )

    def med(field: str) -> float:
        return float(statistics.median(r[field] for r in runs))

    def stats(field: str) -> dict[str, float]:
        vals = [r[field] for r in runs]
        out: dict[str, float] = {
            "min": float(min(vals)),
            "max": float(max(vals)),
            "median": float(statistics.median(vals)),
        }
        out["std"] = float(statistics.stdev(vals)) if len(vals) > 1 else 0.0
        out["cv"] = (out["std"] / out["median"]) if out["median"] > 0 else 0.0
        return out

    encode_s = med("encode_s")
    prefill_s = med("prefill_s")
    decode_s = med("decode_s")
    total_s = med("total_s")
    total_stats = stats("total_s")
    rtf = total_s / audio_seconds

    # 翻车判定:gen=0 / 文本空 / RTF 离谱大。这里只是给 reporting 一个 status
    # hint,不 abort —— 让所有 case 跑完后人工读报告
    status = "PASS"
    if last_gen <= 3 or not last_text:
        status = "FAIL"  # issue #7 翻车谱命中
    elif rtf > 5.0:
        status = "SLOW"

    return {
        "case_id": case_id,
        "backend_name": backend.name,
        "family": backend.family,
        "variant": backend.variant,
        "quant": backend.quant,
        "fixture_slug": fixture.slug,
        "audio_seconds": audio_seconds,
        "n_audio_tokens": runs[0]["n_audio_tokens"],
        "generated_count": last_gen,
        "encode_s": encode_s,
        "prefill_s": prefill_s,
        "decode_s": decode_s,
        "total_s": total_s,
        "total_min_s": total_stats["min"],
        "total_max_s": total_stats["max"],
        "total_std_s": total_stats["std"],
        "total_cv": total_stats["cv"],
        "rtf": rtf,
        "text": last_text,
        "all_runs": runs,
        "status": status,
    }
