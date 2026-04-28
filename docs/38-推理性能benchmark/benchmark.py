"""Round 38 第一阶段 spike — int8 vs fp16 baseline benchmark(历史快照)。

对比 zengshuishui int8 vs baicai1145 fp16 在 0.6B / 1.7B × 三段音频长度
上的推理性能。详见同目录 PLAN.md / SUMMARY.md。

**这是 spike 阶段一次性脚本,本轮第二阶段已升级成顶层 ``benchmarks/``
框架(可重复 / 可加 backend / 入 baseline 库)。新跑请用:**

    uv run python -m benchmarks --output benchmarks/results/baselines/<...>

本脚本保留作 round 38 spike 历史快照,不再维护。
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort

from daobidao.stt.qwen3._feature import log_mel_spectrogram
from daobidao.stt.qwen3._postprocess import parse_asr_output
from daobidao.stt.qwen3._prompt import build_prompt
from daobidao.stt.qwen3._tokenizer import Qwen3Tokenizer
from daobidao.stt.qwen3.qwen3_asr import _wav_bytes_to_float32

# ---------------------------------------------------------------------------
# Cache 路径(本机已有,不重下)
# ---------------------------------------------------------------------------

HOME = Path.home()
INT8_ROOT = HOME / ".cache/modelscope/hub/models/zengshuishui/Qwen3-ASR-onnx"
INT8_DIR_BY_VARIANT = {
    "0.6B": INT8_ROOT / "model_0.6B",
    "1.7B": INT8_ROOT / "model_1.7B",
}
INT8_TOKENIZER_DIR = INT8_ROOT / "tokenizer"

FP16_ROOT_BY_VARIANT = {
    "0.6B": HOME
    / ".cache/modelscope/hub/models/baicai1145/Qwen3-ASR-0___6B-ONNX",
    "1.7B": HOME
    / ".cache/modelscope/hub/models/baicai1145/Qwen3-ASR-1___7B-ONNX",
}

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ZH_WAV = REPO_ROOT / "tests/fixtures/zh.wav"
ZH_LONG_WAV = REPO_ROOT / "tests/fixtures/zh_long.wav"

# ---------------------------------------------------------------------------
# 切片清单(samples @ 16 kHz)
# ---------------------------------------------------------------------------

SR = 16000
CASES_AUDIO: list[tuple[str, Path, int]] = [
    ("short", ZH_WAV, 5 * SR),  # 80000 samples / 5.00s
    (
        "medium",
        ZH_WAV,
        int(10.5 * SR),
    ),  # 168000 samples / 10.50s — int8 1.7B 安全
    ("long", ZH_LONG_WAV, 25 * SR),  # 400000 samples / 25.00s — 8s~28s 安全区
]

VARIANTS = ["0.6B", "1.7B"]
QUANTS = ["int8", "fp16"]
N_REPEATS = 3
MAX_NEW_TOKENS = 400


# ---------------------------------------------------------------------------
# Int8 runner — 从 git 6dec467^:_onnx_runner.py 抠出来,改名 Int8Runner,
# 加 eos_ids 属性方便 benchmark 统一接口。3-session,KV cache (B,T,H,D),
# scatter delta 写回。
# ---------------------------------------------------------------------------


class Int8Runner:
    DEFAULT_MAX_TOTAL_LEN = 1200

    def __init__(self, model_dir: Path) -> None:
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ["CPUExecutionProvider"]

        self.conv = ort.InferenceSession(
            str(model_dir / "conv_frontend.onnx"),
            sess_options=so,
            providers=providers,
        )
        self.encoder = ort.InferenceSession(
            str(model_dir / "encoder.int8.onnx"),
            sess_options=so,
            providers=providers,
        )
        self.decoder = ort.InferenceSession(
            str(model_dir / "decoder.int8.onnx"),
            sess_options=so,
            providers=providers,
        )

        self.max_total_len = self.DEFAULT_MAX_TOTAL_LEN
        # 推断 num_layers / kv_heads / head_dim
        key_inputs = [
            i
            for i in self.decoder.get_inputs()
            if i.name.startswith("cache_key_")
        ]
        self.num_layers = len(key_inputs)
        shape = key_inputs[0].shape
        self.kv_heads = shape[2] if isinstance(shape[2], int) else 8
        self.head_dim = shape[3] if isinstance(shape[3], int) else 128

        for inp in self.decoder.get_inputs():
            if inp.name == "audio_features":
                self.audio_feature_dim = (
                    inp.shape[-1] if isinstance(inp.shape[-1], int) else None
                )
                break

        self._decoder_output_names = ["logits"]
        for i in range(self.num_layers):
            self._decoder_output_names.append(f"key_delta_{i}")
            self._decoder_output_names.append(f"value_delta_{i}")

        # 单 EOS = <|im_end|>
        self.eos_ids: set[int] = {151645}

    def encode_audio(self, audio: np.ndarray) -> np.ndarray:
        """audio (float32 mono 16 kHz) → audio_features (1, n_audio, dim) float32."""
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        # 老 int8 走 30s pad
        target = 30 * SR
        if len(audio) < target:
            audio = np.pad(audio, (0, target - len(audio)))
        elif len(audio) > target:
            # int8 单段最多 30s;超出 truncate(本 benchmark 不超 25s,不会触发)
            audio = audio[:target]
        mel = log_mel_spectrogram(audio).astype(np.float32)  # (128, n_frames)
        conv_input = mel.T[np.newaxis, ...]
        conv_output = self.conv.run(
            ["conv_output"], {"input_features": conv_input}
        )[0]
        n_audio = conv_output.shape[1]
        feature_attention_mask = np.ones((1, n_audio), dtype=bool)
        audio_features = self.encoder.run(
            ["audio_features"],
            {
                "input_features": conv_output,
                "feature_attention_mask": feature_attention_mask,
            },
        )[0]
        return audio_features  # (1, n_audio, dim) float32

    def alloc_caches(self) -> list[np.ndarray]:
        return [
            np.zeros(
                (1, self.max_total_len, self.kv_heads, self.head_dim),
                dtype=np.float32,
            )
            for _ in range(2 * self.num_layers)
        ]

    def decoder_step(
        self,
        input_ids: np.ndarray,
        audio_features: np.ndarray,
        caches: list[np.ndarray],
        cache_position: np.ndarray,
    ) -> np.ndarray:
        """logits shape (1, seq, vocab) — caller 用 [0, -1]。"""
        cur_len = int(cache_position[0])
        seq = input_ids.shape[1]
        if cur_len + seq > self.max_total_len:
            raise RuntimeError(
                f"Int8 KV cache overflow: cur_len={cur_len}+seq={seq}>"
                f"{self.max_total_len}"
            )
        attention_mask = np.ones(input_ids.shape, dtype=np.int64)
        feed: dict[str, np.ndarray] = {
            "input_ids": input_ids,
            "audio_features": audio_features,
            "attention_mask": attention_mask,
            "cache_position": cache_position,
        }
        for i in range(self.num_layers):
            feed[f"cache_key_{i}"] = caches[2 * i]
            feed[f"cache_value_{i}"] = caches[2 * i + 1]

        outputs = self.decoder.run(self._decoder_output_names, feed)
        logits = outputs[0]
        end = cur_len + seq
        for i in range(self.num_layers):
            key_delta = outputs[1 + 2 * i]
            value_delta = outputs[1 + 2 * i + 1]
            caches[2 * i][:, cur_len:end, :, :] = key_delta
            caches[2 * i + 1][:, cur_len:end, :, :] = value_delta
        return logits


# ---------------------------------------------------------------------------
# Fp16 runner — 抄自 docs/37-换fp16-ONNX修1.7B-offline/spike.py SpikeRunner
# 2-session,KV cache (B,H,T,D) fp16,present 整段覆盖,双 EOS。
# 跟 Int8Runner 接口对齐:encode_audio / alloc_caches / decoder_step,
# decoder_step 返回 (B, 1, vocab) 让 callers 用 [0, -1]。
# ---------------------------------------------------------------------------


def _make_padded_feature(
    audio: np.ndarray, window: int, dtype: np.dtype
) -> tuple[np.ndarray, np.ndarray]:
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)
    mel = log_mel_spectrogram(audio)
    n_mels, feature_len = mel.shape
    chunk_num = (feature_len + window - 1) // window
    padded_total = chunk_num * window
    if padded_total > feature_len:
        mel = np.pad(
            mel, ((0, 0), (0, padded_total - feature_len)), mode="constant"
        )
    padded = (
        mel.T.reshape(chunk_num, window, n_mels).transpose(0, 2, 1)[
            :, None, :, :
        ]
    ).astype(dtype, copy=False)
    chunk_lengths = np.full((chunk_num,), window, dtype=np.int64)
    chunk_lengths[-1] = feature_len - window * (chunk_num - 1)
    return padded, chunk_lengths


class Fp16Runner:
    def __init__(self, cache_root: Path) -> None:
        meta = json.loads((cache_root / "metadata.json").read_text())
        self.dtype = np.float16 if meta["dtype"] == "float16" else np.float32
        self.num_layers = int(meta["num_layers"])
        self.cache_len = int(meta["static_cache_len"])
        self.audio_output_dim = int(meta["audio_output_dim"])
        self.eos_ids: set[int] = set(meta["eos_token_ids"])
        self.window = int(meta["n_window"]) * 2

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        providers = ["CPUExecutionProvider"]
        self.encoder = ort.InferenceSession(
            str(cache_root / "encoder.onnx"),
            sess_options=so,
            providers=providers,
        )
        self.decoder = ort.InferenceSession(
            str(cache_root / "decoder.onnx"),
            sess_options=so,
            providers=providers,
        )
        sample_in = next(
            i
            for i in self.decoder.get_inputs()
            if i.name.startswith("past_key_")
        )
        self.kv_shape = list(sample_in.shape)

    def encode_audio(self, audio: np.ndarray) -> np.ndarray:
        """Returns (n_audio_tokens, audio_output_dim) — 注意没有 batch 维。
        为了对齐 Int8Runner 输出 (1, n, dim),外面 transcribe 用 -2 索引而非
        硬编码,二者都通用。"""
        padded, lengths = _make_padded_feature(audio, self.window, self.dtype)
        out = self.encoder.run(
            ["audio_features"],
            {"padded_feature": padded, "chunk_lengths": lengths},
        )[0]
        return out

    def alloc_caches(self) -> dict[str, np.ndarray]:
        shape = tuple(self.kv_shape)
        caches: dict[str, np.ndarray] = {}
        for i in range(self.num_layers):
            caches[f"past_key_{i:02d}"] = np.zeros(shape, dtype=self.dtype)
            caches[f"past_value_{i:02d}"] = np.zeros(shape, dtype=self.dtype)
        return caches

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
        names = [o.name for o in self.decoder.get_outputs()]
        outs = self.decoder.run(names, feed)
        out_map = dict(zip(names, outs, strict=True))
        for i in range(self.num_layers):
            caches[f"past_key_{i:02d}"] = out_map[f"present_key_{i:02d}"]
            caches[f"past_value_{i:02d}"] = out_map[f"present_value_{i:02d}"]
        # logits: (B, vocab) → unsqueeze 到 (B, 1, vocab) 让 callers [0, -1] 通用
        logits = out_map["logits"]
        if logits.ndim == 2:
            logits = logits[:, None, :]
        return logits


# ---------------------------------------------------------------------------
# 通用 transcribe(吃任一 runner)
# ---------------------------------------------------------------------------


def transcribe(
    audio: np.ndarray,
    runner: Int8Runner | Fp16Runner,
    tokenizer: Qwen3Tokenizer,
    max_new_tokens: int = MAX_NEW_TOKENS,
) -> tuple[str, list[int], dict[str, Any]]:
    t0 = time.perf_counter()
    af = runner.encode_audio(audio)
    t_enc = time.perf_counter() - t0

    n_audio = int(af.shape[-2])
    prompt = build_prompt(n_audio)
    prompt_ids = tokenizer.encode(prompt)
    input_ids = np.array([prompt_ids], dtype=np.int64)

    caches = runner.alloc_caches()
    cur_len = 0
    seq = input_ids.shape[1]
    cache_position = np.arange(cur_len, cur_len + seq, dtype=np.int64)

    t1 = time.perf_counter()
    logits = runner.decoder_step(input_ids, af, caches, cache_position)
    t_pre = time.perf_counter() - t1
    cur_len += seq

    generated: list[int] = []
    t2 = time.perf_counter()
    for _ in range(max_new_tokens):
        nid = int(np.argmax(logits[0, -1]))
        if nid in runner.eos_ids:
            break
        generated.append(nid)
        next_in = np.array([[nid]], dtype=np.int64)
        cache_position = np.array([cur_len], dtype=np.int64)
        logits = runner.decoder_step(next_in, af, caches, cache_position)
        cur_len += 1
    t_dec = time.perf_counter() - t2

    text = parse_asr_output(
        tokenizer.decode(generated, skip_special_tokens=True)
    )
    return (
        text,
        generated,
        {
            "encode_s": t_enc,
            "prefill_s": t_pre,
            "decode_s": t_dec,
            "total_s": t_enc + t_pre + t_dec,
            "n_audio_tokens": n_audio,
            "generated_count": len(generated),
        },
    )


# ---------------------------------------------------------------------------
# 测量循环
# ---------------------------------------------------------------------------


def load_audio_clip(path: Path, n_samples: int) -> np.ndarray:
    full = _wav_bytes_to_float32(path.read_bytes())
    if len(full) < n_samples:
        raise RuntimeError(
            f"{path} 长度 {len(full)} 不够切 {n_samples} samples"
        )
    return full[:n_samples].astype(np.float32, copy=False)


def measure_case(
    quant: str,
    variant: str,
    slug: str,
    audio: np.ndarray,
    runner: Int8Runner | Fp16Runner,
    tokenizer: Qwen3Tokenizer,
) -> dict[str, Any]:
    """warmup 1 次 + 测量 N_REPEATS 次,取 median。"""
    audio_seconds = len(audio) / SR
    case_id = f"{quant}-{variant}-{slug}"
    print(f"\n  [{case_id}] audio_seconds={audio_seconds:.2f}")

    # warmup
    text_w, _, t_w = transcribe(audio, runner, tokenizer)
    print(
        f"    warmup: total={t_w['total_s']:.2f}s gen={t_w['generated_count']} "
        f"text={text_w[:30]!r}…"
    )

    metrics_runs: list[dict[str, Any]] = []
    last_text = text_w
    last_gen_count = t_w["generated_count"]
    for r in range(N_REPEATS):
        text, _gen, t = transcribe(audio, runner, tokenizer)
        metrics_runs.append(t)
        last_text = text
        last_gen_count = t["generated_count"]
        print(
            f"    run {r + 1}: encode={t['encode_s']:.2f}s "
            f"prefill={t['prefill_s']:.2f}s decode={t['decode_s']:.2f}s "
            f"total={t['total_s']:.2f}s rtf={t['total_s'] / audio_seconds:.2f}"
        )

    # 取 median
    def med(field: str) -> float:
        return float(statistics.median(r[field] for r in metrics_runs))

    encode_s = med("encode_s")
    prefill_s = med("prefill_s")
    decode_s = med("decode_s")
    total_s = med("total_s")

    record: dict[str, Any] = {
        "case_id": case_id,
        "quant": quant,
        "variant": variant,
        "slug": slug,
        "audio_seconds": audio_seconds,
        "n_audio_tokens": metrics_runs[0]["n_audio_tokens"],
        "generated_count": last_gen_count,
        "encode_s": encode_s,
        "prefill_s": prefill_s,
        "decode_s": decode_s,
        "total_s": total_s,
        "rtf": total_s / audio_seconds,
        "text": last_text,
        "all_runs": metrics_runs,
        "status": "PASS"
        if (last_gen_count > 5 and last_text and total_s / audio_seconds < 5)
        else "FAIL",
    }
    return record


def main() -> None:
    print("=" * 70)
    print("Round 38 spike — int8 vs fp16 baseline benchmark(历史快照)")
    print("=" * 70)

    # 预先 load 三段音频(每个切片只切一次)
    audio_clips: dict[str, np.ndarray] = {}
    for slug, path, n in CASES_AUDIO:
        audio_clips[slug] = load_audio_clip(path, n)
        print(
            f"  audio[{slug}] = {path.name}[:{n}] = "
            f"{len(audio_clips[slug]) / SR:.2f}s"
        )

    # 老 int8 共用 tokenizer dir
    int8_tokenizer = Qwen3Tokenizer(INT8_TOKENIZER_DIR)

    records: list[dict[str, Any]] = []

    # int8: 0.6B + 1.7B
    for variant in VARIANTS:
        print(f"\n>> 加载 int8-{variant}")
        t0 = time.perf_counter()
        runner = Int8Runner(INT8_DIR_BY_VARIANT[variant])
        print(
            f"   int8-{variant} session ready in {time.perf_counter() - t0:.1f}s"
        )
        for slug, _, _ in CASES_AUDIO:
            rec = measure_case(
                "int8", variant, slug, audio_clips[slug], runner, int8_tokenizer
            )
            records.append(rec)
        del runner

    # fp16: 0.6B + 1.7B(per-variant tokenizer 在自己 cache_root)
    for variant in VARIANTS:
        print(f"\n>> 加载 fp16-{variant}")
        t0 = time.perf_counter()
        cache_root = FP16_ROOT_BY_VARIANT[variant]
        runner = Fp16Runner(cache_root)
        tokenizer = Qwen3Tokenizer(cache_root)
        print(
            f"   fp16-{variant} session ready in {time.perf_counter() - t0:.1f}s"
        )
        for slug, _, _ in CASES_AUDIO:
            rec = measure_case(
                "fp16", variant, slug, audio_clips[slug], runner, tokenizer
            )
            records.append(rec)
        del runner

    # 写盘
    out_dir = Path(__file__).resolve().parent
    json_path = out_dir / "baseline_results.json"
    md_path = out_dir / "baseline_results.md"

    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2))
    print(f"\n✓ 写出 {json_path}")

    md_path.write_text(_format_markdown(records))
    print(f"✓ 写出 {md_path}")


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------


def _format_markdown(records: list[dict[str, Any]]) -> str:
    """生成 baseline_results.md。

    布局:RTF 矩阵 + encode/prefill/decode 三张子矩阵 + per-case 详情。
    """
    by_key = {(r["quant"], r["variant"], r["slug"]): r for r in records}
    slugs = ["short", "medium", "long"]

    def matrix(metric: str, fmt: str) -> str:
        rows = ["| | " + " | ".join(f"**{s}**" for s in slugs) + " |"]
        rows.append("|" + "---|" * (len(slugs) + 1))
        for variant in VARIANTS:
            for quant in QUANTS:
                cells = []
                for slug in slugs:
                    r = by_key.get((quant, variant, slug))
                    if r is None:
                        cells.append("—")
                    elif r["status"] == "FAIL":
                        cells.append(f"⚠️ FAIL ({fmt.format(r[metric])})")
                    else:
                        cells.append(fmt.format(r[metric]))
                rows.append(
                    f"| **{quant}-{variant}** | " + " | ".join(cells) + " |"
                )
        return "\n".join(rows)

    def slowdown_matrix() -> str:
        rows = ["| | " + " | ".join(f"**{s}**" for s in slugs) + " |"]
        rows.append("|" + "---|" * (len(slugs) + 1))
        for variant in VARIANTS:
            cells = []
            for slug in slugs:
                a = by_key.get(("int8", variant, slug))
                b = by_key.get(("fp16", variant, slug))
                if a is None or b is None:
                    cells.append("—")
                else:
                    ratio = b["total_s"] / a["total_s"]
                    cells.append(f"{ratio:.2f}×")
            rows.append(
                f"| **{variant}** fp16/int8 | " + " | ".join(cells) + " |"
            )
        return "\n".join(rows)

    lines: list[str] = []
    lines.append("# Round 38 spike — baseline 测量结果\n")
    lines.append(
        "硬件:本地 Apple Silicon CPU,onnxruntime CPU EP。每 case warmup 1 次 + "
        f"{N_REPEATS} 次正式测量取 median。\n"
    )
    lines.append("## RTF(real-time factor = total_s / audio_seconds)\n")
    lines.append(matrix("rtf", "{:.2f}"))
    lines.append("\n## Total 推理时间(秒)\n")
    lines.append(matrix("total_s", "{:.2f}"))
    lines.append("\n## fp16 vs int8 slowdown(total_s 比值)\n")
    lines.append(slowdown_matrix())
    lines.append("\n## Encode 时间(秒)\n")
    lines.append(matrix("encode_s", "{:.2f}"))
    lines.append("\n## Prefill 时间(秒)\n")
    lines.append(matrix("prefill_s", "{:.2f}"))
    lines.append("\n## Decode 时间(秒,生成所有 token 的总和)\n")
    lines.append(matrix("decode_s", "{:.2f}"))
    lines.append("\n## Per-case 详情\n")
    for r in records:
        lines.append(
            f"- **{r['case_id']}** ({r['audio_seconds']:.2f}s): "
            f"status={r['status']} gen_tokens={r['generated_count']} "
            f"n_audio={r['n_audio_tokens']}\n"
            f"  - encode={r['encode_s']:.2f}s prefill={r['prefill_s']:.2f}s "
            f"decode={r['decode_s']:.2f}s total={r['total_s']:.2f}s "
            f"rtf={r['rtf']:.2f}\n"
            f"  - text: {r['text'][:120]!r}"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
