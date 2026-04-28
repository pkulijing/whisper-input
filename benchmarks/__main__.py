"""Daobidao benchmark CLI 入口。

跑法:
    uv run python -m benchmarks                          # 默认全量
    uv run python -m benchmarks --backend qwen3-fp16     # prefix match,跑所有 fp16 backend
    uv run python -m benchmarks --fixture short,medium   # 多 fixture 用逗号分
    uv run python -m benchmarks --output benchmarks/results/baselines/2026-04-28_apple-silicon_round38

输出默认写到 ``benchmarks/results/<utc-stamp>_<git_sha7>.{json,md}``,
``--output <stem>`` 自定义 stem(不带后缀)。
"""

from __future__ import annotations

import argparse
import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.backends.base import Backend
from benchmarks.config import DEFAULT_BACKEND_MODULES, N_REPEATS
from benchmarks.fixtures import FIXTURES, resolve_fixtures
from benchmarks.harness import measure_case
from benchmarks.reporting import (
    machine_fingerprint,
    render_markdown,
    write_json,
)


def _split_csv(arg: str | None) -> list[str]:
    if not arg:
        return []
    return [s.strip() for s in arg.split(",") if s.strip()]


def discover_all_backends() -> list[Backend]:
    backends: list[Backend] = []
    for mod_path in DEFAULT_BACKEND_MODULES:
        mod = importlib.import_module(mod_path)
        if not hasattr(mod, "discover"):
            print(f"  ⚠️  {mod_path} 没有 discover() 函数,跳过")
            continue
        backends.extend(mod.discover())
    return backends


def filter_backends(
    backends: list[Backend], filters: list[str]
) -> list[Backend]:
    if not filters:
        return backends
    selected: list[Backend] = []
    for b in backends:
        for pat in filters:
            if b.name.startswith(pat):
                selected.append(b)
                break
    if not selected:
        names = ",".join(b.name for b in backends)
        raise SystemExit(
            f"--backend filter {filters!r} 没匹配任何 backend。可选:{names}"
        )
    return selected


def default_output_stem() -> Path:
    """``benchmarks/results/<UTC stamp>_<git sha7>``。"""
    fp = machine_fingerprint()
    sha = fp["git_sha"][:7] if fp["git_sha"] != "unknown" else "nogit"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(__file__).resolve().parent / "results" / f"{stamp}_{sha}"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks", description="Daobidao 推理性能 benchmark"
    )
    parser.add_argument(
        "--backend",
        default="",
        help="prefix-match backend 名,逗号分隔。空=全部",
    )
    parser.add_argument(
        "--fixture",
        default="",
        help="prefix-match fixture slug,逗号分隔。空=全部",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="输出文件 stem(不含后缀),默认 results/<stamp>_<sha7>",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=N_REPEATS,
        help=f"每 case 正式测量次数(默认 {N_REPEATS})",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="每 case warmup 次数(默认 1,不计入 median)",
    )
    parser.add_argument(
        "--title",
        default="Daobidao 推理性能 benchmark",
        help="Markdown 报告标题",
    )
    args = parser.parse_args()

    backend_filters = _split_csv(args.backend)
    fixture_filters = _split_csv(args.fixture)

    print("=" * 70)
    print("Daobidao 推理性能 benchmark")
    print("=" * 70)

    print("\n>> 枚举 backend")
    all_backends = discover_all_backends()
    backends = filter_backends(all_backends, backend_filters)
    print(f"   选中 {len(backends)}/{len(all_backends)} 个 backend:")
    for b in backends:
        print(f"     - {b.name}")

    print("\n>> 选 fixture")
    fixtures = resolve_fixtures(fixture_filters)
    print(f"   选中 {len(fixtures)}/{len(FIXTURES)} 个 fixture:")
    for f in fixtures:
        print(f"     - {f.slug} ({f.seconds:.2f}s, {f.wav_path.name})")

    records: list[dict[str, Any]] = []
    for backend in backends:
        print(f"\n>> 加载 backend {backend.name}")
        backend.load()
        for fx in fixtures:
            rec = measure_case(
                backend,
                fx,
                n_repeats=args.repeats,
                n_warmup=args.warmup,
            )
            records.append(rec)

    fingerprint = machine_fingerprint()

    stem = Path(args.output) if args.output else default_output_stem()
    # 不用 with_suffix —— stem 名字含 "." 会被当后缀截掉(如 "fp16-1.7B"
    # → with_suffix 误把 ".7B" 当后缀替换),直接拼后缀更安全
    json_path = stem.parent / f"{stem.name}.json"
    md_path = stem.parent / f"{stem.name}.md"

    write_json(records, fingerprint, json_path)
    print(f"\n✓ JSON: {json_path}")

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(records, fingerprint, title=args.title))
    print(f"✓ Markdown: {md_path}")


if __name__ == "__main__":
    main()
