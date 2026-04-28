"""Tests for ``daobidao.stt.qwen3._onnx_runner.Qwen3ONNXRunner``.

Round 37 起 runner 走 baicai1145 fp16 export(2-session,无独立 conv_frontend),
本文件只测对外行为(introspection / encode_audio shape & dtype / decoder_step
shape & overflow / 真音频集成),不测 ONNX 输出名 / KV cache 内部布局这些
强耦合具体 export 的细节 —— 那些细节随 export 来源不同会换,measure 它们
的话每次切版本都得重写测试,价值低。

跨 variant 不变量:
- num_layers = 28
- kv_heads = 8
- head_dim = 128
- dtype = float16

跨 variant 变量:
- audio_feature_dim: 0.6B → 1024, 1.7B → 2048
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from daobidao.stt.qwen3._feature import N_MELS, log_mel_spectrogram
from daobidao.stt.qwen3._onnx_runner import Qwen3ONNXRunner


@pytest.fixture(
    scope="module",
    params=["0.6B", "1.7B"],
    ids=["0.6B", "1.7B"],
)
def runner(request, stt_0_6b, stt_1_7b) -> Qwen3ONNXRunner:
    return {"0.6B": stt_0_6b, "1.7B": stt_1_7b}[request.param]._runner


# --------------------------------------------------------------------------
# Introspection
# --------------------------------------------------------------------------


def test_decoder_layer_count_is_28(runner: Qwen3ONNXRunner):
    assert runner.num_layers == 28


def test_decoder_kv_dims(runner: Qwen3ONNXRunner):
    assert runner.kv_heads == 8
    assert runner.head_dim == 128


def test_max_total_len_matches_metadata(runner: Qwen3ONNXRunner):
    # baicai1145 metadata.json 写 static_cache_len=1664
    assert runner.max_total_len == 1664


def test_audio_feature_dim_matches_variant(runner: Qwen3ONNXRunner, request):
    expected = {"0.6B": 1024, "1.7B": 2048}[request.node.callspec.id]
    assert runner.audio_feature_dim == expected


def test_eos_ids_includes_im_end_and_endoftext(runner: Qwen3ONNXRunner):
    # baicai1145 双 EOS:151645 (<|im_end|>) + 151643 (<|endoftext|>)
    assert 151645 in runner.eos_ids
    assert 151643 in runner.eos_ids


# --------------------------------------------------------------------------
# Audio encoding
# --------------------------------------------------------------------------


def test_encode_audio_shape_and_dtype(runner: Qwen3ONNXRunner):
    audio = np.zeros(16000 * 10, dtype=np.float32)  # 10s
    mel = log_mel_spectrogram(audio)
    assert mel.shape == (N_MELS, 1000)

    af = runner.encode_audio(mel)
    assert af.ndim == 3
    assert af.shape[0] == 1  # batch dim added by runner
    assert af.shape[2] == runner.audio_feature_dim
    # 10s 真音频 → audio_seq 在百量级
    assert 50 < af.shape[1] < 1000
    assert af.dtype == np.float16


def test_encode_audio_rejects_wrong_rank(runner: Qwen3ONNXRunner):
    with pytest.raises(ValueError, match="N_MELS"):
        runner.encode_audio(np.zeros((3000,), dtype=np.float32))


def test_encode_audio_coerces_non_float32(runner: Qwen3ONNXRunner):
    audio = np.zeros(16000 * 5, dtype=np.float32)
    mel64 = log_mel_spectrogram(audio).astype(np.float64)
    out = runner.encode_audio(mel64)
    # encoder 输出固定 fp16,无论 mel dtype 是什么
    assert out.dtype == np.float16


# --------------------------------------------------------------------------
# KV cache allocation
# --------------------------------------------------------------------------


def test_alloc_decoder_caches_count_and_shape(runner: Qwen3ONNXRunner):
    caches = runner.alloc_decoder_caches()
    assert len(caches) == 2 * runner.num_layers
    # baicai1145 KV cache shape: (B=1, H=8, T=1664, D=128) fp16
    expected_shape = (
        1,
        runner.kv_heads,
        runner.max_total_len,
        runner.head_dim,
    )
    for c in caches:
        assert c.shape == expected_shape
        assert c.dtype == np.float16
        assert (c == 0).all()


# --------------------------------------------------------------------------
# Decoder step — prefill + single-step generation
# --------------------------------------------------------------------------


def test_decoder_step_prefill_returns_last_token_logits(
    runner: Qwen3ONNXRunner,
):
    audio_features = np.zeros(
        (1, 100, runner.audio_feature_dim), dtype=np.float32
    )
    input_ids = np.zeros((1, 10), dtype=np.int64)
    caches = runner.alloc_decoder_caches()

    logits = runner.decoder_step(input_ids, audio_features, caches, cur_len=0)
    # baicai1145 decoder 只输出最后一位置 (B, vocab),runner 内部 unsqueeze
    # 成 (B, 1, vocab) 兼容老 caller 接口。
    assert logits.shape == (1, 1, 151936)


def test_decoder_step_cache_overflow_raises(runner: Qwen3ONNXRunner):
    audio_features = np.zeros(
        (1, 50, runner.audio_feature_dim), dtype=np.float32
    )
    caches = runner.alloc_decoder_caches()
    input_ids = np.zeros((1, 5), dtype=np.int64)

    with pytest.raises(RuntimeError, match="overflow"):
        runner.decoder_step(
            input_ids,
            audio_features,
            caches,
            cur_len=runner.max_total_len - 2,
        )


def test_decoder_step_caches_change_after_step(runner: Qwen3ONNXRunner):
    """Sanity: 调一次 decoder_step,caches list 里的 array 引用应该被更新
    (baicai1145 用整段 present 覆盖,不是 in-place scatter delta)。"""
    audio_features = np.zeros(
        (1, 50, runner.audio_feature_dim), dtype=np.float32
    )
    input_ids = np.zeros((1, 5), dtype=np.int64)
    caches = runner.alloc_decoder_caches()
    pre_id = id(caches[0])
    runner.decoder_step(input_ids, audio_features, caches, cur_len=0)
    post_id = id(caches[0])
    # baicai1145 returns a fresh present_key_00 each time → reference changes
    assert pre_id != post_id


# --------------------------------------------------------------------------
# Integration: encode → prefill produces non-trivial logits
# --------------------------------------------------------------------------


def test_real_audio_prefill_produces_plausible_logits(
    runner: Qwen3ONNXRunner,
):
    import soundfile as sf

    wav = Path(__file__).parent / "fixtures" / "zh.wav"
    audio, sr = sf.read(str(wav), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    assert sr == 16000

    # Round 37 baicai1145 不需要 30s pad,任意长度 mel 都行
    mel = log_mel_spectrogram(audio)
    audio_features = runner.encode_audio(mel)

    input_ids = np.array([[151644]], dtype=np.int64)  # <|im_start|>
    caches = runner.alloc_decoder_caches()
    logits = runner.decoder_step(input_ids, audio_features, caches, cur_len=0)
    assert logits.shape == (1, 1, 151936)
    assert np.isfinite(logits).all()
    assert (logits != 0).any()
