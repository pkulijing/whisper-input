"""Tests for ``whisper_input.stt.qwen3._downloader``.

The real ``snapshot_download`` call is mocked so these tests run without
network access and without model-sized downloads. We assert that the right
``allow_patterns`` are passed and variant validation behaves as expected.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from whisper_input.stt.qwen3._downloader import (
    REPO_ID,
    VALID_VARIANTS,
    download_qwen3_asr,
)


def test_valid_variants_tuple_exactly():
    assert VALID_VARIANTS == ("0.6B", "1.7B")


@pytest.mark.parametrize("variant", ["", "0.6", "1.7", "large", "0.6b"])
def test_download_rejects_unknown_variant(variant: str):
    with pytest.raises(ValueError, match="unknown variant"):
        download_qwen3_asr(variant)


def test_download_calls_snapshot_with_expected_allow_patterns(
    tmp_path: Path,
):
    fake_root = tmp_path / "modelscope-cache"
    fake_root.mkdir()

    def fake_snapshot(repo_id, allow_patterns, **kwargs):
        assert repo_id == REPO_ID
        assert set(allow_patterns) == {
            "model_0.6B/conv_frontend.onnx",
            "model_0.6B/encoder.int8.onnx",
            "model_0.6B/decoder.int8.onnx",
            "tokenizer/*",
        }
        return str(fake_root)

    with patch("modelscope.snapshot_download") as mock_snap:
        mock_snap.side_effect = fake_snapshot
        out = download_qwen3_asr("0.6B")

    assert out == fake_root
    assert mock_snap.call_count == 1


def test_download_1_7b_uses_1_7b_patterns(tmp_path: Path):
    fake_root = tmp_path / "cache"
    fake_root.mkdir()

    with patch("modelscope.snapshot_download") as mock_snap:
        mock_snap.return_value = str(fake_root)
        download_qwen3_asr("1.7B")

    kwargs = mock_snap.call_args.kwargs
    allow_patterns = kwargs["allow_patterns"]
    assert "model_1.7B/conv_frontend.onnx" in allow_patterns
    assert "model_1.7B/encoder.int8.onnx" in allow_patterns
    assert "model_1.7B/decoder.int8.onnx" in allow_patterns
    assert "tokenizer/*" in allow_patterns
    # Must NOT pull the other variant
    assert not any("0.6B" in p for p in allow_patterns)


def test_download_returns_path_type(tmp_path: Path):
    with patch("modelscope.snapshot_download") as mock_snap:
        mock_snap.return_value = str(tmp_path)
        out = download_qwen3_asr("0.6B")
    assert isinstance(out, Path)
    assert out == tmp_path


def test_repo_id_is_zengshuishui():
    assert REPO_ID == "zengshuishui/Qwen3-ASR-onnx"


# --------------------------------------------------------------------------
# Round 27: local-only fast path + force_network
# --------------------------------------------------------------------------

def test_download_local_only_hit_skips_network(tmp_path: Path):
    """cache 命中时 local_files_only=True 一把过,不再发第二次请求。"""
    fake_root = tmp_path / "cache"
    fake_root.mkdir()

    with patch("modelscope.snapshot_download") as mock_snap:
        mock_snap.return_value = str(fake_root)
        out = download_qwen3_asr("0.6B")

    assert out == fake_root
    assert mock_snap.call_count == 1
    assert mock_snap.call_args.kwargs["local_files_only"] is True


def test_download_local_only_miss_falls_back_to_network(tmp_path: Path):
    """local_only ValueError → 自动降级到完整 snapshot_download。"""
    fake_root = tmp_path / "cache"
    fake_root.mkdir()
    call_log: list[bool] = []

    def side_effect(repo_id, **kwargs):
        call_log.append(bool(kwargs.get("local_files_only", False)))
        if kwargs.get("local_files_only"):
            raise ValueError(
                "Cannot find the requested files in the cached path "
                "and outgoing traffic has been disabled."
            )
        return str(fake_root)

    with patch("modelscope.snapshot_download", side_effect=side_effect):
        out = download_qwen3_asr("0.6B")

    assert out == fake_root
    assert call_log == [True, False]


def test_download_local_only_swallows_os_error(tmp_path: Path):
    """不只是 ValueError,OSError / 其它 Exception 也要走 fallback。"""
    fake_root = tmp_path / "cache"
    fake_root.mkdir()
    seen: list[bool] = []

    def side_effect(repo_id, **kwargs):
        seen.append(bool(kwargs.get("local_files_only", False)))
        if kwargs.get("local_files_only"):
            raise OSError("simulated I/O error during cache probe")
        return str(fake_root)

    with patch("modelscope.snapshot_download", side_effect=side_effect):
        out = download_qwen3_asr("0.6B")

    assert out == fake_root
    assert seen == [True, False]


def test_download_force_network_skips_local_only(tmp_path: Path):
    """force_network=True 跳过 local_only,直接走正常路径。"""
    fake_root = tmp_path / "cache"
    fake_root.mkdir()

    with patch("modelscope.snapshot_download") as mock_snap:
        mock_snap.return_value = str(fake_root)
        out = download_qwen3_asr("0.6B", force_network=True)

    assert out == fake_root
    assert mock_snap.call_count == 1
    assert "local_files_only" not in mock_snap.call_args.kwargs
