"""SentencepiecesTokenizer —— 从 funasr_onnx 移植的 BPE tokenizer 薄封装。

Port 来源:
  https://github.com/modelscope/FunASR/blob/main/runtime/python/onnxruntime/funasr_onnx/utils/sentencepiece_tokenizer.py

作者 / 版权:
  Copyright FunASR (https://github.com/alibaba-damo-academy/FunASR)
  MIT License (https://opensource.org/licenses/MIT)
  Speech Lab of DAMO Academy, Alibaba Group

只在 SenseVoice 推理里用到 decode(List[int]) -> str,但保留了 encode 方向
以便未来可能需要。底层是 Google 官方 sentencepiece 库的 SentencePieceProcessor。
"""

from collections.abc import Iterable
from pathlib import Path

import sentencepiece as spm


class SentencepiecesTokenizer:
    def __init__(self, bpemodel: Path | str, **kwargs):
        super().__init__(**kwargs)
        self.bpemodel = str(bpemodel)
        self.sp = None
        self._build_sentence_piece_processor()

    def __repr__(self):
        return f'{self.__class__.__name__}(model="{self.bpemodel}")'

    def _build_sentence_piece_processor(self):
        if self.sp is None:
            self.sp = spm.SentencePieceProcessor()
            self.sp.load(self.bpemodel)

    def text2tokens(self, line: str) -> list[str]:
        self._build_sentence_piece_processor()
        return self.sp.EncodeAsPieces(line)

    def tokens2text(self, tokens: Iterable[str]) -> str:
        self._build_sentence_piece_processor()
        return self.sp.DecodePieces(list(tokens))

    def encode(self, line: str, **kwargs) -> list[int]:
        self._build_sentence_piece_processor()
        return self.sp.EncodeAsIds(line)

    def decode(self, line: list[int], **kwargs) -> str:
        self._build_sentence_piece_processor()
        return self.sp.DecodeIds(line)

    def get_vocab_size(self) -> int:
        return self.sp.GetPieceSize()
