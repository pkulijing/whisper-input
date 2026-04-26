"""滑动窗口单测:Qwen3 流式状态机的长音频续航 (round 35)。

策略 E 的硬墙是 ``max_total_len=1200`` token,典型 35-80s 撞墙。本轮加滑窗:
audio_features 超 ``MAX_AUDIO_TOKENS`` 时折叠保留尾部、committed_tokens 超
``MAX_COMMITTED_TOKENS`` 时只截 prefill 用的 slice(state 本体不动)。

复用 test_qwen3_stream.py 的 FakeRunner / FakeTokenizer,但单独成文件,
避免对老用例造成意外干扰。
"""

from __future__ import annotations

from daobidao.stt.base import STREAMING_CHUNK_SAMPLES
from daobidao.stt.qwen3._stream import (
    MAX_AUDIO_TOKENS,
    MAX_COMMITTED_TOKENS,
    init_stream_state,
    stream_step,
)
from tests.test_qwen3_stream import FakeRunner, FakeTokenizer, _chunk_audio

# --------------------------------------------------------------------------
# Case A: audio 滑窗 —— audio_features 超阈值时折叠 + 截尾
# --------------------------------------------------------------------------


def test_audio_features_slide_when_exceeds_cap():
    """累积 audio_features 超 MAX_AUDIO_TOKENS 时:
    - 下一次 stream_step 喂给 decoder 的 af_len = MAX_AUDIO_TOKENS
    - state.audio_features_pieces 折叠为单元素(避免下次又 grow)
    - 不抛 StreamingKVOverflowError
    """
    marker = 900
    # 让每 chunk 生成 1 个 marker token + EOS,几乎不长 committed
    # 这样滑窗只在 audio 端触发
    per_chunk_gen = [marker, 0]
    preset = [300] + per_chunk_gen * 200  # 200 chunks 余量
    # 每 chunk 100 audio tokens,跑 8 chunk 累 800 > MAX_AUDIO_TOKENS=700
    runner = FakeRunner(preset_tokens=preset, audio_tokens_per_chunk=100)
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    # 跑 8 个 chunk:第 8 个 chunk 时累计 800 audio tokens > 700 → 必须滑窗
    for _ in range(8):
        stream_step(
            state,
            _chunk_audio(STREAMING_CHUNK_SAMPLES),
            is_last=False,
            runner=runner,
            tokenizer=tok,
        )

    # 滑窗后 state.audio_features_pieces 必须折叠
    assert len(state.audio_features_pieces) == 1, (
        "audio 滑窗触发后 audio_features_pieces 必须折叠为单片,"
        "否则下次 stream_step 又会从所有 pieces 重新 concat,白滑"
    )
    # 折叠后单片的 token 数 = MAX_AUDIO_TOKENS
    assert state.audio_features_pieces[0].shape[1] == MAX_AUDIO_TOKENS

    # 最后一次 decoder_step 调用时,af_len 应被截到 MAX_AUDIO_TOKENS
    # (decoder_step 的第一次调用是 init prefill,后面是 chunk prefill + greedy gen)
    last_prefill = [c for c in runner.decoder_calls if c["seq"] > 1][-1]
    assert last_prefill["af_len"] == MAX_AUDIO_TOKENS


# --------------------------------------------------------------------------
# Case B: committed 滑窗 —— prefill slice 截尾,state 本体不变
# --------------------------------------------------------------------------


def test_committed_slide_truncates_prefill_slice_only():
    """committed_tokens 超 MAX_COMMITTED_TOKENS 时:
    - mid_ids 中的 committed 段长度 <= MAX_COMMITTED_TOKENS
    - state.committed_tokens 本体长度未变(全部历史 token 都在)
    """
    tok = FakeTokenizer()
    runner = FakeRunner(preset_tokens=[300, 0], audio_tokens_per_chunk=5)
    state = init_stream_state(runner, tok)

    # 手工灌 committed 到超 cap
    fake_committed_len = MAX_COMMITTED_TOKENS + 50
    # marker (900) + 一堆假 transcript token(165 起,避开特殊 id)
    state.committed_tokens = [900] + [
        165 + (i % 60) for i in range(fake_committed_len - 1)
    ]
    original_len = len(state.committed_tokens)

    # 跑一次 stream_step 触发滑窗
    stream_step(
        state,
        _chunk_audio(STREAMING_CHUNK_SAMPLES),
        is_last=False,
        runner=runner,
        tokenizer=tok,
    )

    # state.committed_tokens 本体未变
    assert len(state.committed_tokens) >= original_len, (
        "滑窗不能裁掉 state.committed_tokens 本体 —— 它跟 committed_text "
        "强绑定,改了会让粘贴出去的字和 state 内部记录不一致"
    )

    # 找到 chunk-level prefill 调用(seq > 1 且 cur_len = chat_prefix_len)
    chat_prefix_len = len(state.chat_prefix_ids)
    prefill_calls = [
        c
        for c in runner.decoder_calls
        if c["seq"] > 1 and c["cur_len"] == chat_prefix_len
    ]
    assert prefill_calls, "应该有至少一次 chunk prefill"
    prefill = prefill_calls[-1]

    # mid_ids 长度 = audio_pad_count + chat_suffix_len + committed_for_prefill
    chat_suffix_len = len(state.chat_suffix_ids)
    audio_pad_count = prefill["af_len"]
    committed_in_prefill = prefill["seq"] - audio_pad_count - chat_suffix_len
    assert committed_in_prefill <= MAX_COMMITTED_TOKENS, (
        f"prefill 中 committed 段长度 {committed_in_prefill} "
        f"超过 MAX_COMMITTED_TOKENS={MAX_COMMITTED_TOKENS}"
    )


