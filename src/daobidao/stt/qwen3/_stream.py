"""Qwen3-ASR 流式识别状态机 —— 策略 E (prefix-cached re-prefill)。

Spike (docs/28-Qwen3-ASR流式识别/) 验证过:
  - 策略 A(预分配零 buffer + 一次 prompt prefill)在 zh.wav 上直接输出空字符串
    (staleness 灾难,比 Plan agent 预测的 10-40% 退化严重得多)
  - 策略 E(chat prefix KV 永久缓存,audio_pad + 后缀 + committed 每 chunk 重
    prefill)流式结果 = 离线 baseline,字级 edit distance = 0

本模块实现策略 E,对外只暴露:
  - ``Qwen3StreamState`` —— 引擎私有状态 (opaque dataclass)
  - ``init_stream_state(runner, tokenizer) -> Qwen3StreamState``
  - ``stream_step(state, audio_chunk, is_last, *, runner, tokenizer) -> StreamEvent``
  - ``StreamingKVOverflowError`` —— KV cache 容量不够时抛出

PROMPT 里禁止的"伪流式"是"每 chunk 从头重跑 encoder + 从头 decode"。策略 E
的 encoder 仍然是增量(只对新 chunk 跑 encode_audio),decoder 的 chat prefix
KV 永久缓存不重跑,只有 `[audio_pad * N_k + chat_suffix + committed]` 这段做
批量 prefill 刷新 cross-attn。随着 chunk 增长 prefill 成本 O(K²) 累积,但
batched prefill 比逐 token 生成快 10-20×,实际 CPU 开销在 30s 以内可接受
(spike 实测 10.6s 音频最后 chunk 延迟 543ms)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from daobidao.logger import get_logger
from daobidao.stt.base import (
    STREAMING_CHUNK_SAMPLES,
    STREAMING_CHUNK_SEC,
    StreamEvent,
    StreamingKVOverflowError,
)
from daobidao.stt.qwen3._feature import (
    N_FFT,
    log_mel_spectrogram,
)
from daobidao.stt.qwen3._onnx_runner import Qwen3ONNXRunner
from daobidao.stt.qwen3._postprocess import parse_asr_output
from daobidao.stt.qwen3._prompt import (
    AUDIO_END,
    AUDIO_START,
    IM_END,
    IM_START,
)
from daobidao.stt.qwen3._tokenizer import Qwen3Tokenizer

logger = get_logger(__name__)

# --------------------------------------------------------------------------
# Streaming knobs (常量,本轮不暴露到 UI)
# --------------------------------------------------------------------------

# CHUNK_SIZE_SEC / CHUNK_SIZE_SAMPLES 在 stt.base 里定义,编排层也要用。
# 下面两个别名是为了本模块内部代码可读性,保持跟旧名一致。
CHUNK_SIZE_SEC = STREAMING_CHUNK_SEC
CHUNK_SIZE_SAMPLES = STREAMING_CHUNK_SAMPLES

ROLLBACK_TOKENS = 3
"""每步生成后保留为 pending 的尾部 token 数(marker 之后的 transcript 尾巴)。
下一 chunk 会把整段 pending 丢弃重新生成,让最近的决策能根据新音频纠正。

调优历程:
- Plan agent 最初估中文 BPE + 模型决策延迟下 5 不够,建议 10
- 28 轮上线发现 10 太保守:真机需要说 ~13+ 个 transcript token 才能触发第一次
  commit(rollback=10 意味着新生成的内容 token 至少要超过 10 才会有剩余溢出
  committed),感受上"说了好多话才出一次字"
- Spike 实测 zh.wav rollback 命中率 0% —— 干净朗读下模型决策稳定,rollback
  其实是"保险而非救命药",留小一点更符合流式体验
- 最终定 3:对应 ~1-2 个汉字的尾部缓冲,边界决策还有修正机会,但不至于
  把整段 transcript 都憋着不贴
