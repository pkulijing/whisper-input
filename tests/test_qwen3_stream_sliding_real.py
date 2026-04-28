"""真音频端到端测试:用 122s 中文长音频跑流式滑窗,验证 (round 35):

1. 跑完整段不抛 ``StreamingKVOverflowError``
2. audio_features 滑窗在中段(~54s 处)真触发,且最终 cap 在 MAX_AUDIO_TOKENS
3. committed_tokens 持续增长,prefill slice 永远 ≤ MAX_COMMITTED_TOKENS
4. 滑窗触发后下一段输出**仍然连贯**(包含目标关键词,字数大致对得上)

数据源:``tests/fixtures/zh_long.wav`` —— 用户朗读的近代史短文,16kHz mono PCM,
122.86s。spike (``scripts/spike_qwen3_long_audio.py``) 实测了 token 速率
(audio ~13/s, committed ~3.4/s),阈值 700/400 设计验证通过。

跟 ``test_qwen3_stream.py`` 的 FakeRunner 测试互补:那边测 Python 切片逻辑
正确,这边测真模型在滑窗触发后是否仍能产出连贯转录。
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from daobidao.stt.base import STREAMING_CHUNK_SAMPLES
from daobidao.stt.qwen3._stream import (
    MAX_AUDIO_TOKENS,
    MAX_COMMITTED_TOKENS,
    init_stream_state,
    stream_step,
)

# Round 37: DAOBIDAO_SKIP_E2E_STT 兜底已删,见 test_qwen3_asr.py 顶部。

WAV_PATH = Path(__file__).parent / "fixtures" / "zh_long.wav"


def _load_wav_mono16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        n = w.getnframes()
        raw = w.readframes(n)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def test_long_audio_sliding_window_end_to_end(stt_0_6b):
    """122s 真音频流式跑通 + 滑窗触发 + 转录连贯。

    用 0.6B(spike 验证过的 variant)。1.7B 同样应该跑通,但因为长 prompt
    数值不稳定问题更明显,不强制跑;手动可以改 fixture 验。
    """
    audio = _load_wav_mono16k(WAV_PATH)
    duration_s = len(audio) / 16000.0
    assert duration_s > 100, (
        f"fixture 应当 ≥100s 才能触发滑窗,实际 {duration_s:.1f}s"
    )

    runner = stt_0_6b._runner
    tokenizer = stt_0_6b._tokenizer
    state = init_stream_state(runner, tokenizer)

    chunk_size = STREAMING_CHUNK_SAMPLES
    n_chunks = (len(audio) + chunk_size - 1) // chunk_size

    audio_slid_first_chunk: int | None = None

    for i in range(n_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, len(audio))
        is_last = i == n_chunks - 1
        chunk = audio[start:end]
        if chunk.size < chunk_size:
            chunk = np.concatenate(
                [
                    chunk,
                    np.zeros(chunk_size - chunk.size, dtype=np.float32),
                ]
            )

        # 跑一步;若抛 StreamingKVOverflowError 直接挂测试(滑窗实现错了)
        stream_step(
            state,
            chunk,
            is_last=is_last,
            runner=runner,
            tokenizer=tokenizer,
        )

        # 检测 audio 滑窗触发:折叠为单片 + 截到 cap
        if (
            audio_slid_first_chunk is None
            and len(state.audio_features_pieces) == 1
            and state.audio_features_pieces[0].shape[1] == MAX_AUDIO_TOKENS
        ):
            audio_slid_first_chunk = i

    # === 断言 1: audio 滑窗触发了 ===
    assert audio_slid_first_chunk is not None, (
        "122s 音频应当在中段触发 audio 滑窗 —— "
        "如果没触发说明 audio token 速率比预期低很多"
    )
    # 触发时间应当在合理范围(~40-70s,留出测试不稳定空间)
    first_t_s = (audio_slid_first_chunk + 1) * (chunk_size / 16000)
    assert 40 < first_t_s < 70, (
        f"audio 滑窗触发在 {first_t_s:.1f}s,超出 40-70s 预期范围。"
        f"可能 audio token 速率偏离 spike 实测的 13/s 较多,需重测阈值"
    )

    # === 断言 2: 最终 audio_features 被 cap 住 ===
    final_n_af = sum(p.shape[1] for p in state.audio_features_pieces)
    assert final_n_af == MAX_AUDIO_TOKENS, (
        f"最终 audio_features token 数 {final_n_af} != "
        f"MAX_AUDIO_TOKENS={MAX_AUDIO_TOKENS}"
    )
    assert len(state.audio_features_pieces) == 1, (
        "audio 滑窗触发后 pieces 应折叠为单片"
    )

    # === 断言 3: committed_tokens 本体未被滑窗裁剪 ===
    # 122s 念稿子按实测 ~3.4/s 应当出 ~400+ committed token,会触发
    # committed 滑窗 → 但 state 本体不能被裁剪(切的是 prefill slice)
    final_committed = len(state.committed_tokens)
    assert final_committed > MAX_COMMITTED_TOKENS, (
        f"122s 念稿子应当 committed 涨过 cap={MAX_COMMITTED_TOKENS},"
        f"实际 {final_committed}。如果太低可能模型没正常输出"
    )

    # === 断言 4: 转录文本连贯且包含目标关键词 ===
    text = state.committed_text
    # 字数:~3.4 token/s × 122s × 中文 BPE 平均 1 token ≈ 1.5 字 ≈ 600+ 字
    # 给宽松下限避免 1 个 token 滑导致测试脆
    assert len(text) > 400, (
        f"转录文本太短({len(text)} 字,期望 > 400),"
        f"可能模型在某 chunk 早 EOS 或滑窗后崩了。文本头部:{text[:200]!r}"
    )
    # 关键词验证滑窗触发后语义仍连贯(这些词在 50s 之后才出现,在 audio 滑窗
    # 触发之后,所以能验证滑窗后输出质量)
    for keyword in ["太平天国", "鸦片战争", "新文化运动"]:
        assert keyword in text, (
            f"关键词 {keyword!r} 不在转录里 —— 滑窗后语义连贯性可能破了。"
            f"完整 text:{text}"
        )
