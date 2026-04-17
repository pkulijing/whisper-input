"""测试 WhisperInput 的热键事件 worker 机制。

对应 docs/22-热键回调死锁修复/。核心不变量：

1. on_key_press / on_key_release 是系统回调线程入口，必须立刻返回，
   绝不能同步执行 recorder / play_sound / overlay 等可能阻塞的动作
2. 入队的任务由 worker 串行执行（保证 press 先于 release）
3. worker 里抛异常不能让线程挂掉，后续事件仍要被处理
4. stop_worker 能在合理时间内让 worker 退出
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from whisper_input.__main__ import WhisperInput


@pytest.fixture
def wi(monkeypatch):
    """构造一个最小化的 WhisperInput：stt / recorder / overlay 全部 mock。"""
    # 阻止真正的 create_stt 被触发（会尝试 import onnxruntime/modelscope）
    monkeypatch.setattr(
        "whisper_input.__main__.create_stt_engine",
        lambda cfg: MagicMock(),
    )
    instance = WhisperInput(
        {
            "audio": {"sample_rate": 16000, "channels": 1},
            "sound": {"enabled": False},
            "tray_status": {"enabled": False},
            "overlay": {"enabled": False},
        }
    )
    instance.recorder = MagicMock()
    instance.recorder.is_recording = True
    instance.recorder.stop.return_value = b""  # 无音频 → _process 不被触发
    yield instance
    instance.stop_worker(timeout=2.0)


def test_on_key_press_returns_immediately_without_worker(wi):
    """worker 没启动时，on_key_press 也必须立刻返回，不碰 recorder。"""
    start = time.perf_counter()
    wi.on_key_press()
    elapsed = time.perf_counter() - start
    assert elapsed < 0.01, f"on_key_press 阻塞了 {elapsed * 1000:.2f}ms"
    assert not wi.recorder.start.called


def test_worker_executes_enqueued_press(wi):
    """worker 启动后，入队的 press 事件会真正触发 recorder.start。"""
    wi.start_worker()
    wi.on_key_press()

    # worker 是异步的，等最多 1s
    for _ in range(100):
        if wi.recorder.start.called:
            break
        time.sleep(0.01)
    assert wi.recorder.start.called


def test_worker_serializes_press_then_release(wi):
    """press → release 入队顺序必须被 worker 严格保留。"""
    order: list[str] = []
    order_lock = threading.Lock()

    def fake_start():
        with order_lock:
            order.append("start")
        time.sleep(0.05)  # 故意慢一点，确保不是并行跑的

    def fake_stop():
        with order_lock:
            order.append("stop")
        return b""

    wi.recorder.start.side_effect = fake_start
    wi.recorder.stop.side_effect = fake_stop

    wi.start_worker()
    wi.on_key_press()
    wi.on_key_release()

    for _ in range(200):
        if len(order) >= 2:
            break
        time.sleep(0.01)
    assert order == ["start", "stop"]


def test_worker_survives_exception(wi):
    """worker 里一次抛异常后，后续事件仍然要被处理。"""
    call_count = 0

    def flaky_start():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")

    wi.recorder.start.side_effect = flaky_start

    wi.start_worker()
    wi.on_key_press()  # 会抛
    # 第二次 press 之前把 _processing 清零（_do_key_release 走不到这里）
    wi._processing = False
    wi.on_key_press()  # 应该仍然被执行

    for _ in range(100):
        if call_count >= 2:
            break
        time.sleep(0.01)
    assert call_count == 2


def test_stop_worker_joins_cleanly(wi):
    """stop_worker 必须能在 timeout 内让线程退出。"""
    wi.start_worker()
    thread = wi._worker_thread
    assert thread is not None and thread.is_alive()

    start = time.perf_counter()
    wi.stop_worker(timeout=2.0)
    elapsed = time.perf_counter() - start
    assert not thread.is_alive()
    assert elapsed < 2.0
    assert wi._worker_thread is None


def test_start_worker_is_idempotent(wi):
    """重复调 start_worker 不能创建多个线程。"""
    wi.start_worker()
    first = wi._worker_thread
    wi.start_worker()
    assert wi._worker_thread is first


def test_stop_worker_without_start_is_noop(wi):
    """没 start 过也能 stop，不报错。"""
    assert wi._worker_thread is None
    wi.stop_worker()  # 不应抛
