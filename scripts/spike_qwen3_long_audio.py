"""Spike (round 35): 跑 122s 真实长音频过 Qwen3-ASR 0.6B 流式管道,
量化:

  1. 真实 audio_features 速率(token/s)—— 决定 MAX_AUDIO_TOKENS
     是否对得上 ~56-70s 滑窗目标
  2. 真实 committed token 速率(token/s)—— 决定 MAX_COMMITTED_TOKENS
     是否够覆盖 ~80-130s 历史上下文
  3. 滑窗实际触发的 chunk 序号 + 触发后输出连贯性

跟 docs/35-流式滑窗/PLAN.md "阈值精调方式" 段对应。

Usage:
    uv run python scripts/spike_qwen3_long_audio.py

跑完直接打印每 chunk 的 audio token 数 / committed token 数 / 是否触发滑窗
/ 当前段 transcript。完事后人工读输出反推阈值是否合理。

One-shot tooling,SUMMARY.md 记录数字后此脚本可删。
"""

from __future__ import annotations

import logging
import sys
import wave
from pathlib import Path

import numpy as np

# 静音 _stream.py 的 debug 日志,免得 75KB 输出截断早期 chunk
logging.getLogger("daobidao").setLevel(logging.WARNING)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# 必须 sys.path.insert 之后再 import 项目代码,所以 E402 在这里是预期的
from daobidao.stt.base import STREAMING_CHUNK_SAMPLES  # noqa: E402
from daobidao.stt.qwen3 import Qwen3ASRSTT  # noqa: E402
from daobidao.stt.qwen3._stream import (  # noqa: E402
    MAX_AUDIO_TOKENS,
    MAX_COMMITTED_TOKENS,
    init_stream_state,
    stream_step,
)

WAV_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "zh_long.wav"


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000, f"need 16k, got {w.getframerate()}"
        assert w.getnchannels() == 1, f"need mono, got {w.getnchannels()}"
        n = w.getnframes()
        raw = w.readframes(n)
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio


def main():
    print("=== Round 35 long audio spike ===")
    print(f"WAV: {WAV_PATH}")
    print(f"MAX_AUDIO_TOKENS={MAX_AUDIO_TOKENS}")
    print(f"MAX_COMMITTED_TOKENS={MAX_COMMITTED_TOKENS}")
    print()

    audio = load_wav(WAV_PATH)
    duration_s = len(audio) / 16000.0
    print(f"Audio duration: {duration_s:.2f}s ({len(audio)} samples)")
    print()

    print("Loading Qwen3-ASR 0.6B (will use modelscope cache)...")
    stt = Qwen3ASRSTT(variant="0.6B")
    stt.load()
    print("Loaded.")
    print()

    state = init_stream_state(stt._runner, stt._tokenizer)

    # Slice into 2s chunks
    chunk_size = STREAMING_CHUNK_SAMPLES
    n_chunks = (len(audio) + chunk_size - 1) // chunk_size
    print(
        f"Feeding {n_chunks} chunks of {chunk_size} samples "
        f"({chunk_size / 16000:.1f}s each)..."
    )
    print()

    out_path = (
        Path(__file__).parent.parent / "tests" / "fixtures" / "_spike_table.txt"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_lines: list[str] = [
        f"{'chunk':>5} {'time(s)':>8} {'n_af':>6} {'committed':>9} "
        f"{'slid_af':>8} {'slid_cmt':>8}  delta",
        "-" * 80,
    ]

    audio_slid_first_chunk = None
    committed_slid_first_chunk = None

    for i in range(n_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, len(audio))
        is_last = i == n_chunks - 1
        chunk = audio[start:end]
        if chunk.size < chunk_size:
            # pad to full chunk so log_mel_spectrogram is happy
            chunk = np.concatenate(
                [
                    chunk,
                    np.zeros(chunk_size - chunk.size, dtype=np.float32),
                ]
            )

        evt = stream_step(
            state,
            chunk,
            is_last=is_last,
            runner=stt._runner,
            tokenizer=stt._tokenizer,
        )

        # 实际进 mid_ids 的 audio token 数 = state.audio_features_pieces 的总和
        # (滑窗后被折叠为单片 + 截到 MAX_AUDIO_TOKENS)
        post_pieces_total = sum(p.shape[1] for p in state.audio_features_pieces)
        committed_total = len(state.committed_tokens)

        # 滑窗触发判断:本 chunk 处理完后 post_pieces_total == MAX_AUDIO_TOKENS
        # 且 pre 已经接近(post == cap & pieces 折叠为单片)→ 滑窗已触发
        audio_slid = (
            post_pieces_total == MAX_AUDIO_TOKENS
            and len(state.audio_features_pieces) == 1
        )
        committed_slid = committed_total > MAX_COMMITTED_TOKENS

        if audio_slid and audio_slid_first_chunk is None:
            audio_slid_first_chunk = i
        if committed_slid and committed_slid_first_chunk is None:
            committed_slid_first_chunk = i

        delta = (evt.committed_delta or "")[:30]
        time_s = (i + 1) * (chunk_size / 16000)
        out_lines.append(
            f"{i:>5} {time_s:>8.1f} {post_pieces_total:>6} "
            f"{committed_total:>9} "
            f"{'YES' if audio_slid else '-':>8} "
            f"{'YES' if committed_slid else '-':>8}  "
            f"{delta!r}"
        )

    out_path.write_text("\n".join(out_lines), encoding="utf-8")
    print(f"Per-chunk table written to: {out_path}")

    print()
    print("=== Summary ===")
    print(f"Total chunks: {n_chunks}")
    print(f"Audio duration: {duration_s:.2f}s")
    print(
        f"Final audio_features token count (post-slide): "
        f"{sum(p.shape[1] for p in state.audio_features_pieces)}"
    )
    print(f"Final committed token count: {len(state.committed_tokens)}")
    print(f"Final committed_text length (chars): {len(state.committed_text)}")

    if audio_slid_first_chunk is not None:
        first_t = (audio_slid_first_chunk + 1) * (chunk_size / 16000)
        print(
            f"Audio slide first triggered at chunk #{audio_slid_first_chunk} "
            f"(~{first_t:.1f}s)"
        )
    else:
        print("Audio slide NEVER triggered in this 122s run.")

    if committed_slid_first_chunk is not None:
        first_t = (committed_slid_first_chunk + 1) * (chunk_size / 16000)
        print(
            f"Committed slide first triggered at chunk "
            f"#{committed_slid_first_chunk} (~{first_t:.1f}s)"
        )
    else:
        print("Committed slide NEVER triggered in this 122s run.")

    # 反推真实速率
    if audio_slid_first_chunk is None or audio_slid_first_chunk == 0:
        pre_slide_estimate_audio_per_sec = (
            sum(p.shape[1] for p in state.audio_features_pieces) / duration_s
        )
    else:
        pre_slide_estimate_audio_per_sec = MAX_AUDIO_TOKENS / (
            audio_slid_first_chunk * chunk_size / 16000
        )
    cmt_per_sec = len(state.committed_tokens) / duration_s
    print()
    print(f"实测 audio token 速率: ~{pre_slide_estimate_audio_per_sec:.2f} /s")
    print(f"实测 committed token 速率: ~{cmt_per_sec:.2f} /s")
    print()
    print("=== Final committed_text ===")
    print(state.committed_text)


if __name__ == "__main__":
    main()
