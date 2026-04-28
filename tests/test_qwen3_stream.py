"""纯逻辑单测:Qwen3 流式状态机 (stt/qwen3/_stream.py)。

策略:用 FakeRunner / FakeTokenizer 替换真 ONNX + 真 HF tokenizer,让我们
能精确控制"每一步 decoder 吐出哪个 token id、encoder 输出多长 audio
features",从而验证 rollback / commit 切分 / is_last flush / KV overflow
四条分支。

和 test_qwen3_asr.py:115-173 同一 mock 风格(monkeypatch 过 Qwen3ONNXRunner
/ Qwen3Tokenizer),避免任何磁盘 / 模型加载。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from daobidao.stt.base import (
    STREAMING_CHUNK_SAMPLES,
    StreamingKVOverflowError,
)
from daobidao.stt.qwen3 import _stream as stream_mod
from daobidao.stt.qwen3._stream import (
    MAX_NEW_TOKENS_PER_CHUNK,
    ROLLBACK_TOKENS,
    init_stream_state,
    stream_step,
)

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeRunner:
    """Mock of Qwen3ONNXRunner.

    - ``encode_audio(mel)`` returns a ``(1, audio_tokens_per_chunk,
      audio_feature_dim)`` float32 zero tensor.
    - ``decoder_step`` returns logits whose argmax on the last position is the
      next element of ``preset_tokens``. One call consumes one element.

    ``audio_feature_dim`` is settable so a回归用例可以注入 1.7B 的 2048
    断言 init_stream_state 用 runner 实际维度而不是硬编码 1024。
    """

    num_layers = 2
    kv_heads = 8
    head_dim = 128

    def __init__(
        self,
        preset_tokens: list[int],
        audio_tokens_per_chunk: int = 5,
        max_total_len: int = 1200,
        vocab_size: int = 1024,
        audio_feature_dim: int = 1024,
        eos_ids: tuple[int, ...] = (0,),
    ):
        self.preset_tokens = list(preset_tokens)
        self.call_idx = 0
        self.audio_tokens_per_chunk = audio_tokens_per_chunk
        self.max_total_len = max_total_len
        self.vocab_size = vocab_size
        self.audio_feature_dim = audio_feature_dim
        # Round 37: streaming 从 runner.eos_ids 拿 EOS,fake 也得有
        self.eos_ids = eos_ids
        self.encode_calls: list[tuple] = []
        self.decoder_calls: list[dict[str, Any]] = []

    def alloc_decoder_caches(self) -> list[np.ndarray]:
        return [
            np.zeros(
                (1, self.max_total_len, self.kv_heads, self.head_dim),
                dtype=np.float32,
            )
            for _ in range(2 * self.num_layers)
        ]

    def encode_audio(self, mel: np.ndarray) -> np.ndarray:
        self.encode_calls.append(mel.shape)
        return np.zeros(
            (1, self.audio_tokens_per_chunk, self.audio_feature_dim),
            dtype=np.float32,
        )

    def decoder_step(
        self,
        input_ids: np.ndarray,
        audio_features: np.ndarray,
        caches: list[np.ndarray],
        cur_len: int,
    ) -> np.ndarray:
        seq = input_ids.shape[1]
        if cur_len + seq > self.max_total_len:
            raise RuntimeError(
                f"KV cache overflow: cur_len={cur_len}+{seq} "
                f"> max_total_len={self.max_total_len}"
            )
        self.decoder_calls.append(
            {
                "seq": seq,
                "cur_len": cur_len,
                "af_len": audio_features.shape[1],
                "af_dim": audio_features.shape[2],
            }
        )
        logits = np.zeros((1, seq, self.vocab_size), dtype=np.float32)
        if self.call_idx < len(self.preset_tokens):
            next_tok = self.preset_tokens[self.call_idx]
        else:
            next_tok = 0  # fallback: EOS
        logits[0, -1, next_tok] = 10.0
        self.call_idx += 1
        return logits


class FakeTokenizer:
    """Simple deterministic tokenizer:
    - encode: chars → ord + 100
    - decode: reverse of above
    - audio_pad_id = 99
    - eos_id = 0
    """

    audio_pad_id = 99
    eos_id = 0
    # 900 = "<asr_text>" marker id;真 tokenizer 是 151704,这里挑一个不会跟
    # encode() 映射冲突的 id,方便在 preset 里精确注入。
    asr_text_id = 900

    def encode(self, text: str) -> list[int]:
        return [ord(c) + 100 for c in text]

    def decode(self, ids: list[int], skip_special_tokens: bool = True) -> str:
        parts = []
        for i in ids:
            if i == self.asr_text_id:
                # 特判 marker token → "<asr_text>" 字面,给 parse_asr_output 看
                parts.append("<asr_text>")
            elif 100 <= i < 100 + 0x110000 - 100:
                parts.append(chr(i - 100))
            # 其它 id(例如 0 = EOS,或 300 等 scaffold)丢弃
        return "".join(parts)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


# Audio chunk long enough to survive log_mel_spectrogram's reflect-pad gate
# (>= N_FFT // 2 + 1) — STREAMING_CHUNK_SAMPLES is 32000, plenty.
def _chunk_audio(samples: int) -> np.ndarray:
    return np.random.RandomState(0).randn(samples).astype(np.float32) * 0.01


# --------------------------------------------------------------------------
# init_stream_state
# --------------------------------------------------------------------------


def test_init_stream_state_prefills_chat_prefix():
    """init_stream_state 应该调用 decoder_step 一次为 chat_prefix 做 prefill。

    之后每 chunk 都不重做 chat_prefix 的 prefill,只重 prefill [audio_pad +
    suffix + committed] 这一中段。
    """
    runner = FakeRunner(preset_tokens=[0])
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    assert runner.call_idx == 1, "仅一次 chat_prefix prefill"
    assert state.chat_prefix_ids, "chat_prefix 有内容"
    assert state.committed_tokens == []
    assert state.pending_tokens == []
    assert state.audio_features_pieces == []


def test_init_requires_audio_pad_and_eos_ids():
    """tokenizer 缺 audio_pad_id / runner 缺 eos_ids 时 init 抛 RuntimeError。"""
    runner = FakeRunner(preset_tokens=[0])

    class BrokenTok(FakeTokenizer):
        audio_pad_id = None

    with pytest.raises(RuntimeError, match="audio_pad_id"):
        init_stream_state(runner, BrokenTok())

    runner_no_eos = FakeRunner(preset_tokens=[0], eos_ids=())
    with pytest.raises(RuntimeError, match="eos_ids"):
        init_stream_state(runner_no_eos, FakeTokenizer())


def test_init_stream_state_passes_runner_audio_feature_dim():
    """Regression (round 30): init_stream_state 喂给 decoder 的 dummy
    audio_features 必须用 ``runner.audio_feature_dim``,不能写死 1024。

    1.7B 模型 encoder 输出 dim = 2048,旧版 dummy_af = (1, 1, 1024) 会让
    onnxruntime 直接 shape mismatch,导致流式 init 挂掉。
    """
    runner = FakeRunner(preset_tokens=[0], audio_feature_dim=2048)
    tok = FakeTokenizer()

    init_stream_state(runner, tok)

    # init 阶段只调一次 decoder_step (chat_prefix prefill)
    assert len(runner.decoder_calls) == 1
    assert runner.decoder_calls[0]["af_dim"] == 2048, (
        "dummy_af 必须按 runner.audio_feature_dim(2048 for 1.7B);"
        "若仍是 1024,说明 _stream.py 又退回硬编码"
    )


# --------------------------------------------------------------------------
# accumulate-below-threshold
# --------------------------------------------------------------------------


def test_stream_step_accumulates_below_threshold():
    """不足 2s 的 chunk + not is_last → 返回空 StreamEvent,不调 encoder。"""
    runner = FakeRunner(preset_tokens=[0])
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)
    prior_call_idx = runner.call_idx
    prior_encode_calls = len(runner.encode_calls)

    tiny = _chunk_audio(STREAMING_CHUNK_SAMPLES // 4)
    evt = stream_step(state, tiny, is_last=False, runner=runner, tokenizer=tok)

    assert evt.committed_delta == ""
    assert evt.pending_text == ""
    assert evt.is_final is False
    # 没有调到 encoder 也没有新的 decoder_step
    assert len(runner.encode_calls) == prior_encode_calls
    assert runner.call_idx == prior_call_idx


# --------------------------------------------------------------------------
# is_last with no / tiny audio
# --------------------------------------------------------------------------


def test_stream_step_is_last_with_no_audio_flushes_pending():
    """is_last=True 且几乎无音频 → 不跑 encoder,只把 pending 并入 committed。"""
    runner = FakeRunner(preset_tokens=[0])
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)
    # id=165..167 对应 chr(65..67)="ABC"(可见 ASCII,不会被 strip 吞)
    state.pending_tokens = [165, 166, 167]

    evt = stream_step(
        state,
        np.zeros(0, dtype=np.float32),
        is_last=True,
        runner=runner,
        tokenizer=tok,
    )

    assert evt.is_final is True
    assert state.committed_tokens == [165, 166, 167]
    assert state.pending_tokens == []
    assert evt.committed_delta == "ABC"


# --------------------------------------------------------------------------
# rollback 行为 —— 精确 token 序列验证
# --------------------------------------------------------------------------


def test_stream_step_rollback_splits_committed_and_pending():
    """单 chunk 生成 > ROLLBACK_TOKENS,应切 committed / pending。

    Preset: chat_prefix prefill 吃 1 个(call 0),chunk 1 prefill mid 再吃
    1 个(call 1),之后贪心喂每次一个 token。第 13 次调 decoder (索引 12)
    返回 EOS,前 12 个 token 是生成的文本。

    ROLLBACK_TOKENS=10 → commit 前 2,pending 后 10。
    """
    # idx: 0(init prefill), 1(chunk1 prefill), 2..13(gen), 14(EOS)
    #      ↑ call_0 unused     ↑ gen starts        ↑ EOS at gen iter 13
    # 模拟模型生成 [<asr_text>, A..K] 共 12 tokens,marker 在位置 0
    marker = 900
    preset = [300, marker, *range(165, 176), 0]
    runner = FakeRunner(preset_tokens=preset, audio_tokens_per_chunk=5)
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    evt = stream_step(
        state,
        _chunk_audio(STREAMING_CHUNK_SAMPLES),
        is_last=False,
        runner=runner,
        tokenizer=tok,
    )

    # 12 个生成 token = [marker, 165..175]
    # first_marker 分支:marker 位置 0,post_marker 11 个,tail=min(11,3)=3
    # commit_up_to = 12 - 3 = 9:commit [marker, 165..172] = marker + ABCDEFGH
    # pending 后 3 = [173, 174, 175] = IJK
    assert state.committed_tokens == [
        900,
        165,
        166,
        167,
        168,
        169,
        170,
        171,
        172,
    ]
    assert state.pending_tokens == [173, 174, 175]
    # parse_asr_output 会 rfind marker 取之后部分:"ABCDEFGH"
    assert evt.committed_delta == "ABCDEFGH"
    assert evt.is_final is False


def test_stream_step_rollback_regenerates_pending_each_chunk():
    """下一 chunk 把上一 chunk 的 pending 全部丢弃,重新生成。

    断言:chunk 2 的 committed 只包含 chunk 2 自己 prefix 出来的 token
    开头部分,不是 chunk 1 pending 的副本(那些在 prefill 时作为 kept_prefix
    已经进入 input_ids 并被回写了 KV)。
    """
    # chunk 1: 12 gen tokens [marker, 165..175] then EOS
    #   first_marker: commit 9 tokens ([marker, 165..172]), pending 3 ([173,174,175])
    # chunk 2 (is_last=True): 3 gen tokens [185, 186, 187] then EOS → 全部 commit
    marker = 900
    preset = [
        300,  # init prefill (unused)
        marker,
        *range(165, 176),
        0,  # chunk 1: marker + 11 content + EOS
        185,
        186,
        187,
        0,  # chunk 2: 3 gen + EOS
    ]
    runner = FakeRunner(preset_tokens=preset, audio_tokens_per_chunk=5)
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    stream_step(
        state,
        _chunk_audio(STREAMING_CHUNK_SAMPLES),
        is_last=False,
        runner=runner,
        tokenizer=tok,
    )
    evt2 = stream_step(
        state,
        _chunk_audio(STREAMING_CHUNK_SAMPLES),
        is_last=True,
        runner=runner,
        tokenizer=tok,
    )

    # chunk 2 是 is_last,所以 new_gen 全部进 committed
    # final committed = [marker, 165..172] (from chunk 1) + [185, 186, 187] (chunk 2)
    assert state.committed_tokens == [
        900,
        165,
        166,
        167,
        168,
        169,
        170,
        171,
        172,
        185,
        186,
        187,
    ]
    assert state.pending_tokens == []
    assert evt2.is_final is True
    # parse_asr_output 取 marker 后全部:"ABCDEFGHUVW"
    # delta = 增量 "ABCDEFGH" → "ABCDEFGHUVW" = "UVW"
    assert evt2.committed_delta == "UVW"


def test_stream_step_prefill_includes_audio_pads_and_committed():
    """chunk 2 的 prefill input 应该是 audio_pad*N + chat_suffix + committed。

    断言 decoder_step 在 chunk 2 的 prefill 调用里,seq 长度 = 总 audio token
    数 + chat_suffix 长度 + committed 长度。
    """
    # 简化:chunk 1 生成少量 token,commit 0 / pending 1(< ROLLBACK)
    # Preset: init(1) + chunk1 prefill(1) + 1 gen + EOS + chunk2 prefill(1) + 1 gen + EOS
    preset = [
        300,
        165,
        0,  # chunk 1: 1 gen, EOS → pending=[165], committed=[]
        185,
        0,  # chunk 2
    ]
    runner = FakeRunner(preset_tokens=preset, audio_tokens_per_chunk=5)
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    stream_step(
        state,
        _chunk_audio(STREAMING_CHUNK_SAMPLES),
        is_last=False,
        runner=runner,
        tokenizer=tok,
    )
    # len(new_generated)=1 <= ROLLBACK_TOKENS → pending=[165]
    assert state.pending_tokens == [165]
    assert state.committed_tokens == []

    # chunk 2: pending 被丢弃,prefill 传的 kept_prefix = committed_tokens = []
    # 所以 mid_ids = audio_pad * 10 (2 chunks * 5) + suffix + []
    stream_step(
        state,
        _chunk_audio(STREAMING_CHUNK_SAMPLES),
        is_last=True,
        runner=runner,
        tokenizer=tok,
    )

    # 验证 chunk 2 的 prefill 调用(decoder_calls 里的第 3 个,idx=2,
    # 因为 idx=0 是 init prefill,idx=1 是 chunk 1 prefill,idx=2 chunk 1 gen EOS call,
    # 实际要算清:init 1次 + chunk1 prefill 1次 + chunk1 gen 1次(生成 110 后再喂它得到 EOS 逻辑)
    # = 3 次。chunk 2 prefill 是第 4 次 decoder_step,idx=3。
    chunk2_prefill_seq = runner.decoder_calls[3]["seq"]
    # n_af = 2 * 5 = 10, chat_suffix_ids 长度,committed=0
    expected_min = 10  # 至少 10 个 audio_pad
    assert chunk2_prefill_seq >= expected_min


# --------------------------------------------------------------------------
# KV overflow
# --------------------------------------------------------------------------


def test_stream_step_raises_on_kv_overflow():
    """max_total_len 调得很小 → 流式到某 chunk 会抛 StreamingKVOverflowError。"""
    # max_total_len 小到只够 chat_prefix prefill + 很少的 mid
    # audio_tokens_per_chunk=50, chat_suffix ~5 tokens
    # prefix(~6) + 50 + 5 = 61; +MAX_NEW_TOKENS_PER_CHUNK=32 → need 93
    # 设 max_total_len=70 → 第一次 chunk prefill 就应 overflow
    preset = [300] + [165] * 40 + [0]
    runner = FakeRunner(
        preset_tokens=preset,
        audio_tokens_per_chunk=50,
        max_total_len=70,
    )
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    with pytest.raises(StreamingKVOverflowError):
        stream_step(
            state,
            _chunk_audio(STREAMING_CHUNK_SAMPLES),
            is_last=False,
            runner=runner,
            tokenizer=tok,
        )


# --------------------------------------------------------------------------
# 非法输入
# --------------------------------------------------------------------------


def test_stream_step_rejects_2d_audio():
    runner = FakeRunner(preset_tokens=[0])
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    with pytest.raises(ValueError, match="1D audio"):
        stream_step(
            state,
            np.zeros((2, 16000), dtype=np.float32),
            is_last=False,
            runner=runner,
            tokenizer=tok,
        )


# --------------------------------------------------------------------------
# is_last without pending / with audio remaining
# --------------------------------------------------------------------------


def test_stream_step_is_last_flushes_all_new_generated():
    """is_last=True 且 > rollback 个新 token 生成,应全部 commit 不保留 pending。"""
    preset = [300, *range(165, 180), 0]  # 15 gen tokens + EOS
    runner = FakeRunner(preset_tokens=preset, audio_tokens_per_chunk=5)
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    evt = stream_step(
        state,
        _chunk_audio(STREAMING_CHUNK_SAMPLES),
        is_last=True,
        runner=runner,
        tokenizer=tok,
    )

    # is_last → 全部 15 个 token 都 commit,pending 清空
    assert state.committed_tokens == list(range(165, 180))
    assert state.pending_tokens == []
    assert evt.is_final is True


# --------------------------------------------------------------------------
# MAX_NEW_TOKENS_PER_CHUNK cap
# --------------------------------------------------------------------------


def test_stream_step_caps_at_max_new_tokens_per_chunk():
    """模型生成远超 MAX_NEW_TOKENS_PER_CHUNK 时,应在上限处截断,剩下的
    进入 rollback / 下个 chunk 再决定。
    """
    # 让 preset 吐出大量非 EOS token(比 MAX_NEW_TOKENS_PER_CHUNK 多很多),
    # 不含 EOS → 跑到 MAX_NEW_TOKENS_PER_CHUNK 会停
    marker = 900
    # marker + 31 个相同内容 token (总共 32 = MAX_NEW_TOKENS_PER_CHUNK) 然后 EOS
    preset = [300, marker, *([165] * (MAX_NEW_TOKENS_PER_CHUNK - 1)), 0]
    runner = FakeRunner(preset_tokens=preset, audio_tokens_per_chunk=5)
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    stream_step(
        state,
        _chunk_audio(STREAMING_CHUNK_SAMPLES),
        is_last=False,
        runner=runner,
        tokenizer=tok,
    )

    # first_marker: marker 在位置 0,post_marker = 31 个 content token
    # tail = min(31, ROLLBACK_TOKENS) = ROLLBACK_TOKENS
    # commit_up_to = MAX_NEW_TOKENS_PER_CHUNK - ROLLBACK_TOKENS
    expected_committed_len = MAX_NEW_TOKENS_PER_CHUNK - ROLLBACK_TOKENS
    assert len(state.committed_tokens) == expected_committed_len
    assert len(state.pending_tokens) == ROLLBACK_TOKENS


# --------------------------------------------------------------------------
# committed_delta 跟 committed_text 的一致性
# --------------------------------------------------------------------------


def test_stream_step_committed_text_diff_gives_delta():
    """committed_delta 必须 = 新 committed_text 相对旧 committed_text 的尾部差。

    多 chunk 之后,delta 连起来 = 整个 committed_text。
    """
    marker = 900
    preset = [
        300,
        marker,
        *range(165, 176),
        0,  # chunk 1: marker + 11 content + EOS
        *range(185, 195),
        0,  # chunk 2: 10 content (过 marker) + EOS
        200,
        201,
        0,  # chunk 3 is_last: 2 + EOS
    ]
    runner = FakeRunner(preset_tokens=preset, audio_tokens_per_chunk=5)
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    deltas = []
    for i in range(3):
        is_last = i == 2
        evt = stream_step(
            state,
            _chunk_audio(STREAMING_CHUNK_SAMPLES),
            is_last=is_last,
            runner=runner,
            tokenizer=tok,
        )
        deltas.append(evt.committed_delta)

    # 拼接 deltas = 最终 committed_text
    assert "".join(deltas) == state.committed_text
    # committed_text = parse_asr_output(tokenizer.decode(committed_tokens))
    # —— 即去掉 "<asr_text>" marker 之前 scaffolding 之后的部分
    from daobidao.stt.qwen3._postprocess import parse_asr_output

    expected = parse_asr_output(
        tok.decode(state.committed_tokens, skip_special_tokens=True)
    )
    assert state.committed_text == expected


# --------------------------------------------------------------------------
# encode 只跑新 chunk 不跑累积 buffer
# --------------------------------------------------------------------------


def test_stream_step_encoder_incremental_per_chunk():
    """每次 stream_step 只对新 chunk 跑 1 次 encode_audio,不重编码历史。

    这是"跟伪流式划清界线"的核心证据:encode_calls 数 = chunk 数。
    """
    preset = [200, 110, 0, 130, 0, 150, 0]
    runner = FakeRunner(preset_tokens=preset, audio_tokens_per_chunk=5)
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    for i in range(3):
        stream_step(
            state,
            _chunk_audio(STREAMING_CHUNK_SAMPLES),
            is_last=(i == 2),
            runner=runner,
            tokenizer=tok,
        )

    assert len(runner.encode_calls) == 3


# --------------------------------------------------------------------------
# stream module exposes exception and API
# --------------------------------------------------------------------------


def test_stream_step_converts_non_float32_audio():
    """传 float64 / int16 audio 应被自动转 float32。"""
    preset = [300, 165, 0]
    runner = FakeRunner(preset_tokens=preset, audio_tokens_per_chunk=5)
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)

    float64_chunk = _chunk_audio(STREAMING_CHUNK_SAMPLES).astype(np.float64)
    evt = stream_step(
        state,
        float64_chunk,
        is_last=False,
        runner=runner,
        tokenizer=tok,
    )
    assert evt is not None  # 没崩就算过


def test_finalize_empty_no_pending_returns_empty_delta():
    """is_last=True 无音频 + 无 pending → committed_delta 为空,仍返回 is_final=True。"""
    runner = FakeRunner(preset_tokens=[0])
    tok = FakeTokenizer()
    state = init_stream_state(runner, tok)
    # 无 pending,无音频
    evt = stream_step(
        state,
        np.zeros(0, dtype=np.float32),
        is_last=True,
        runner=runner,
        tokenizer=tok,
    )
    assert evt.is_final is True
    assert evt.committed_delta == ""


def test_stream_module_api_surface():
    """快速断言公共 API 存在,防止将来重构把名字改了还没人发现。"""
    assert callable(stream_mod.init_stream_state)
    assert callable(stream_mod.stream_step)
    # Constants
    assert stream_mod.ROLLBACK_TOKENS == 3
    assert stream_mod.MAX_NEW_TOKENS_PER_CHUNK == 32
    # 把 overflow 异常重新 export 也行(但主 canonical 在 stt.base)
    assert StreamingKVOverflowError is stream_mod.StreamingKVOverflowError
