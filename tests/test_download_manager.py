"""DownloadManager 单元测试 — 36 轮"模型管理与可视化下载"。

测试策略:全部 monkeypatch ``modelscope.snapshot_download`` 和
``ModelFileSystemCache.get_file_by_path``,不真下网络。snapshot_download
mock 让它**手动驱动 progress_callbacks 序列**(实例化 callback class →
跑一系列 update(chunk) → end),这样 DownloadManager 行为可重复验证。
"""

from __future__ import annotations

from unittest.mock import patch

from daobidao.stt.qwen3._download_manager import (
    REQUIRED_FILES,
    DownloadManager,
)


def test_initial_state_all_idle() -> None:
    """实例化后每个 variant 的 state 字段齐全且 downloading=False。"""
    mgr = DownloadManager()
    states = mgr.variant_states()

    assert set(states.keys()) == {"0.6B", "1.7B"}
    for variant in ("0.6B", "1.7B"):
        s = states[variant]
        # 字段齐全(给前端用)
        assert "downloaded" in s
        assert "downloading" in s
        assert "received_bytes" in s
        assert "total_bytes" in s
        assert "speed_bps" in s
        assert "eta_seconds" in s
        assert "error" in s
        assert "cancelled" in s
        # 初始态:不在下
        assert s["downloading"] is False
        assert s["received_bytes"] == 0
        assert s["error"] is None


def test_variant_states_returns_copy_not_reference() -> None:
    """变更返回的 dict 不应该污染内部状态。"""
    mgr = DownloadManager()
    states = mgr.variant_states()
    states["0.6B"]["received_bytes"] = 999_999

    fresh = mgr.variant_states()
    assert fresh["0.6B"]["received_bytes"] == 0


def test_required_files_covers_both_variants() -> None:
    """REQUIRED_FILES 常量必须列出每个 variant 的核心 .onnx 文件。"""
    assert set(REQUIRED_FILES.keys()) == {"0.6B", "1.7B"}
    for _variant, files in REQUIRED_FILES.items():
        # Round 37 baicai1145 layout: encoder.onnx + decoder.onnx + 各自 .data
        assert "encoder.onnx" in files
        assert "encoder.onnx.data" in files
        assert "decoder.onnx" in files
        assert "decoder.onnx.data" in files


def test_is_variant_downloaded_true_when_all_files_present() -> None:
    """所有必需文件都被 cache 索引指向有效路径 → True。"""
    mgr = DownloadManager()
    with patch.object(
        mgr,
        "_cache_lookup",
        return_value="/fake/path/some_file.onnx",
    ):
        assert mgr.is_variant_downloaded("0.6B") is True


def test_is_variant_downloaded_false_when_any_file_missing() -> None:
    """任一必需文件 lookup 返 None → False。"""
    mgr = DownloadManager()
    # 第一次返路径,第二次返 None(模拟某个文件被手动 rm 后 cache 索引兜底)
    return_values = iter(["/fake/path/a.onnx", None, None, None, None, None])

    def fake_lookup(_variant: str, _path: str) -> str | None:
        return next(return_values)

    with patch.object(mgr, "_cache_lookup", side_effect=fake_lookup):
        assert mgr.is_variant_downloaded("0.6B") is False


def test_is_variant_downloaded_invalid_variant() -> None:
    """非法 variant → False(防御性,避免上层崩)。"""
    mgr = DownloadManager()
    assert mgr.is_variant_downloaded("99B") is False


def test_variant_states_reflects_cache_check() -> None:
    """variant_states 返回的 downloaded 字段应该实时反映 cache 检查结果。"""
    mgr = DownloadManager()
    with patch.object(mgr, "_cache_lookup", return_value="/fake/path"):
        states = mgr.variant_states()
        assert states["0.6B"]["downloaded"] is True
        assert states["1.7B"]["downloaded"] is True

    with patch.object(mgr, "_cache_lookup", return_value=None):
        states = mgr.variant_states()
        assert states["0.6B"]["downloaded"] is False
        assert states["1.7B"]["downloaded"] is False


# ----------------------------------------------------------------------------
# start() — 触发后台下载
# ----------------------------------------------------------------------------


def test_start_invalid_variant_returns_invalid_reason() -> None:
    mgr = DownloadManager()
    accepted, reason = mgr.start("99B")
    assert accepted is False
    assert reason == "invalid_variant"


def test_start_when_already_downloaded_is_noop() -> None:
    mgr = DownloadManager()
    with patch.object(mgr, "_cache_lookup", return_value="/fake/path"):
        accepted, reason = mgr.start("0.6B")
    assert accepted is False
    assert reason == "already_downloaded"
    # 状态不变(没有起线程)
    assert mgr.variant_states()["0.6B"]["downloading"] is False


