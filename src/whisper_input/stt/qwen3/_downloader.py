"""Download Qwen3-ASR weights + tokenizer from ModelScope.

ModelScope is the sole distribution source. Repo
``zengshuishui/Qwen3-ASR-onnx`` hosts both variants side-by-side:

    model_0.6B/{conv_frontend,encoder.int8,decoder.int8}.onnx
    model_1.7B/{conv_frontend,encoder.int8,decoder.int8}.onnx
    tokenizer/{vocab.json,merges.txt,tokenizer_config.json, ...}

``allow_patterns`` restricts the download to the requested variant so users
don't pull 3+ GB when they only want 0.6B.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from whisper_input.logger import get_logger

logger = get_logger(__name__)

REPO_ID = "zengshuishui/Qwen3-ASR-onnx"
Variant = Literal["0.6B", "1.7B"]
VALID_VARIANTS: tuple[Variant, ...] = ("0.6B", "1.7B")


def download_qwen3_asr(
    variant: str, *, force_network: bool = False
) -> Path:
    """Fetch the ONNX bundle + tokenizer for the given variant.

    Parameters
    ----------
    variant:
        ``"0.6B"`` (default choice, ~990 MiB) or ``"1.7B"`` (~2.4 GiB).
    force_network:
        If ``True``, skip the local-only fast path and always hit
        ModelScope's manifest check. Used by the corrupt-file fallback
        in ``Qwen3ASRSTT.load`` to force a re-download.

    Returns
    -------
    Path
        Root directory under ModelScope's cache; callers pass
        ``root / f"model_{variant}"`` to the ONNX runner and
        ``root / "tokenizer"`` to the tokenizer.
    """
    if variant not in VALID_VARIANTS:
        raise ValueError(
            f"unknown variant {variant!r}; expected one of {VALID_VARIANTS}"
        )

    # Lazy import so `--help`, tests that mock the downloader, and the
    # module-import path don't pay the modelscope cost.
    from modelscope import snapshot_download

    allow_patterns = [
        f"model_{variant}/conv_frontend.onnx",
        f"model_{variant}/encoder.int8.onnx",
        f"model_{variant}/decoder.int8.onnx",
        "tokenizer/*",
    ]

    # Cache-hit fast path: saves the ~1.5–2.4s manifest round-trip. On any
    # failure (missing files → ValueError, other I/O quirks → OSError etc.)
    # fall through to the full network path and let modelscope self-heal.
    if not force_network:
        try:
            root = snapshot_download(
                REPO_ID,
                allow_patterns=allow_patterns,
                local_files_only=True,
            )
            logger.info("qwen3_snapshot_local_only_hit", variant=variant)
            return Path(root)
        except Exception as exc:
            logger.info(
                "qwen3_snapshot_local_only_miss",
                variant=variant,
                reason=type(exc).__name__,
            )

    root = snapshot_download(REPO_ID, allow_patterns=allow_patterns)
    return Path(root)