"""

MAX_NEW_TOKENS_PER_CHUNK = 32
"""每步自回归生成的硬上限,防止模型在某 chunk 停不下来。"""

# log_mel_spectrogram 最小可接受长度:reflect pad 需要 audio >= N_FFT // 2
_MIN_CHUNK_SAMPLES_FOR_MEL = N_FFT // 2 + 1


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


@dataclass
class Qwen3StreamState:
    """一次"按键→说话→松手"周期的私有状态。由 :func:`init_stream_state` 创建。"""

    # --- prompt 结构 ---
    chat_prefix_ids: list[int]
    chat_suffix_ids: list[int]
    audio_pad_id: int
    eos_id: int

    # --- decoder KV cache ---
    caches: list[np.ndarray]
    """Layer-wise KV cache,长度 2*num_layers,由 :meth:`Qwen3ONNXRunner.alloc_decoder_caches` 分配。"""

    # --- 音频累积 ---
    audio_features_pieces: list[np.ndarray] = field(default_factory=list)
    """每 chunk 新 encode 出的 audio_features,长度 = chunk 数。
    调 decoder 时 ``np.concatenate(pieces, axis=1)``。"""

    pending_audio: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float32)
    )
    """积累但不够 ``CHUNK_SIZE_SAMPLES`` 的尾巴音频,等下次再处理。"""

    total_audio_samples: int = 0
    """累计接收到的音频 sample 数(含 pending_audio + 已处理 chunks)。
    用于编排层判断是否接近 KV cache 上限。"""

    # --- 文本状态 ---
    committed_tokens: list[int] = field(default_factory=list)
    """已 commit 的文本 token(不含 prompt / 特殊 token)。
    编排层通过 :class:`StreamEvent` 的 ``committed_delta`` 看到新增。"""

    pending_tokens: list[int] = field(default_factory=list)
    """最近一步生成但未 commit 的尾部 token,下一 chunk 全部丢弃重做。"""

    committed_text: str = ""
    """``tokenizer.decode(committed_tokens)`` 的累积结果。每步 diff 出
    ``committed_delta`` 供粘贴。单独维护是因为按 token 切片解码会在多字节
    字符边界产生替换字符 / 截断 bug(中文 UTF-8 多字节)。"""

    chunk_count: int = 0
    """已处理的 chunk 数(仅做诊断 / logging 用)。"""


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def init_stream_state(
    runner: Qwen3ONNXRunner, tokenizer: Qwen3Tokenizer
) -> Qwen3StreamState:
    """初始化一次流式周期:prefill chat prefix 到 KV cache。

    chat prefix = ``<|im_start|>system\n<|im_end|>\n<|im_start|>user\n<|audio_start|>``
    (6 个 token),这段 KV 整个 stream 期间保持不变,每 chunk 只重 prefill
    其后的 ``[audio_pad * N_k + chat_suffix + committed]``。

    audio_pad_id / eos_id 等常量也在这里一次性解析,避免 stream_step 每次
    再查。
    """
    chat_prefix = (
        f"{IM_START}system\n{IM_END}\n"
        f"{IM_START}user\n{AUDIO_START}"
    )
    chat_suffix = f"{AUDIO_END}{IM_END}\n{IM_START}assistant\n"

    chat_prefix_ids = tokenizer.encode(chat_prefix)
    chat_suffix_ids = tokenizer.encode(chat_suffix)

    audio_pad_id = tokenizer.audio_pad_id
    eos_id = tokenizer.eos_id
    if audio_pad_id is None or eos_id is None:
        raise RuntimeError(
            "tokenizer 缺 audio_pad_id / eos_id,无法启动流式"
        )

    caches = runner.alloc_decoder_caches()

    # 初始 prefill: chat prefix 在 cur_len=0 处写入 KV。
    # 这几个 token 都是 chat template(不是 audio_pad),它们的 cross-attn
    # 输出值对整体生成影响小,用 1 slot 的零 audio_features 作为 dummy。
    # last dim 必须匹配 runner 当前 variant 的 encoder hidden:
    # 0.6B = 1024, 1.7B = 2048。
    dummy_af = np.zeros(
        (1, 1, runner.audio_feature_dim), dtype=np.float32
    )
    runner.decoder_step(
        np.array([chat_prefix_ids], dtype=np.int64),
        dummy_af,
        caches,
        0,
    )

    return Qwen3StreamState(
        chat_prefix_ids=chat_prefix_ids,
        chat_suffix_ids=chat_suffix_ids,
        audio_pad_id=audio_pad_id,
        eos_id=eos_id,
        caches=caches,
    )


def stream_step(
    state: Qwen3StreamState,
    audio_chunk: np.ndarray,
    is_last: bool,
    *,
    runner: Qwen3ONNXRunner,
    tokenizer: Qwen3Tokenizer,
) -> StreamEvent:
    """增量喂一段音频,返回本步 committed 增量。

    - 未凑够 ``CHUNK_SIZE_SAMPLES`` 且 not is_last → 原地累积,返回空 StreamEvent
    - 凑够或 is_last → encode 新 chunk → 追加 audio_features → 重 prefill
      ``[audio_pad * n_af + chat_suffix + committed]`` → 自回归生成 ≤
      ``MAX_NEW_TOKENS_PER_CHUNK`` 个 token → 切 commit / pending

    is_last=True 时把所有新生成 token 全部 commit(pending 清空),并尝试把
    `pending_audio` 里的零头也跑完。
    """
    # 把本 chunk 新来的音频拼到 pending_audio
    if audio_chunk.ndim != 1:
        raise ValueError(
            f"stream_step expects 1D audio chunk, got shape "
            f"{audio_chunk.shape}"
        )
    if audio_chunk.dtype != np.float32:
        audio_chunk = audio_chunk.astype(np.float32)

    if audio_chunk.size > 0:
        state.pending_audio = np.concatenate(
            [state.pending_audio, audio_chunk]
        )
        state.total_audio_samples += audio_chunk.size

    # 未凑够 chunk size,且不是最后一步 —— 什么都不做
    if (
        not is_last
        and state.pending_audio.size < CHUNK_SIZE_SAMPLES
    ):
        return StreamEvent(
            committed_delta="",
            pending_text="",
            is_final=False,
        )

    # 最后一步且没有任何积累音频(用户瞬按瞬松) —— flush pending + 结束
    if is_last and state.pending_audio.size < _MIN_CHUNK_SAMPLES_FOR_MEL:
        return _finalize_empty(state, tokenizer)

    # --- encode 本 chunk 的音频 ---
    encode_slice = state.pending_audio
    state.pending_audio = np.zeros(0, dtype=np.float32)

    logger.debug(
        "stream_step_begin",
        chunk_idx=state.chunk_count,
        is_last=is_last,
        encode_slice_samples=int(encode_slice.size),
        committed_token_count=len(state.committed_tokens),
        pending_token_count=len(state.pending_tokens),
    )

    mel = log_mel_spectrogram(encode_slice)
    new_af = runner.encode_audio(mel)
    state.audio_features_pieces.append(new_af)

    # 拼接所有 audio_features
    audio_features = np.concatenate(
        state.audio_features_pieces, axis=1
    )
    n_af = audio_features.shape[1]

    # --- 构造 mid_ids = [audio_pad * n_af + chat_suffix + committed] ---
    # pending 全部丢弃:每 chunk 都从 committed 尾部重新生成
    mid_ids = (
        [state.audio_pad_id] * n_af
        + state.chat_suffix_ids
        + state.committed_tokens
    )

    # --- 检查 KV cache 预算 ---
    # 重 prefill 从 cur_len = len(chat_prefix) 开始,加 mid_ids 占位,再为
    # 生成留 MAX_NEW_TOKENS_PER_CHUNK
    prefill_start = len(state.chat_prefix_ids)
    prefill_end = prefill_start + len(mid_ids)
    need = prefill_end + MAX_NEW_TOKENS_PER_CHUNK
    if need > runner.max_total_len:
        raise StreamingKVOverflowError(
            f"decoder KV cache 不够:需要 {need} > "
            f"max_total_len={runner.max_total_len}"
            f"(audio_features={n_af}, committed={len(state.committed_tokens)})"
        )

    # --- 重 prefill mid 段 ---
    # 注意:这会覆写 cache 里 [prefill_start, prefill_end) 的所有位置,
    # 包括上一轮 chunk 写在那里的内容。chat_prefix KV 保持不变。
    prefill_logits = runner.decoder_step(
        np.array([mid_ids], dtype=np.int64),
        audio_features,
        state.caches,
        prefill_start,
    )

    # --- 自回归生成本 chunk 的新 token ---
    new_generated, _ = _greedy_decode(
        runner=runner,
        first_logits=prefill_logits,
        audio_features=audio_features,
        caches=state.caches,
        eos_id=state.eos_id,
        cur_len=prefill_end,
        max_new=MAX_NEW_TOKENS_PER_CHUNK,
    )

    # 日志:模型本 chunk 生成的原始 token 序列 + 未过 parse_asr_output 的原文。
    # 这是**调试流式识别怪异行为的关键抓手**。parse_asr_output 会按 <asr_text>
    # marker 剪裁,如果 marker 未出现就返回整段原文 —— 比如 "language Chinese"
    # 这种 scaffolding 前缀就可能这样漏出来。
    raw_decoded = tokenizer.decode(
        new_generated, skip_special_tokens=True
    )
    logger.debug(
        "stream_step_generated",
        chunk_idx=state.chunk_count,
        is_last=is_last,
        n_new_tokens=len(new_generated),
        max_new=MAX_NEW_TOKENS_PER_CHUNK,
        new_token_ids=new_generated,
        raw_decoded=raw_decoded,
    )

    # --- commit / pending 切分 ---
    # 注意 1:EOS 并不意味着"用户说完了" —— greedy decode 每个 chunk 都会命中 EOS
    # (模型在当前可见音频的末尾生成 EOS 作为"这段音频讲到这"的边界)。如果以
    # "命中 EOS 就全 commit"为快捷,就会把每 chunk 尾部的 "。" + 半个词 paste
    # 出去。rollback 窗口就是为这种场景存在的。
    #
    # 注意 2(28 轮真机踩到的 bug):Qwen3-ASR 每次生成是
    # `[language, Chinese, <asr_text>, 真 transcript..., <|im_end|>]` 的结构
    # —— 前 2-3 个 scaffolding token 永远在最前面。直接按"commit 前 N,pending
    # 后 ROLLBACK"切会把 `[language, Chinese]` 切进 committed,parse_asr_output
    # 找不到 `<asr_text>` marker 就把整段原样返回,用户看到 "language Chinese"。
    #
    # 修法 —— **marker-anchored split**:
    #   1. 找 `<asr_text>` marker 在 `committed + new_generated` 里的位置
    #   2. marker 及之前的全部 token 无条件 commit(scaffolding 在 committed 里
    #      无害,parse_asr_output 会从 marker 起截掉前缀)
    #   3. marker 之后的 transcript 尾巴留最后 ROLLBACK_TOKENS 个进 pending,
    #      其余 commit
    #   4. 如果整个序列都没 marker(模型吐了 scaffolding 就 EOS,极短 / 噪声音
    #      频常见),`deferred` 归 pending 等下一 chunk
    #
    # 关键:first commit 只依赖"模型吐出 marker",不依赖"生成的 token 数超过
    # 某阈值"—— 大幅降低"说完好久才出字"的延迟。
    asr_text_id = tokenizer.asr_text_id
    committed_has_marker = (
        asr_text_id is not None
        and asr_text_id in state.committed_tokens
    )

    if is_last:
        state.committed_tokens.extend(new_generated)
        state.pending_tokens = []
        split_mode = "is_last"
    elif committed_has_marker:
        # 已经过了 marker 阶段,正常 rollback:commit 前 N-ROLLBACK,pending 后
        # ROLLBACK;committed 一定含 marker(不变),满足 invariant
        commit_count = max(0, len(new_generated) - ROLLBACK_TOKENS)
        if commit_count > 0:
            state.committed_tokens.extend(new_generated[:commit_count])
            state.pending_tokens = new_generated[commit_count:]
            split_mode = "normal"
        else:
            state.pending_tokens = new_generated
            split_mode = "normal_short"
    else:
        # committed 里还没 marker,看本次 new_generated 是否带上了
        marker_idx_in_new = -1
        if asr_text_id is not None:
            try:
                marker_idx_in_new = new_generated.index(asr_text_id)
            except ValueError:
                marker_idx_in_new = -1

        if marker_idx_in_new < 0:
            # 模型还没吐 marker → 全部归 pending,等下一 chunk 再看
            state.pending_tokens = new_generated
            split_mode = "deferred_no_marker"
        else:
            # marker 首次出现:marker 及之前全部 commit;marker 之后的尾巴
            # 留最后 ROLLBACK 进 pending
            post_marker = new_generated[marker_idx_in_new + 1:]
            tail_pending_n = min(len(post_marker), ROLLBACK_TOKENS)
            commit_up_to = len(new_generated) - tail_pending_n
            state.committed_tokens.extend(new_generated[:commit_up_to])
            state.pending_tokens = new_generated[commit_up_to:]
            split_mode = "first_marker"

    state.chunk_count += 1

    # --- 计算 committed_delta(避免跨字节边界 bug) ---
    committed_raw = tokenizer.decode(
        state.committed_tokens, skip_special_tokens=True
    )
    # 二道保护:即使上面的切分让 scaffolding 滑进来了(或者 is_last 遇到了
    # "全是 scaffolding 没 marker" 的极端情况),committed_raw 不含
    # <asr_text> 就拒绝贴。安全兜底。
    if (
        asr_text_id is not None
        and asr_text_id not in state.committed_tokens
    ):
        new_committed_text = ""
    else:
        new_committed_text = parse_asr_output(committed_raw)
    committed_delta = new_committed_text[len(state.committed_text):]
    state.committed_text = new_committed_text

    pending_raw = tokenizer.decode(
        state.pending_tokens, skip_special_tokens=True
    )
    pending_text = parse_asr_output(pending_raw)

    logger.debug(
        "stream_step_split",
        chunk_idx=state.chunk_count - 1,  # 这 chunk 已 +1,用它原本的序号
        is_last=is_last,
        split_mode=split_mode,
        committed_total=len(state.committed_tokens),
        pending_total=len(state.pending_tokens),
        committed_raw=committed_raw,
        committed_text=new_committed_text,
        committed_delta=committed_delta,
        pending_raw=pending_raw,
        pending_text=pending_text,
    )

    return StreamEvent(
        committed_delta=committed_delta,
        pending_text=pending_text,
        is_final=is_last,
    )


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------


def _greedy_decode(
    *,
    runner: Qwen3ONNXRunner,
    first_logits: np.ndarray,
    audio_features: np.ndarray,
    caches: list[np.ndarray],
    eos_id: int,
    cur_len: int,
    max_new: int,
) -> tuple[list[int], int]:
    """贪心自回归:从 first_logits 起循环喂 argmax,直到 EOS 或上限。

    Returns (generated_token_ids, final_cur_len).
    """
    generated: list[int] = []
    logits = first_logits
    for _ in range(max_new):
        nid = int(np.argmax(logits[0, -1]))
        if nid == eos_id:
            break
        generated.append(nid)
        if cur_len + 1 > runner.max_total_len:
            raise StreamingKVOverflowError(
                f"KV cache overflow during generation at cur_len={cur_len}"
            )
        logits = runner.decoder_step(
            np.array([[nid]], dtype=np.int64),
            audio_features,
            caches,
            cur_len,
        )
        cur_len += 1
    return generated, cur_len


def _finalize_empty(
    state: Qwen3StreamState, tokenizer: Qwen3Tokenizer
) -> StreamEvent:
    """is_last=True 但没有可处理音频:把 pending 并入 committed 即可。"""
    if state.pending_tokens:
        state.committed_tokens.extend(state.pending_tokens)
        state.pending_tokens = []
        new_committed_text = parse_asr_output(
            tokenizer.decode(
                state.committed_tokens, skip_special_tokens=True
            )
        )
        committed_delta = new_committed_text[
            len(state.committed_text):
        ]
        state.committed_text = new_committed_text
    else:
        committed_delta = ""
    return StreamEvent(
        committed_delta=committed_delta,
        pending_text="",
        is_final=True,
    )