def test_start_when_idle_sets_downloading_and_runs_thread() -> None:
    """variant 未下载且无活跃下载时,start() 应该:
    - accept=True
    - state['downloading']=True
    - 起一个后台线程跑 snapshot_download
    - worker 跑完后 state['downloading'] 翻回 False
    """
    import threading
    import time

    started = threading.Event()
    finished = threading.Event()

    def fake_snapshot(repo_id, **kwargs):
        started.set()
        # 让 worker 跑一小段然后返回成功
        time.sleep(0.05)
        finished.set()
        return "/fake/cache/dir"

    mgr = DownloadManager()
    with (
        patch.object(mgr, "_cache_lookup", return_value=None),
        patch(
            "daobidao.stt.qwen3._download_manager.snapshot_download",
            side_effect=fake_snapshot,
        ),
    ):
        accepted, reason = mgr.start("1.7B")
        assert accepted is True
        assert reason is None

        # 等线程开始跑
        assert started.wait(timeout=2.0)
        # downloading 期间应是 True
        assert mgr.variant_states()["1.7B"]["downloading"] is True

        # 等 worker 跑完
        assert finished.wait(timeout=2.0)
        # 给 worker finally 块清理 state 一点时间
        for _ in range(100):
            if not mgr.variant_states()["1.7B"]["downloading"]:
                break
            time.sleep(0.01)
        assert mgr.variant_states()["1.7B"]["downloading"] is False


def _drive_callback_sequence(
    progress_callbacks: list,
    files: list[tuple[str, int]],
    chunk_size: int = 1024 * 1024,
) -> None:
    """模拟 modelscope 内部:对每个文件实例化 callback class →
    跑一系列 update(chunk) → end。
    """
    for fname, fsize in files:
        for cb_cls in progress_callbacks:
            cb = cb_cls(fname, fsize)
            sent = 0
            while sent < fsize:
                chunk = min(chunk_size, fsize - sent)
                cb.update(chunk)
                sent += chunk
            cb.end()


def test_progress_callback_passes_class_to_snapshot() -> None:
    """worker 应该把 callback class 传给 snapshot_download(不是 instance)。"""
    captured = {}

    def fake_snapshot(repo_id, **kwargs):
        captured["progress_callbacks"] = kwargs.get("progress_callbacks")
        return "/fake"

    mgr = DownloadManager()
    with (
        patch.object(mgr, "_cache_lookup", return_value=None),
        patch(
            "daobidao.stt.qwen3._download_manager.snapshot_download",
            side_effect=fake_snapshot,
        ),
    ):
        mgr.start("1.7B")
        # 等 worker 跑完
        import time

        for _ in range(200):
            if not mgr.variant_states()["1.7B"]["downloading"]:
                break
            time.sleep(0.01)

    callbacks = captured.get("progress_callbacks")
    assert callbacks is not None
    assert len(callbacks) == 1
    # 是 class 不是 instance
    assert isinstance(callbacks[0], type)


def test_progress_callback_accumulates_bytes() -> None:
    """单文件下 N 个 chunk,received_bytes 应该等于所有 chunk 之和。"""
    chunks_seen: list[int] = []

    def fake_snapshot(repo_id, **kwargs):
        cbs = kwargs.get("progress_callbacks") or []
        cb = cbs[0]("model_1.7B/encoder.int8.onnx", 5_000_000)
        for size in (1_000_000, 2_000_000, 1_500_000, 500_000):
            cb.update(size)
            chunks_seen.append(size)
        cb.end()
        return "/fake"

    mgr = DownloadManager()
    with (
        patch.object(mgr, "_cache_lookup", return_value=None),
        patch(
            "daobidao.stt.qwen3._download_manager.snapshot_download",
            side_effect=fake_snapshot,
        ),
    ):
        mgr.start("1.7B")
        # 等 worker 跑完
        import time

        for _ in range(200):
            if not mgr.variant_states()["1.7B"]["downloading"]:
                break
            time.sleep(0.01)

    state = mgr.variant_states()["1.7B"]
    assert state["received_bytes"] == sum(chunks_seen)
    assert state["total_bytes"] == 5_000_000


