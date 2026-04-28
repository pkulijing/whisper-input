"""把 measure_case 的 record list 渲染成 JSON + Markdown 报告。

JSON 给机器读(后续选型脚本 / 趋势对比)、Markdown 给人读(矩阵表格 +
分析结论)。两者共享同一个 records list,人工分析段落由调用方追加。
"""

from __future__ import annotations

import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import onnxruntime as ort


def _git_sha_and_dirty() -> tuple[str, bool]:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        dirty_out = subprocess.check_output(
            ["git", "status", "--porcelain"], text=True
        ).strip()
        return sha, bool(dirty_out)
    except Exception:
        return "unknown", False


def machine_fingerprint() -> dict[str, Any]:
    """记录环境信息,run 文件头里写一份。"""
    sha, dirty = _git_sha_and_dirty()
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "python_version": platform.python_version(),
        "onnxruntime_version": ort.__version__,
        "git_sha": sha,
        "git_dirty": dirty,
        "timestamp_utc": datetime.now(UTC).isoformat(),
    }


def write_json(
    records: list[dict[str, Any]],
    fingerprint: dict[str, Any],
    out_path: Path,
) -> None:
    payload = {
        "fingerprint": fingerprint,
        "records": records,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------


def _matrix_table(
    records: list[dict[str, Any]],
    metric: str,
    fmt: str,
    fixture_slugs: list[str],
    backend_keys: list[str],
) -> str:
    by_key = {(r["backend_name"], r["fixture_slug"]): r for r in records}
    header = "| | " + " | ".join(f"**{s}**" for s in fixture_slugs) + " |"
    sep = "|" + "---|" * (len(fixture_slugs) + 1)
    rows = [header, sep]
    for bk in backend_keys:
        cells = []
        for slug in fixture_slugs:
            r = by_key.get((bk, slug))
            if r is None:
                cells.append("—")
                continue
            val = fmt.format(r[metric])
            if r["status"] == "FAIL":
                cells.append(f"⚠️ FAIL ({val})")
            elif r["status"] == "SLOW":
                cells.append(f"🐢 ({val})")
            else:
                cells.append(val)
        rows.append(f"| **{bk}** | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _slowdown_pairs_table(
    records: list[dict[str, Any]],
    fixture_slugs: list[str],
) -> str | None:
    """同 family + 同 variant + 不同 quant 之间的 slowdown(总时间比)。

    每个 family/variant pair 选两条 quant 时,基线选 ``int8`` 优先,其次
    任意非新条目。返回 None = 没有可对比 pair。
    """
    by_pair: dict[tuple[str, str, str], dict[str, Any]] = {}
    quants_per_group: dict[tuple[str, str], set[str]] = {}
    for r in records:
        key = (r["family"], r["variant"])
        quants_per_group.setdefault(key, set()).add(r["quant"])
        by_pair[(r["family"], r["variant"], r["quant"])] = r  # last wins ok
    # 实际我们要 by_pair[(family, variant, quant, slug)] 才完整;重新组
    by_full: dict[tuple[str, str, str, str], dict[str, Any]] = {
        (r["family"], r["variant"], r["quant"], r["fixture_slug"]): r
        for r in records
    }

    rows: list[str] = []
    for (family, variant), quants in sorted(quants_per_group.items()):
        if len(quants) < 2:
            continue
        baseline_quant = "int8" if "int8" in quants else sorted(quants)[0]
        compare_quants = sorted(q for q in quants if q != baseline_quant)
        for cq in compare_quants:
            cells = []
            for slug in fixture_slugs:
                base = by_full.get((family, variant, baseline_quant, slug))
                comp = by_full.get((family, variant, cq, slug))
                if base is None or comp is None:
                    cells.append("—")
                else:
                    cells.append(f"{comp['total_s'] / base['total_s']:.2f}×")
            rows.append(
                f"| **{family}-{variant}** {cq} / {baseline_quant} | "
                + " | ".join(cells)
                + " |"
            )
    if not rows:
        return None
    header = "| | " + " | ".join(f"**{s}**" for s in fixture_slugs) + " |"
    sep = "|" + "---|" * (len(fixture_slugs) + 1)
    return "\n".join([header, sep, *rows])


def render_markdown(
    records: list[dict[str, Any]],
    fingerprint: dict[str, Any],
    title: str = "Daobidao 推理性能 benchmark",
) -> str:
    fixture_slugs = []
    seen = set()
    for r in records:
        if r["fixture_slug"] not in seen:
            fixture_slugs.append(r["fixture_slug"])
            seen.add(r["fixture_slug"])

    backend_keys = []
    seen_b = set()
    for r in records:
        if r["backend_name"] not in seen_b:
            backend_keys.append(r["backend_name"])
            seen_b.add(r["backend_name"])

    lines: list[str] = []
    lines.append(f"# {title}\n")

    # 环境指纹块
    lines.append("## 环境指纹\n")
    lines.append("```")
    for k, v in fingerprint.items():
        lines.append(f"{k}: {v}")
    lines.append("```\n")

    lines.append("## RTF(real-time factor = total_s / audio_seconds)\n")
    lines.append(
        _matrix_table(records, "rtf", "{:.2f}", fixture_slugs, backend_keys)
    )
    lines.append("\n## Total 推理时间(秒)\n")
    lines.append(
        _matrix_table(records, "total_s", "{:.2f}", fixture_slugs, backend_keys)
    )

    slowdown = _slowdown_pairs_table(records, fixture_slugs)
    if slowdown:
        lines.append("\n## 同 family + variant 不同 quant 的 slowdown 比\n")
        lines.append(slowdown)

    lines.append("\n## Encode 时间(秒)\n")
    lines.append(
        _matrix_table(
            records, "encode_s", "{:.2f}", fixture_slugs, backend_keys
        )
    )
    lines.append("\n## Prefill 时间(秒)\n")
    lines.append(
        _matrix_table(
            records, "prefill_s", "{:.2f}", fixture_slugs, backend_keys
        )
    )
    lines.append("\n## Decode 时间(秒,生成所有 token 的总和)\n")
    lines.append(
        _matrix_table(
            records, "decode_s", "{:.2f}", fixture_slugs, backend_keys
        )
    )

    lines.append(
        "\n## 运行离散度(total_s 的 N 次正式 run 统计 —— median 之外的可信度参考)\n"
    )
    lines.append("| backend / fixture | runs | median | min | max | std | cv |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in records:
        raw = ", ".join(f"{run['total_s']:.2f}" for run in r["all_runs"])
        lines.append(
            f"| {r['case_id']} | [{raw}] | {r['total_s']:.2f}s | "
            f"{r['total_min_s']:.2f}s | {r['total_max_s']:.2f}s | "
            f"{r['total_std_s']:.2f}s | {r['total_cv'] * 100:.1f}% |"
        )

    lines.append("\n## Per-case 详情\n")
    for r in records:
        lines.append(
            f"- **{r['case_id']}** ({r['audio_seconds']:.2f}s): "
            f"status={r['status']} gen_tokens={r['generated_count']} "
            f"n_audio={r['n_audio_tokens']}\n"
            f"  - encode={r['encode_s']:.2f}s prefill={r['prefill_s']:.2f}s "
            f"decode={r['decode_s']:.2f}s total={r['total_s']:.2f}s "
            f"(min {r['total_min_s']:.2f} / max {r['total_max_s']:.2f} / "
            f"std {r['total_std_s']:.2f} / cv {r['total_cv'] * 100:.1f}%) "
            f"rtf={r['rtf']:.2f}\n"
            f"  - text: {r['text'][:120]!r}"
        )
    lines.append("")
    return "\n".join(lines)
