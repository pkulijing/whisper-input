"""Round 37 spike — 验证 baicai1145/Qwen3-ASR-1.7B-ONNX (fp16) 在原 int8
翻车点上是否给出非空合理文本。

跑法:uv run python docs/37-换fp16-ONNX修1.7B-offline/spike.py

Schema 摸清:
- encoder.onnx: padded_feature (chunks,1,128,W) fp16 + chunk_lengths
  (chunks,) int64 → audio_features (audio_seq, 2048) fp16
- decoder.onnx: input_ids + audio_features + cache_position +
  past_{key,value}_{00..27} (1,8,1664,128) fp16 → logits (B,vocab) fp16 +
  present_{key,value}_{00..27} (1,8,1664,128) fp16
- baicai1145 不 pad 到 30s,只 pad 到 chunk-aligned (window=100 帧)
- decoder 单步只输出最后位置 logits;return present 是完整 cache 直接覆盖即可
- 双 EOS = [151645, 151643]
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from modelscope import snapshot_download

from daobidao.stt.qwen3._feature import log_mel_spectrogram
from daobidao.stt.qwen3._postprocess import parse_asr_output
from daobidao.stt.qwen3._prompt import build_prompt
from daobidao.stt.qwen3._tokenizer import Qwen3Tokenizer
from daobidao.stt.qwen3.qwen3_asr import _wav_bytes_to_float32

REPO_BY_VARIANT = {
    "0.6B": "baicai1145/Qwen3-ASR-0.6B-ONNX",
    "1.7B": "baicai1145/Qwen3-ASR-1.7B-ONNX",
}
ALLOW_PATTERNS = [
    "encoder.onnx",
    "encoder.onnx.data",
    "decoder.onnx",
    "decoder.onnx.data",
    "*.json",
    "*.txt",
    "*.jinja",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ZH_WAV = REPO_ROOT / "tests" / "fixtures" / "zh.wav"
ZH_LONG_WAV = REPO_ROOT / "tests" / "fixtures" / "zh_long.wav"


# ---------------------------------------------------------------------------
# Audio preprocessing - baicai1145 风格 (不 pad 到 30s,chunk-aligned)
# ---------------------------------------------------------------------------


def make_padded_feature(
    audio: np.ndarray, window: int, dtype: np.dtype
) -> tuple[np.ndarray, np.ndarray]:
    """Mel + chunk-align + reshape 成 baicai1145 encoder.onnx 期望格式。

    Returns
    -------
    padded_feature: (chunks, 1, 128, window) dtype
    chunk_lengths: (chunks,) int64,最后一段可能短
    """
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)

    mel = log_mel_spectrogram(audio)  # (128, n_frames)
    n_mels, feature_len = mel.shape
    chunk_num = (feature_len + window - 1) // window
    padded_total = chunk_num * window
    if padded_total > feature_len:
        mel = np.pad(
            mel,
            ((0, 0), (0, padded_total - feature_len)),
            mode="constant",
        )
    # (n_mels, padded_total) → (chunks, n_mels, window) → (chunks, 1, n_mels, window)
    padded = (
        mel.T.reshape(chunk_num, window, n_mels).transpose(0, 2, 1)[
            :, None, :, :
        ]
    ).astype(dtype, copy=False)

    chunk_lengths = np.full((chunk_num,), window, dtype=np.int64)
    chunk_lengths[-1] = feature_len - window * (chunk_num - 1)
    return padded, chunk_lengths


# ---------------------------------------------------------------------------
# Spike runner
# ---------------------------------------------------------------------------


class SpikeRunner:
    def __init__(self, cache_root: Path) -> None:
        self.cache_root = cache_root
        meta = json.loads((cache_root / "metadata.json").read_text())
        self.meta = meta
        print(
            f"\nmetadata.json: dtype={meta['dtype']} num_layers={meta['num_layers']} "
            f"static_cache_len={meta['static_cache_len']} eos={meta['eos_token_ids']} "
            f"audio_d={meta['audio_d_model']} audio_out={meta['audio_output_dim']} "
            f"n_window={meta['n_window']}"
        )

        self.dtype = np.float16 if meta["dtype"] == "float16" else np.float32
        self.num_layers = int(meta["num_layers"])
        self.cache_len = int(meta["static_cache_len"])
        self.audio_output_dim = int(meta["audio_output_dim"])
        self.eos_ids: set[int] = set(meta["eos_token_ids"])
        # n_window * 2 = window 大小(参考 baicai1145 onnx_asr_service.py:593)
        self.window = int(meta["n_window"]) * 2

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ["CPUExecutionProvider"]

        t0 = time.perf_counter()
        self.encoder = ort.InferenceSession(
            str(cache_root / "encoder.onnx"),
            sess_options=so,
            providers=providers,
        )
        print(f"  encoder ready in {time.perf_counter() - t0:.2f}s")

        t0 = time.perf_counter()
        self.decoder = ort.InferenceSession(
            str(cache_root / "decoder.onnx"),
            sess_options=so,
            providers=providers,
        )
        print(f"  decoder ready in {time.perf_counter() - t0:.2f}s")

        # KV cache shape (1, 8, cache_len, 128)
        sample_in = next(
            i
            for i in self.decoder.get_inputs()
            if i.name.startswith("past_key_")
        )
        self.kv_shape = list(sample_in.shape)
        print(f"  KV cache shape: {self.kv_shape}")

    def alloc_caches(self) -> dict[str, np.ndarray]:
        """KV cache 形状 (1, kv_heads=8, cache_len, head_dim=128) 全 0。"""
        shape = tuple(self.kv_shape)
        caches: dict[str, np.ndarray] = {}
        for i in range(self.num_layers):
            caches[f"past_key_{i:02d}"] = np.zeros(shape, dtype=self.dtype)
            caches[f"past_value_{i:02d}"] = np.zeros(shape, dtype=self.dtype)
        return caches

    def encode_audio(self, audio: np.ndarray) -> np.ndarray:
        padded, lengths = make_padded_feature(audio, self.window, self.dtype)
        out = self.encoder.run(
            ["audio_features"],
            {"padded_feature": padded, "chunk_lengths": lengths},
        )[0]
        return out  # (audio_seq, 2048)

    def decoder_step(
        self,
        input_ids: np.ndarray,
        audio_features: np.ndarray,
        caches: dict[str, np.ndarray],
        cache_position: np.ndarray,
    ) -> np.ndarray:
        feed: dict[str, np.ndarray] = {
            "input_ids": input_ids,
            "audio_features": audio_features,
            "cache_position": cache_position,
            **caches,
        }
        # 拿所有 output(logits + presents)
        names = [o.name for o in self.decoder.get_outputs()]
        outs = self.decoder.run(names, feed)
        out_map = dict(zip(names, outs, strict=True))
        # present_xx 直接覆盖 past_xx
        for i in range(self.num_layers):
            caches[f"past_key_{i:02d}"] = out_map[f"present_key_{i:02d}"]
            caches[f"past_value_{i:02d}"] = out_map[f"present_value_{i:02d}"]
        return out_map["logits"]


# ---------------------------------------------------------------------------
# Transcribe
# ---------------------------------------------------------------------------


def transcribe(
    audio: np.ndarray,
    runner: SpikeRunner,
    tokenizer: Qwen3Tokenizer,
    max_new_tokens: int = 400,
) -> tuple[str, list[int], dict]:
    t_start = time.perf_counter()
    af = runner.encode_audio(audio)
    t_enc = time.perf_counter() - t_start

    prompt = build_prompt(int(af.shape[0]))
    prompt_ids = tokenizer.encode(prompt)
    input_ids = np.array([prompt_ids], dtype=np.int64)

    caches = runner.alloc_caches()
    cur_len = 0
    seq = input_ids.shape[1]
    cache_position = np.arange(cur_len, cur_len + seq, dtype=np.int64)

    t_pre0 = time.perf_counter()
    logits = runner.decoder_step(input_ids, af, caches, cache_position)
    cur_len += seq
    t_pre = time.perf_counter() - t_pre0

    generated: list[int] = []
    t_dec0 = time.perf_counter()
    for _ in range(max_new_tokens):
        nid = int(np.argmax(logits[0]))
        if nid in runner.eos_ids:
            break
        generated.append(nid)
        next_in = np.array([[nid]], dtype=np.int64)
        cache_position = np.array([cur_len], dtype=np.int64)
        logits = runner.decoder_step(next_in, af, caches, cache_position)
        cur_len += 1
    t_dec = time.perf_counter() - t_dec0

    text = parse_asr_output(
        tokenizer.decode(generated, skip_special_tokens=True)
    )
    timings = {
        "encode_s": t_enc,
        "prefill_s": t_pre,
        "decode_s": t_dec,
        "total_s": time.perf_counter() - t_start,
        "audio_seconds": len(audio) / 16000,
        "audio_features_shape": af.shape,
        "prompt_len": seq,
        "generated_count": len(generated),
    }
    return text, generated, timings


def main() -> None:
    print("=" * 70)
    print("Round 37 Spike — baicai1145 fp16 1.7B 离线翻车修复验证")
    print("=" * 70)

    print("\n[1] 确保 baicai1145/Qwen3-ASR-1.7B-ONNX 已下载…")
    cache = Path(
        snapshot_download(
            REPO_BY_VARIANT["1.7B"], allow_patterns=ALLOW_PATTERNS
        )
    )
    print(f"  cache_root = {cache}")

    print("\n[2] 构造 Spike Runner")
    runner = SpikeRunner(cache)
    tokenizer = Qwen3Tokenizer(cache)

    print("\n[3] 跑三段 audio (1.7B fp16)")
    cases = [
        (
            "zh.wav full (10.56s) — int8 翻车点 1",
            _wav_bytes_to_float32(ZH_WAV.read_bytes()),
        ),
        (
            "zh_long[:5s] — int8 翻车点 2",
            _wav_bytes_to_float32(ZH_LONG_WAV.read_bytes())[:80000],
        ),
        (
            "zh.wav[:10.5s] — int8 PASS 对照",
            _wav_bytes_to_float32(ZH_WAV.read_bytes())[:168000],
        ),
    ]
    for label, audio in cases:
        print(f"\n  --- {label} ---")
        text, gen, t = transcribe(audio, runner, tokenizer)
        status = "PASS" if (len(gen) > 5 and text) else "FAIL"
        print(
            f"    {status} af={t['audio_features_shape']} prompt_len={t['prompt_len']} "
            f"gen={t['generated_count']}"
        )
        print(
            f"    encode={t['encode_s']:.2f}s prefill={t['prefill_s']:.2f}s "
            f"decode={t['decode_s']:.2f}s total={t['total_s']:.2f}s "
            f"(rtf={t['total_s'] / t['audio_seconds']:.2f})"
        )
        print(f"    text: {text!r}")


if __name__ == "__main__":
    main()
