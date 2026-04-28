"""Benchmark 全局配置常量。"""

from __future__ import annotations

# 每个 (backend, fixture) case 跑 1 次 warmup + 这么多次正式测量,取 median。
N_REPEATS = 3

# decoder greedy 生成上限 —— 跟产品代码 `_MAX_NEW_TOKENS` 对齐。
MAX_NEW_TOKENS = 400

# 默认 backend adapter 模块 —— CLI 不带 --backend 时,枚举这个列表。
# 每个模块必须暴露 `discover() -> list[Backend]`。
DEFAULT_BACKEND_MODULES = [
    "benchmarks.backends.qwen3_int8_zengshuishui",
    "benchmarks.backends.qwen3_fp16_baicai1145",
]