# --------------------------------------------------------------------------
# Case C: committed_text 在滑窗后仍然正确,无漏字 / 无重复
# --------------------------------------------------------------------------


def test_committed_text_remains_consistent_after_audio_slide():
    """audio 滑窗触发后,committed_text 仍然 = decode(committed_tokens) 的
    parse_asr_output 结果,无漏字 / 无重复。

    这是 case A 的延伸校验:滑窗不影响文本侧 invariant。
    """
    marker = 900
    # 每 chunk 生成 [marker(只在第一次), letter, letter, EOS]
    # 让 committed_text 持续增长,便于检查一致性
    chars = list(range(165, 170))  # ABCDE
    preset = [300]  # init prefill
    # chunk 1: marker + ABC + EOS
    preset += [marker, chars[0], chars[1], chars[2], 0]
    # chunk 2..10: 各生成 [chars[3], chars[4], EOS] → DE
    for _ in range(9):
        preset += [chars[3], chars[4], 0]

    runner = FakeRunner(preset_tokens=preset, audio_tokens_per_chunk=100)
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    deltas = []
    for _ in range(10):
        evt = stream_step(
            state,
            _chunk_audio(STREAMING_CHUNK_SAMPLES),
            is_last=False,
            runner=runner,
            tokenizer=tok,
        )
        deltas.append(evt.committed_delta)

    # 全程拼起来的 committed_delta 应该等于最终 committed_text
    # (无漏字、无重复)
    final_text = state.committed_text
    accumulated = "".join(deltas)
    assert accumulated == final_text, (
        f"committed_delta 累加 {accumulated!r} != "
        f"committed_text {final_text!r} —— 滑窗破坏了文本 invariant"
    )


# --------------------------------------------------------------------------
# Case D: 长 session 不撞墙
# --------------------------------------------------------------------------


def test_long_session_does_not_overflow_kv():
    """N=200 chunks 跑下来不抛 StreamingKVOverflowError。

    每 chunk 100 audio tokens(对应 ~10s 音频)+ 5 个 commit token,
    跑 200 chunk 等效 ~33 分钟音频。滑窗实现正确就永远不撞墙。

    没 catch 异常 —— 抛了直接让测试 fail。
    """
    marker = 900
    chars = list(range(170, 175))  # 5 个 commit token / chunk
    preset = [300, marker, *chars, 0]  # chunk 1 引入 marker
    for _ in range(200):
        preset += [*chars, 0]  # 后续每 chunk 5 commit + EOS

    # 注意:FakeTokenizer 把 special token 按字符 encode,导致 chat_prefix
    # 和 chat_suffix 比真 tokenizer 大 ~10 倍(~60+46 vs 真实 ~6+5)。这会
    # 让默认 max_total_len=1200 被 fake artifact 挤爆,跟真实生产路径无关。
    # 给 fake runner 加大预算,把测试焦点保留在"滑窗逻辑"而不是"预算计算"。
    runner = FakeRunner(
        preset_tokens=preset,
        audio_tokens_per_chunk=100,
        max_total_len=2000,
    )
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    for _ in range(200):
        stream_step(
            state,
            _chunk_audio(STREAMING_CHUNK_SAMPLES),
            is_last=False,
            runner=runner,
            tokenizer=tok,
        )

    # 跑完没炸,验证结束
    # 顺手断言 audio 仍被压在 cap 内
    assert state.audio_features_pieces[0].shape[1] <= MAX_AUDIO_TOKENS
