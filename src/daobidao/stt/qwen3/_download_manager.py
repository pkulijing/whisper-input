"""模型变体下载管理器 — 36 轮"模型管理与可视化下载"。

封装 modelscope cache 检查 + 后台下载线程 + 进度状态 + 取消信号。
跟 ``Qwen3ASRSTT.load()`` 平级地各自调用 ``modelscope.snapshot_download``
(不引入抽象层),DownloadManager 只负责把文件下到磁盘,session 构造留给
真正切到该 variant 时由 load() 处理。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

# 顶层 import 让测试能 patch 到 _download_manager 模块上的符号
from modelscope import (
    snapshot_download,  # noqa: F401  (re-exported for patching)
)
from modelscope.hub.callback import ProgressCallback

from daobidao.logger import get_logger

logger = get_logger(__name__)

VARIANTS = ("0.6B", "1.7B")

# Round 37: baicai1145 fp16 export 是 per-variant 独立 repo,不再是
# zengshuishui 那种单 repo 内嵌 model_{variant}/ 子目录的 layout。
REPO_BY_VARIANT: dict[str, str] = {
    "0.6B": "baicai1145/Qwen3-ASR-0.6B-ONNX",
    "1.7B": "baicai1145/Qwen3-ASR-1.7B-ONNX",
}
REPO_OWNER_NAME_BY_VARIANT: dict[str, tuple[str, str]] = {
    "0.6B": ("baicai1145", "Qwen3-ASR-0___6B-ONNX"),
    "1.7B": ("baicai1145", "Qwen3-ASR-1___7B-ONNX"),
}

# 每个 variant 必需的核心文件 — 走 baicai1145 layout(repo 根目录平铺)。
# 检查 4 个核心文件全在视为已下载,任一缺失(用户外部 rm)报未下载。
# tokenizer / metadata.json 等小文件每次 snapshot_download 都跟着下,不入清单。
REQUIRED_FILES: dict[str, list[str]] = {
    "0.6B": [
        "encoder.onnx",
        "encoder.onnx.data",
        "decoder.onnx",
        "decoder.onnx.data",
    ],
    "1.7B": [
        "encoder.onnx",
        "encoder.onnx.data",
        "decoder.onnx",
        "decoder.onnx.data",
    ],
}

# 跟 qwen3_asr.py 的 _ALLOW_PATTERNS 同集合;复制而非 import 避免循环依赖。
_ALLOW_PATTERNS = [
    "encoder.onnx",
    "encoder.onnx.data",
    "decoder.onnx",
    "decoder.onnx.data",
    "*.json",
    "*.txt",
    "*.jinja",
]


def _empty_state() -> dict[str, Any]:
    return {
        "downloaded": False,
        "downloading": False,
        "received_bytes": 0,
        "total_bytes": 0,
        "speed_bps": 0.0,
        "eta_seconds": 0,
        "error": None,
        "cancelled": False,
    }


class DownloadManager:
    """单实例,管理 0.6B / 1.7B 两个 variant 的下载状态与触发。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {
            v: _empty_state() for v in VARIANTS
        }
        # "全局单 active 下载":同一时刻只允许下一个 variant
        self._active_variant: str | None = None
        # 速度计算用的滑动窗口(只跟当前活跃下载有关,变更 variant 时清空)
        # 元素 (monotonic_seconds, cumulative_received_bytes)
        self._byte_log: deque[tuple[float, int]] = deque(maxlen=128)
        # 取消信号:set 后下一次 callback.update 会抛 _DownloadCancelled
        self._cancel_event = threading.Event()

    def variant_states(self) -> dict[str, dict[str, Any]]:
        """返回每个 variant 当前状态的浅拷贝(直接给 _send_json 用)。

        ``downloaded`` 字段每次都重新跑 cache 检查,反映磁盘实时状态(用户
        外部 rm 后下次 GET 自动报 False)。``eta_seconds`` 实时计算。
        """
        with self._lock:
            snap = {v: dict(s) for v, s in self._state.items()}
        # cache 检查在锁外做(可能 I/O)
        for v in VARIANTS:
            snap[v]["downloaded"] = self.is_variant_downloaded(v)
            s = snap[v]
            remaining = max(0, s["total_bytes"] - s["received_bytes"])
            speed = s["speed_bps"]
            s["eta_seconds"] = int(remaining / speed) if speed > 0 else 0
        return snap

    def is_variant_downloaded(self, variant: str) -> bool:
        """检查 variant 必需的所有核心文件是否都在 modelscope cache 里。

        走 modelscope 官方 ``ModelFileSystemCache.get_file_by_path``,该方法
        自带磁盘一致性兜底:索引说有但磁盘没有时自动清掉索引并返 None。
        """
        if variant not in REQUIRED_FILES:
            return False
        for rel_path in REQUIRED_FILES[variant]:
            if self._cache_lookup(variant, rel_path) is None:
                return False
        return True

    def _cache_lookup(self, variant: str, rel_path: str) -> str | None:
        """单文件 cache 查询。抽出独立方法方便测试 patch。"""
        from modelscope.hub.file_download import (
            ModelFileSystemCache,
            get_model_cache_root,
        )

        owner, name = REPO_OWNER_NAME_BY_VARIANT[variant]
        cache = ModelFileSystemCache(
            get_model_cache_root(),
            owner=owner,
            name=name,
        )
        return cache.get_file_by_path(rel_path)

    # ------------------------------------------------------------------
    # start() — 触发后台下载
    # ------------------------------------------------------------------

    def start(self, variant: str) -> tuple[bool, str | None]:
        """触发后台下载。

        返回 (accepted, reason):
        - accepted=True:已起后台线程,reason=None
        - accepted=False:reason 为 i18n key
          (``invalid_variant`` / ``already_downloaded`` / ``busy``)
        """
        if variant not in REQUIRED_FILES:
            return False, "invalid_variant"
        if self.is_variant_downloaded(variant):
            return False, "already_downloaded"

        with self._lock:
            if self._active_variant is not None:
                return False, "busy"
            self._active_variant = variant
            # 重置该 variant 的进度状态 + 速度窗口 + cancel 信号
            self._state[variant] = _empty_state()
            self._state[variant]["downloading"] = True
            self._byte_log.clear()
            self._cancel_event.clear()

        threading.Thread(
            target=self._worker,
            args=(variant,),
            name=f"model-download-{variant}",
            daemon=True,
        ).start()
        logger.info("model_download_start", variant=variant)
        return True, None

    def cancel(self, variant: str) -> bool:
        """取消正在跑的下载。

        - 仅当当前 active variant 等于参数时才生效(防止误取消)
        - 设置 _cancel_event,worker 内 callback.update 下次抛 _DownloadCancelled
        - 返回是否真正发起了取消
        """
        with self._lock:
            if self._active_variant != variant:
                return False
            self._cancel_event.set()
            logger.info("model_download_cancel_requested", variant=variant)
            return True

    def _worker(self, variant: str) -> None:
        """后台线程:跑 snapshot_download 拉文件,捕获错误写 state。"""
        from daobidao.stt.qwen3 import _download_manager as mod

        try:
            cb_class = _make_callback_class(self, variant)
            mod.snapshot_download(
                REPO_BY_VARIANT[variant],
                allow_patterns=_ALLOW_PATTERNS,
                progress_callbacks=[cb_class],
            )
            logger.info("model_download_done", variant=variant)
        except _DownloadCancelled:
            with self._lock:
                self._state[variant]["cancelled"] = True
            logger.info("model_download_cancelled", variant=variant)
        except Exception as exc:
            with self._lock:
                self._state[variant]["error"] = repr(exc)
            logger.exception("model_download_failed", variant=variant)
        finally:
            with self._lock:
                self._state[variant]["downloading"] = False
                self._active_variant = None

    # ------------------------------------------------------------------
    # progress 累加 — callback 工厂里调用
    # ------------------------------------------------------------------

    def _on_file_start(
        self, variant: str, filename: str, file_size: int
    ) -> None:
        """新文件开始下:累加到 total_bytes。"""
        with self._lock:
            self._state[variant]["total_bytes"] += file_size

    def _on_bytes(self, variant: str, increment: int) -> None:
        """收到一块 chunk:累加 received_bytes,更新速度窗口。

        modelscope ``ProgressCallback.update(size)`` 的 size 是**增量**
        (file_download.py 第 435 行 ``callback.update(len(chunk))``)。
        """
        with self._lock:
            s = self._state[variant]
            s["received_bytes"] += increment
            now = time.monotonic()
            self._byte_log.append((now, s["received_bytes"]))
            # 砍掉 1s 之前的样本
            while self._byte_log and now - self._byte_log[0][0] > 1.0:
                self._byte_log.popleft()
            # 速度 = (window 末端 bytes - 头部 bytes) / Δt
            if len(self._byte_log) >= 2:
                dt = self._byte_log[-1][0] - self._byte_log[0][0]
                db = self._byte_log[-1][1] - self._byte_log[0][1]
                s["speed_bps"] = db / dt if dt > 0 else 0.0
            else:
                s["speed_bps"] = 0.0

    def _on_file_end(self, variant: str) -> None:
        """单文件下完。这里暂不做特殊处理,留 hook 以后扩展。"""


class _DownloadCancelled(BaseException):
    """取消信号专用异常。

    继承 ``BaseException`` 而非 ``Exception``,防止 modelscope retry 装饰器内
    的 ``except Exception`` 误吞 — 我们要它直冲 worker 的顶层捕获。
    """


def _make_callback_class(mgr: DownloadManager, variant: str) -> type:
    """工厂函数:返回一个绑定到指定 mgr+variant 的 ProgressCallback 子类。

    modelscope 对每个文件 ``instantiate(filename, file_size)`` 一次,所以
    我们传的是 class(不是 instance)。闭包让多个文件实例都能写到同一个
    DownloadManager 的 state 上。
    """

    class _Tracker(ProgressCallback):
        def __init__(self, filename: str, file_size: int):
            super().__init__(filename, file_size)
            mgr._on_file_start(variant, filename, file_size)

        def update(self, size: int) -> None:
            mgr._on_bytes(variant, size)
            if mgr._cancel_event.is_set():
                raise _DownloadCancelled()

        def end(self) -> None:
            mgr._on_file_end(variant)

    return _Tracker
