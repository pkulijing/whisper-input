"""Backend 协议 —— benchmark harness 跟具体模型实现之间的契约。

所有 adapter 必须暴露 ``encode_audio`` / ``alloc_caches`` / ``decoder_step``
+ ``eos_ids`` 集合。harness 用这套接口跑 transcribe 计时,不感知 ONNX /
GGUF / MLX 等底层实现。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class Backend(Protocol):
    """Backend 接口 —— Qwen3-ASR 系列(encoder + autoregressive decoder)风格。

    Attributes
    ----------
    name:
        全局唯一标识,CLI ``--backend`` 用 prefix 匹配。约定
        ``<family>-<quant>-<source>-<variant>``,例如
        ``qwen3-fp16-baicai1145-1.7B``。
    family:
        模型家族,用来分类报告里的"非同家族不直接对比"。例如 ``qwen3``。
    variant:
        模型大小标识,用来在矩阵里横向分组。例如 ``0.6B`` / ``1.7B``。
    quant:
        量化精度标识。例如 ``fp16`` / ``int8`` / ``gguf-q4_k_m``。
    eos_ids:
        EOS token id 集合。greedy decode 命中其中任一就停。
    """

    name: str
    family: str
    variant: str
    quant: str
    eos_ids: set[int]

    def load(self) -> None:
        """惰性 load(下载 / 实例化 ONNX session 等)。harness 在第一次用前调用。"""

    def encode_audio(self, audio: np.ndarray) -> np.ndarray:
        """audio (float32 mono 16 kHz) → audio_features。

        返回 shape 由 backend 决定,但末两维必须是 ``(n_audio_tokens, dim)``,
        让 harness 用 ``af.shape[-2]`` 拿 audio token 数。
        """

    def alloc_caches(self) -> Any:
        """分配 decoder KV cache。具体类型(list / dict)由 backend 自定义。"""

    def decoder_step(
        self,
        input_ids: np.ndarray,
        audio_features: np.ndarray,
        caches: Any,
        cache_position: np.ndarray,
    ) -> np.ndarray:
        """单次 decoder forward。

        Returns
        -------
        np.ndarray
            logits,shape 必须保证 ``logits[0, -1]`` 取得到最后位置 vocab 分布。
            建议 ``(1, seq, vocab)`` 或 ``(1, 1, vocab)``。
        """


def get_tokenizer_for(backend: Backend):
    """让 backend 自带 tokenizer 还是 harness 共享 —— 留给 adapter 决定。

    本函数仅作占位 / 文档 hook。实际实现由各 adapter 的 ``discover()``
    返回 backend 实例时一并设置 ``backend.tokenizer`` 属性,harness 直接
    用 ``backend.tokenizer.encode/decode``。
    """
    return getattr(backend, "tokenizer", None)
