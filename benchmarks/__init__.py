"""Daobidao 推理性能 benchmark 框架。

跟 ``tests/`` 同级的顶层目录,跑一次重一次(~3-15min,~7GB 模型 cache),
不进 pytest 也不进 CI。CLI 入口:``uv run python -m benchmarks``。

加新 backend:在 ``benchmarks/backends/`` 下新增 ``qwen3_<quant>_<source>.py``
模块,实现 ``Backend`` 协议(见 ``backends/base.py``)并暴露
``def discover() -> list[Backend]``。详见 ``benchmarks/README.md``。
"""
