"""SenseVoice ONNX 模型下载器 —— 纯 stdlib 实现。

**必须纯 stdlib** —— debian/setup_window.py 和 macos/setup_window.py 的
引导向导会在 user venv 还没建好之前就调用本模块下载模型,此时只有
bundled python 的标准库可用。不要 import numpy / requests / onnxruntime。

调用流程:
  1. find_local_model() 命中本地 → 直接返回,零联网
  2. 否则按 MODEL_FILES 顺序从 ModelScope 逐文件下载
  3. 每个文件下载后校验 SHA256,不匹配就抛 ModelDownloadError
  4. 全部成功后写 manifest,返回目录路径

相比之前的 ghproxy + tar.bz2 方案:
  - 不需要 tar 解压
  - 不需要多源 failover(ModelScope 单源 CDN 稳定,国内实测 10 秒下 230 MB)
  - 不需要用户手动在 GitHub release 传模型
"""

import hashlib
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from stt.model_paths import (
    MODEL_FILES,
    MODEL_VERSION,
    TOTAL_BYTES,
    find_local_model,
    is_model_complete,
    modelscope_file_url,
    save_manifest,
    sense_voice_model_dir,
)

# (downloaded_bytes, total_bytes) — total 是 TOTAL_BYTES
ProgressCallback = Callable[[int, int], None]
# 单行日志回调,不传就打到 stdout
LogCallback = Callable[[str], None]


class ModelDownloadError(RuntimeError):
    """任意文件下载失败或 SHA256 不匹配时抛出。"""


_CHUNK = 256 * 1024  # 256 KB


def _log(log_cb: LogCallback | None, msg: str) -> None:
    if log_cb is not None:
        log_cb(msg)
    else:
        print(msg, flush=True)


def _sha256(path: Path) -> str:
    """计算文件 SHA256 十六进制字符串。"""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _download_one(
    url: str,
    target: Path,
    already_done_bytes: int,
    progress_cb: ProgressCallback | None,
) -> None:
    """下载 url 到 target。进度回调用"当前总累计字节"作为 done 参数。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": "whisper-input"}
    )
    with (
        urllib.request.urlopen(req, timeout=60) as resp,
        target.open("wb") as fh,
    ):
        local_done = 0
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            local_done += len(chunk)
            if progress_cb is not None:
                progress_cb(
                    already_done_bytes + local_done, TOTAL_BYTES
                )


def download_model(
    progress_cb: ProgressCallback | None = None,
    log_cb: LogCallback | None = None,
) -> Path:
    """确保 SenseVoice ONNX 模型就位,返回模型目录路径。

    幂等:本地已完整就直接返回,不联网。
    """
    local = find_local_model()
    if local is not None:
        _log(log_cb, f"[downloader] 命中本地模型: {local}")
        return local

    target_dir = sense_voice_model_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    _log(
        log_cb,
        f"[downloader] 开始下载 SenseVoice 模型到 {target_dir}"
        f" (共 {TOTAL_BYTES / 1024 / 1024:.0f} MB,5 个文件)",
    )

    cumulative = 0
    for idx, (repo, name, expected_size, expected_sha) in enumerate(
        MODEL_FILES, start=1
    ):
        dest = target_dir / name

        # 已经有同名完整文件就跳过(续跑场景)
        if dest.is_file() and dest.stat().st_size == expected_size:
            actual_sha = _sha256(dest)
            if actual_sha == expected_sha:
                _log(
                    log_cb,
                    f"[downloader] ({idx}/{len(MODEL_FILES)})"
                    f" 跳过已存在: {name}",
                )
                cumulative += expected_size
                if progress_cb is not None:
                    progress_cb(cumulative, TOTAL_BYTES)
                continue

        url = modelscope_file_url(repo, name)
        _log(
            log_cb,
            f"[downloader] ({idx}/{len(MODEL_FILES)}) 下载"
            f" {name} ({expected_size / 1024 / 1024:.1f} MB) ...",
        )

        try:
            _download_one(url, dest, cumulative, progress_cb)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise ModelDownloadError(
                f"下载 {name} 失败 (url={url}): {e}"
            ) from e

        actual_sha = _sha256(dest)
        if actual_sha != expected_sha:
            raise ModelDownloadError(
                f"{name} SHA256 不匹配\n"
                f"  url    : {url}\n"
                f"  期望   : {expected_sha}\n"
                f"  实际   : {actual_sha}"
            )

        actual_size = dest.stat().st_size
        if actual_size != expected_size:
            raise ModelDownloadError(
                f"{name} 体积不匹配: 期望 {expected_size} 字节,"
                f" 实际 {actual_size} 字节"
            )

        cumulative += expected_size
        _log(
            log_cb,
            f"[downloader] ({idx}/{len(MODEL_FILES)}) {name} OK",
        )

    if not is_model_complete(target_dir):
        raise ModelDownloadError(
            f"下载完成后模型文件不完整: {target_dir}"
        )

    save_manifest(MODEL_VERSION, target_dir)
    _log(log_cb, f"[downloader] 模型就绪: {target_dir}")
    return target_dir
