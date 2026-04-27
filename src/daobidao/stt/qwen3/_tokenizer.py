"""Thin wrapper over HuggingFace ``tokenizers`` library for Qwen3-ASR.

Qwen3-ASR's ``tokenizer/`` directory ships ``vocab.json`` + ``merges.txt`` +
``tokenizer_config.json`` but NO ``tokenizer.json`` (fast-tokenizer snapshot).
We rebuild a byte-level BPE tokenizer at load time from those files and
register the 62 added tokens (``<|im_start|>``, ``<|audio_pad|>``,
``<asr_text>``, the 27 ``<blankN>`` tokens, etc.) so that the chat-style
prompt encodes to the exact token IDs the decoder was trained on.

Byte-level BPE implementation choice:
    - ``BPE.from_file(vocab, merges)`` for the merge table
    - ``pre_tokenizers.ByteLevel(add_prefix_space=False, use_regex=True)`` —
      matches ``Qwen2Tokenizer``'s GPT-2 style regex split
    - ``decoders.ByteLevel()`` for the inverse mapping
"""

from __future__ import annotations

import json
from pathlib import Path

from tokenizers import AddedToken, Tokenizer, decoders, pre_tokenizers
from tokenizers.models import BPE


class Qwen3Tokenizer:
    """Encode / decode text using Qwen3-ASR's byte-level BPE tokenizer."""

    def __init__(self, tokenizer_dir: Path):
        tokenizer_dir = Path(tokenizer_dir)
        vocab = tokenizer_dir / "vocab.json"
        merges = tokenizer_dir / "merges.txt"
        config_path = tokenizer_dir / "tokenizer_config.json"
        for path in (vocab, merges, config_path):
            if not path.exists():
                raise FileNotFoundError(f"Tokenizer file missing: {path}")

        tok = Tokenizer(BPE.from_file(vocab=str(vocab), merges=str(merges)))
        tok.pre_tokenizer = pre_tokenizers.ByteLevel(
            add_prefix_space=False, use_regex=True
        )
        tok.decoder = decoders.ByteLevel()

        config = json.loads(config_path.read_text(encoding="utf-8"))
        added_tokens = _build_added_tokens(
            config.get("added_tokens_decoder", {})
        )
        tok.add_tokens(added_tokens)

        self._tokenizer = tok
        self._added_token_ids: set[int] = set()
        self._special_token_ids: set[int] = set()
        for tid_str, info in config.get("added_tokens_decoder", {}).items():
            tid = int(tid_str)
            self._added_token_ids.add(tid)
            if info.get("special", False):
                self._special_token_ids.add(tid)

        # Commonly referenced token IDs (None if the vocab lacks them).
        self.eos_id = tok.token_to_id("<|im_end|>")
        self.pad_id = tok.token_to_id("<|endoftext|>")
        self.im_start_id = tok.token_to_id("<|im_start|>")
        self.audio_start_id = tok.token_to_id("<|audio_start|>")
        self.audio_end_id = tok.token_to_id("<|audio_end|>")
        self.audio_pad_id = tok.token_to_id("<|audio_pad|>")
        self.asr_text_id = tok.token_to_id("<asr_text>")

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def encode(self, text: str) -> list[int]:
        """Encode text to token IDs (special tokens are honored)."""
        return self._tokenizer.encode(text).ids

    def decode(
        self,
        ids: list[int] | tuple[int, ...],
        skip_special_tokens: bool = True,
    ) -> str:
        """Decode token IDs back to a string.

        ``skip_special_tokens``: when True, drops the 41 tokens marked
        ``special=True`` in ``tokenizer_config.json`` (matches HF
        ``AutoTokenizer.decode(..., skip_special_tokens=True)``). Non-special
        added tokens such as ``<asr_text>`` are kept — strip those yourself at
        the ``_postprocess`` layer.
        """
        if skip_special_tokens:
            filtered = [i for i in ids if i not in self._special_token_ids]
        else:
            filtered = list(ids)
        return self._tokenizer.decode(filtered, skip_special_tokens=False)

    def token_to_id(self, token: str) -> int | None:
        return self._tokenizer.token_to_id(token)

    def id_to_token(self, token_id: int) -> str | None:
        return self._tokenizer.id_to_token(token_id)

    @property
    def vocab_size(self) -> int:
        return self._tokenizer.get_vocab_size()

    @property
    def special_token_ids(self) -> set[int]:
        """IDs of tokens marked ``special=True`` in tokenizer_config."""
        return set(self._special_token_ids)


def _build_added_tokens(
    decoder_map: dict[str, dict],
) -> list[AddedToken]:
    """Convert ``added_tokens_decoder`` JSON → list[AddedToken] sorted by id.

    Sorting by id keeps the relative token order stable — the ``tokenizers``
    library assigns IDs in insertion order, but since we pass the exact
    vocab + merges the IDs are already locked; we still sort for determinism
    in case of future vocab changes.
    """
    out: list[AddedToken] = []
    for _tid_str, info in sorted(
        decoder_map.items(), key=lambda item: int(item[0])
    ):
        out.append(
            AddedToken(
                info["content"],
                lstrip=info.get("lstrip", False),
                rstrip=info.get("rstrip", False),
                normalized=info.get("normalized", False),
                single_word=info.get("single_word", False),
                special=info.get("special", False),
            )
        )
    return out


def build_qwen3_tokenizer(tokenizer_dir: Path) -> Qwen3Tokenizer:
    """Convenience constructor (parallels ``Qwen3Tokenizer(tokenizer_dir)``)."""
    return Qwen3Tokenizer(tokenizer_dir)
