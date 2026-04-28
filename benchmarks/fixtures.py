"""音频 fixture 定义 —— 共用 ``tests/fixtures/*.wav``,不复制文件。

每个 fixture 描述:slug / 源 wav / 切片秒数。harness 按 slug 索引。
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_FIXTURES = REPO_ROOT / "tests" / "fixtures"


@dataclass(frozen=True)
class AudioFixture:
    slug: str
    wav_path: Path
    n_samples: int

    @property
    def seconds(self) -> float:
        return self.n_samples / SAMPLE_RATE


# Round 38 调研轮选定的三段切片(详见 docs/38-推理性能benchmark/PLAN.md):
# - short / medium / long 都避开 round 37 SUMMARY 的 1.7B int8 翻车谱
# - 不超 30s,确保老 int8 静态 cache (max_total_len=1200) 装得下
FIXTURES: list[AudioFixture] = [
    AudioFixture(
        slug="short",
        wav_path=TESTS_FIXTURES / "zh.wav",
        n_samples=5 * SAMPLE_RATE,  # 80000
    ),
    AudioFixture(
        slug="medium",
        wav_path=TESTS_FIXTURES / "zh.wav",
        n_samples=int(10.5 * SAMPLE_RATE),  # 168000 — int8 1.7B 安全对照点
    ),
    AudioFixture(
        slug="long",
        wav_path=TESTS_FIXTURES / "zh_long.wav",
        n_samples=25 * SAMPLE_RATE,  # 400000 — 8s~28s 安全区
    ),
]
FIXTURES_BY_SLUG = {f.slug: f for f in FIXTURES}


def load_audio(fixture: AudioFixture) -> np.ndarray:
    """16-bit PCM mono WAV → float32 [-1, 1] 1D array,按 fixture.n_samples 切片。"""
    raw_bytes = fixture.wav_path.read_bytes()
    buf = io.BytesIO(raw_bytes)
    with wave.open(buf, "rb") as wf:
        if wf.getframerate() != SAMPLE_RATE:
            raise ValueError(
                f"{fixture.wav_path} 采样率 {wf.getframerate()},预期 {SAMPLE_RATE}"
            )
        if wf.getnchannels() != 1:
            raise ValueError(
                f"{fixture.wav_path} 通道数 {wf.getnchannels()},预期 1(mono)"
            )
        raw = wf.readframes(wf.getnframes())
    full = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if len(full) < fixture.n_samples:
        raise RuntimeError(
            f"{fixture.wav_path} 长度 {len(full)} 不够切 {fixture.n_samples} samples"
        )
    return full[: fixture.n_samples].astype(np.float32, copy=False)


def resolve_fixtures(filters: list[str] | None) -> list[AudioFixture]:
    """CLI ``--fixture`` 解析 —— 支持 prefix 匹配 + 逗号分隔。"""
    if not filters:
        return list(FIXTURES)
    selected: list[AudioFixture] = []
    for f in FIXTURES:
        for pat in filters:
            if f.slug.startswith(pat):
                selected.append(f)
                break
    if not selected:
        raise SystemExit(
            f"--fixture filter {filters!r} 没匹配任何 fixture。可选:"
            + ",".join(f.slug for f in FIXTURES)
        )
    return selected
