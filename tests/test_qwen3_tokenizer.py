"""Tests for ``daobidao.stt.qwen3._tokenizer``.

Requires the tokenizer files to be cached locally (see
``qwen3_tokenizer_dir`` fixture in ``conftest.py``). When the cache is
missing, tests are skipped rather than failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daobidao.stt.qwen3._tokenizer import (
    Qwen3Tokenizer,
    build_qwen3_tokenizer,
)


@pytest.fixture(scope="module")
def tokenizer(qwen3_tokenizer_dir: Path) -> Qwen3Tokenizer:
    return Qwen3Tokenizer(qwen3_tokenizer_dir)


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def test_build_tokenizer_factory_equivalent(
    qwen3_tokenizer_dir: Path,
) -> None:
    a = Qwen3Tokenizer(qwen3_tokenizer_dir)
    b = build_qwen3_tokenizer(qwen3_tokenizer_dir)
    assert a.vocab_size == b.vocab_size
    assert a.encode("hello") == b.encode("hello")


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Qwen3Tokenizer(tmp_path)


# --------------------------------------------------------------------------
# Known special token IDs (asserted against tokenizer_config.json)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content,expected_id",
    [
        ("<|endoftext|>", 151643),
        ("<|im_start|>", 151644),
        ("<|im_end|>", 151645),
        ("<|audio_start|>", 151669),
        ("<|audio_end|>", 151670),
        ("<|audio_pad|>", 151676),
        ("<asr_text>", 151704),
    ],
)
def test_special_token_ids_match_config(
    tokenizer: Qwen3Tokenizer, content: str, expected_id: int
) -> None:
    assert tokenizer.token_to_id(content) == expected_id


def test_commonly_used_id_attributes(tokenizer: Qwen3Tokenizer) -> None:
    assert tokenizer.eos_id == 151645
    assert tokenizer.pad_id == 151643
    assert tokenizer.im_start_id == 151644
    assert tokenizer.audio_start_id == 151669
    assert tokenizer.audio_end_id == 151670
    assert tokenizer.audio_pad_id == 151676
    assert tokenizer.asr_text_id == 151704


def test_special_token_ids_set_includes_expected(
    tokenizer: Qwen3Tokenizer,
) -> None:
    ids = tokenizer.special_token_ids
    # All audio-related chat-template tokens are marked special in config
    assert {151643, 151644, 151645, 151669, 151670, 151676}.issubset(ids)
    # <asr_text> is NOT special in the config (special=False), so it should
    # NOT be in this set — decoders should surface it for _postprocess.
    assert 151704 not in ids


def test_vocab_size(tokenizer: Qwen3Tokenizer) -> None:
    assert tokenizer.vocab_size == 151705


# --------------------------------------------------------------------------
# Encode / decode
# --------------------------------------------------------------------------


def test_encode_decode_roundtrip_ascii(tokenizer: Qwen3Tokenizer) -> None:
    text = "hello world"
    ids = tokenizer.encode(text)
    assert tokenizer.decode(ids) == text


def test_encode_decode_roundtrip_cjk(tokenizer: Qwen3Tokenizer) -> None:
    text = "今天要部署 kubernetes 集群"
    ids = tokenizer.encode(text)
    assert tokenizer.decode(ids) == text


def test_encode_prompt_yields_expected_special_tokens(
    tokenizer: Qwen3Tokenizer,
) -> None:
    prompt = "<|im_start|>user\nhi<|im_end|>"
    ids = tokenizer.encode(prompt)
    assert ids[0] == tokenizer.im_start_id
    assert ids[-1] == tokenizer.eos_id


def test_decode_skips_special_by_default(
    tokenizer: Qwen3Tokenizer,
) -> None:
    ids = tokenizer.encode("<|im_start|>user\nhello<|im_end|>")
    out = tokenizer.decode(ids)  # skip_special_tokens=True by default
    assert "<|im_start|>" not in out
    assert "<|im_end|>" not in out
    assert "hello" in out


def test_decode_can_keep_special(tokenizer: Qwen3Tokenizer) -> None:
    ids = tokenizer.encode("<|im_start|>x<|im_end|>")
    out = tokenizer.decode(ids, skip_special_tokens=False)
    assert out == "<|im_start|>x<|im_end|>"


def test_decode_preserves_asr_text_marker(
    tokenizer: Qwen3Tokenizer,
) -> None:
    # <asr_text> is a non-special added token, so it must NOT be dropped even
    # when skip_special_tokens=True — postprocess depends on seeing it.
    marker_id = tokenizer.asr_text_id
    assert marker_id is not None
    out = tokenizer.decode([marker_id], skip_special_tokens=True)
    assert "<asr_text>" in out


def test_id_to_token_lookup(tokenizer: Qwen3Tokenizer) -> None:
    assert tokenizer.id_to_token(151645) == "<|im_end|>"
    # Unknown id returns None (tokenizers library behavior)
    assert tokenizer.id_to_token(9999999) is None


def test_empty_string_encode(tokenizer: Qwen3Tokenizer) -> None:
    assert tokenizer.encode("") == []
    assert tokenizer.decode([]) == ""