def test_progress_callback_total_bytes_sum_across_files() -> None:
    """多文件下载,total_bytes 应该是所有 file_size 之和。"""

    def fake_snapshot(repo_id, **kwargs):
        cbs = kwargs.get("progress_callbacks") or []
        _drive_callback_sequence(
            cbs,
            files=[
                ("model_1.7B/conv_frontend.onnx", 100_000),
                ("model_1.7B/encoder.int8.onnx", 1_500_000_000),
                ("model_1.7B/decoder.int8.onnx", 800_000_000),
            ],
        )
        return "/fake"

    mgr = DownloadManager()
    with (
        patch.object(mgr, "_cache_lookup", return_value=None),
        patch(
            "daobidao.stt.qwen3._download_manager.snapshot_download",
            side_effect=fake_snapshot,
        ),
    ):
        mgr.start("1.7B")
        import time

        for _ in range(500):
            if not mgr.variant_states()["1.7B"]["downloading"]:
                break
            time.sleep(0.01)

    state = mgr.variant_states()["1.7B"]
    assert state["total_bytes"] == 100_000 + 1_500_000_000 + 800_000_000
    assert state["received_bytes"] == state["total_bytes"]


def test_speed_window_excludes_old_samples(monkeypatch) -> None:
    """1s 之前的样本不参与速度计算。

    模拟 monotonic 时间推进:
    - t=0:  收 1MB → log [(0, 1MB)]
    - t=0.5: 收 2MB → log [(0, 1MB), (0.5, 3MB)] → 速度 = (3-1)/0.5 = 4MB/s
    - t=1.6:收 4MB → 0.5s 那个还在窗口(1.6-0.5=1.1>1.0,被踢),
      log [(1.6, 7MB)] → len < 2,速度按要求处理(0 或保持上次)
    """
    fake_time = [0.0]

    def fake_monotonic() -> float:
        return fake_time[0]

    monkeypatch.setattr(
        "daobidao.stt.qwen3._download_manager.time.monotonic",
        fake_monotonic,
    )

    mgr = DownloadManager()
    # 直接调内部钩子驱动 progress(不经 snapshot_download)
    mgr._on_file_start("0.6B", "x.onnx", 100_000_000)

    fake_time[0] = 0.0
    mgr._on_bytes("0.6B", 1_000_000)

    fake_time[0] = 0.5
    mgr._on_bytes("0.6B", 2_000_000)
    state = mgr.variant_states()["0.6B"]
    # 速度 = (3MB - 1MB) / 0.5s = 4 MB/s
    assert abs(state["speed_bps"] - 4_000_000) < 1

    fake_time[0] = 1.7  # 距 0.5s 那条 1.2s,已超 1s,被踢
    mgr._on_bytes("0.6B", 4_000_000)
    # 窗口里只剩当前这条,len<2 → 退化处理(我们设为 0)
    state = mgr.variant_states()["0.6B"]
    assert state["received_bytes"] == 7_000_000


def test_eta_calculation() -> None:
    """ETA = (total - received) / max(speed, 1)。"""
    mgr = DownloadManager()
    mgr._on_file_start("0.6B", "x.onnx", 100_000_000)
    # 手动写一组 received / speed 来验证 eta
    with mgr._lock:
        mgr._state["0.6B"]["received_bytes"] = 20_000_000
        mgr._state["0.6B"]["speed_bps"] = 8_000_000  # 8 MB/s

    state = mgr.variant_states()["0.6B"]
    # 剩 80MB / 8MB/s = 10s
    assert state["eta_seconds"] == 10


def test_concurrent_start_returns_busy() -> None:
    """正在下 1.7B 时,再次 start("1.7B") 或 start("0.6B") 都应该返 busy。"""
    import threading

    unblock = threading.Event()
    started = threading.Event()

    def slow_snapshot(repo_id, **kwargs):
        started.set()
        unblock.wait(timeout=5.0)
        return "/fake"

    mgr = DownloadManager()
    with (
        patch.object(mgr, "_cache_lookup", return_value=None),
        patch(
            "daobidao.stt.qwen3._download_manager.snapshot_download",
            side_effect=slow_snapshot,
        ),
    ):
        accepted, _ = mgr.start("1.7B")
        assert accepted is True
        assert started.wait(timeout=2.0)

        # 再次启动:同 variant
        accepted2, reason2 = mgr.start("1.7B")
        assert accepted2 is False
        assert reason2 == "busy"

        # 不同 variant 也应该被拒
        accepted3, reason3 = mgr.start("0.6B")
        assert accepted3 is False
        assert reason3 == "busy"

        unblock.set()
        # 等 worker 跑完
        import time

        for _ in range(200):
            if not mgr.variant_states()["1.7B"]["downloading"]:
                break
            time.sleep(0.01)


# ----------------------------------------------------------------------------
# cancel — 取消活跃下载
# ----------------------------------------------------------------------------


def test_cancel_when_idle_returns_false() -> None:
    """没活跃下载时 cancel 是 no-op,返 False。"""
    mgr = DownloadManager()
    assert mgr.cancel("1.7B") is False


def test_cancel_wrong_variant_returns_false() -> None:
    """正在下 1.7B 时 cancel("0.6B") 不该取消 1.7B。"""
    import threading

    unblock = threading.Event()
    started = threading.Event()

    def slow_snapshot(repo_id, **kwargs):
        started.set()
        unblock.wait(timeout=5.0)
        return "/fake"

    mgr = DownloadManager()
    with (
        patch.object(mgr, "_cache_lookup", return_value=None),
        patch(
            "daobidao.stt.qwen3._download_manager.snapshot_download",
            side_effect=slow_snapshot,
        ),
    ):
        mgr.start("1.7B")
        assert started.wait(timeout=2.0)
        # cancel 错误的 variant 应该 no-op
        assert mgr.cancel("0.6B") is False
        # 1.7B 仍在下
        assert mgr.variant_states()["1.7B"]["downloading"] is True
        unblock.set()
        import time

        for _ in range(200):
            if not mgr.variant_states()["1.7B"]["downloading"]:
                break
            time.sleep(0.01)


def test_cancel_sets_event_and_callback_raises() -> None:
    """cancel 应该 set _cancel_event,使后续 callback.update 抛 _DownloadCancelled。"""
    from daobidao.stt.qwen3._download_manager import _make_callback_class

    mgr = DownloadManager()
    # 模拟一个正在下的 variant(直接置 active 不走 start)
    with mgr._lock:
        mgr._active_variant = "1.7B"
        mgr._state["1.7B"]["downloading"] = True

    cb_class = _make_callback_class(mgr, "1.7B")
    cb = cb_class("model_1.7B/encoder.int8.onnx", 1_000_000)

    # cancel 之前,update 正常
    cb.update(1024)
    state = mgr.variant_states()["1.7B"]
    assert state["received_bytes"] == 1024

    # cancel 之后,下次 update 抛
    assert mgr.cancel("1.7B") is True

    import pytest

    from daobidao.stt.qwen3._download_manager import _DownloadCancelled

    with pytest.raises(_DownloadCancelled):
        cb.update(1024)


def test_cancel_marks_state_cancelled_and_clears_active() -> None:
    """cancel + worker 退出后,state['cancelled']=True、active=None、可再次 start。"""
    import threading
    import time

    started = threading.Event()

    def fake_snapshot(repo_id, **kwargs):
        started.set()
        cbs = kwargs.get("progress_callbacks") or []
        cb = cbs[0]("x.onnx", 1_000_000)
        # 跑几次 update,期间会被 cancel
        for _ in range(100):
            cb.update(1024)
            time.sleep(0.005)
        cb.end()
        return "/fake"

    mgr = DownloadManager()
    with (
        patch.object(mgr, "_cache_lookup", return_value=None),
        patch(
            "daobidao.stt.qwen3._download_manager.snapshot_download",
            side_effect=fake_snapshot,
        ),
    ):
        mgr.start("1.7B")
        assert started.wait(timeout=2.0)
        # 让它跑一小段
        time.sleep(0.05)
        assert mgr.cancel("1.7B") is True

        # 等 worker 跑完
        for _ in range(200):
            if not mgr.variant_states()["1.7B"]["downloading"]:
                break
            time.sleep(0.01)

            state = mgr.variant_states()["1.7B"]
            assert state["downloading"] is False
            assert state["cancelled"] is True
            # active_variant 已清空,允许再次 start(在 patch 内,cache_lookup 仍返 None)
            assert mgr._active_variant is None


def test_eta_zero_speed_returns_safe_value() -> None:
    """speed=0 时 eta 应该是合理值(避免除零),不应该 raise。"""
    mgr = DownloadManager()
    with mgr._lock:
        mgr._state["0.6B"]["received_bytes"] = 0
        mgr._state["0.6B"]["total_bytes"] = 1_000_000
        mgr._state["0.6B"]["speed_bps"] = 0

    state = mgr.variant_states()["0.6B"]
    # speed=0 → eta 应该是 0(无意义占位)或一个大整数,不能崩
    assert isinstance(state["eta_seconds"], int)


def test_start_propagates_snapshot_error_to_state() -> None:
    """snapshot_download 抛异常时,worker 捕获并写到 state['error']。"""
    import time

    def fake_snapshot(repo_id, **kwargs):
        raise RuntimeError("network down")

    mgr = DownloadManager()
    with (
        patch.object(mgr, "_cache_lookup", return_value=None),
        patch(
            "daobidao.stt.qwen3._download_manager.snapshot_download",
            side_effect=fake_snapshot,
        ),
    ):
        accepted, _ = mgr.start("1.7B")
        assert accepted is True

        # 等 worker 跑完(失败)
        for _ in range(200):
            state = mgr.variant_states()["1.7B"]
            if not state["downloading"]:
                break
            time.sleep(0.01)
        state = mgr.variant_states()["1.7B"]
        assert state["downloading"] is False
        assert state["error"] is not None
        assert "network down" in state["error"]
