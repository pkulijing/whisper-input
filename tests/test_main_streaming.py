"""测试 WhisperInput 流式编排(28 轮新增)。

不启动真 sd / 真 STT,用 fake 收集 on_chunk / type_text 调用,验证:
1. _should_stream 决策正确(streaming_mode + stt.supports_streaming 双条件)
2. 流式按键路径 ≠ 离线路径(start_streaming 被调而不是 start)
3. on_chunk 累积到 ~2s 才 enqueue stream_step 给 worker
4. _do_stream_step 把 committed_delta paste 出去
5. KV overflow 兜底:不重抛,设置 overflow_hit,is_last=True 时结束 session
6. on_config_changed 的 streaming_mode 分支只切标志位
7. _on_stream_chunk 累计到 28s 会触发"接近上限"提示
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from whisper_input.__main__ import WhisperInput
from whisper_input.stt.base import (
    STREAMING_CHUNK_SAMPLES,
    StreamEvent,
    StreamingKVOverflowError,
)


@pytest.fixture
def fake_stt_streaming():
    """A fake STT with supports_streaming=True; stream_step returns a preset sequence."""
    stt = MagicMock()
    stt.supports_streaming = True
    stt.variant = "0.6B"
    stt.init_stream_state = MagicMock(return_value={"fake": "state"})
    return stt


@pytest.fixture
def fake_recorder():
    """A recorder whose start_streaming captures the on_chunk callback."""

    class FakeRecorder:
        def __init__(self):
            self.sample_rate = 16000
            self.channels = 1
            self.on_level = None
            self._on_chunk = None
            self.is_recording = False
            self.start_streaming_calls = 0
            self.stop_streaming_calls = 0
            self.start_calls = 0
            self.stop_calls = 0

        def start(self):
            self.start_calls += 1
            self.is_recording = True

        def stop(self):
            self.stop_calls += 1
            self.is_recording = False
            return b"fake-wav"

        def start_streaming(self, on_chunk):
            self.start_streaming_calls += 1
            self._on_chunk = on_chunk
            self.is_recording = True

        def stop_streaming(self):
            self.stop_streaming_calls += 1
            self.is_recording = False

        def push(self, chunk: np.ndarray):
            """手动触发 on_chunk(模拟 PortAudio callback)。"""
            assert self._on_chunk is not None
            self._on_chunk(chunk)

    return FakeRecorder()


@pytest.fixture
def wi(
    fake_stt_streaming, fake_recorder, monkeypatch
):
    """构造一个 WhisperInput,用 fake stt + fake recorder。"""
    monkeypatch.setattr(
        "whisper_input.__main__.create_stt_engine",
        lambda cfg: fake_stt_streaming,
    )
    # 替换 type_text(避免真的写到剪贴板)
    pasted: list[str] = []
    monkeypatch.setattr(
        "whisper_input.__main__.type_text",
        lambda text: pasted.append(text),
    )

    instance = WhisperInput(
        {
            "audio": {"sample_rate": 16000, "channels": 1},
            "sound": {"enabled": False},
            "tray_status": {"enabled": False},
            "overlay": {"enabled": False},
            "qwen3": {"variant": "0.6B", "streaming_mode": True},
        }
    )
    # 替换 recorder 为 fake
    instance.recorder = fake_recorder
    instance._pasted = pasted
    instance.start_worker()
    yield instance
    instance.stop_worker(timeout=1.0)


def _drain_worker(wi: WhisperInput, timeout: float = 2.0) -> None:
    """等所有 enqueued 事件处理完。Queue.join() 没有 mark_done,用 empty 轮询。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if wi._event_queue.empty() and not wi._processing:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"worker didn't drain: queue={wi._event_queue.qsize()}, "
        f"processing={wi._processing}"
    )


# --------------------------------------------------------------------------
# _should_stream 决策
# --------------------------------------------------------------------------


def test_should_stream_true_when_both_conditions_met(wi):
    assert wi._should_stream() is True


def test_should_stream_false_when_streaming_mode_off(wi):
    wi.streaming_mode = False
    assert wi._should_stream() is False


def test_should_stream_false_when_stt_doesnt_support(wi):
    wi.stt.supports_streaming = False
    assert wi._should_stream() is False


# --------------------------------------------------------------------------
# 按键分路
# --------------------------------------------------------------------------


def test_key_press_streaming_calls_start_streaming(wi, fake_recorder):
    """streaming_mode=True 的按键应走 start_streaming,不走 start。"""
    wi._do_key_press()
    assert fake_recorder.start_streaming_calls == 1
    assert fake_recorder.start_calls == 0
    assert wi._stream_state is not None


def test_key_press_offline_calls_start(wi, fake_recorder):
    """streaming_mode=False 的按键应走 start,不走 start_streaming。"""
    wi.streaming_mode = False
    wi._do_key_press()
    assert fake_recorder.start_calls == 1
    assert fake_recorder.start_streaming_calls == 0
    assert wi._stream_state is None


# --------------------------------------------------------------------------
# on_chunk 累积行为
# --------------------------------------------------------------------------


def test_on_chunk_accumulates_until_threshold(
    wi, fake_recorder, fake_stt_streaming
):
    """小 chunk 进来不触发 stream_step,累积到 ~2s 才触发。"""
    fake_stt_streaming.stream_step = MagicMock(
        return_value=StreamEvent(
            committed_delta="", pending_text="", is_final=False
        )
    )
    wi._do_key_press()
    # 各 push 1/4 STREAMING_CHUNK,3 次(= 0.75 * 2s = 1.5s),还不够
    quarter = STREAMING_CHUNK_SAMPLES // 4
    for _ in range(3):
        fake_recorder.push(
            np.zeros(quarter, dtype=np.float32)
        )
    time.sleep(0.05)  # 让 worker 有机会跑(实际应无动静)
    assert fake_stt_streaming.stream_step.call_count == 0

    # 第 4 次 push:累积 ≥ 2s,应触发 1 次 stream_step
    fake_recorder.push(np.zeros(quarter, dtype=np.float32))
    _drain_worker(wi)
    assert fake_stt_streaming.stream_step.call_count == 1


# --------------------------------------------------------------------------
# _do_stream_step 行为
# --------------------------------------------------------------------------


def test_stream_step_pastes_committed_delta(
    wi, fake_recorder, fake_stt_streaming
):
    """StreamEvent.committed_delta 非空 → type_text 被调。"""
    fake_stt_streaming.stream_step = MagicMock(
        return_value=StreamEvent(
            committed_delta="hello",
            pending_text="",
            is_final=False,
        )
    )
    wi._do_key_press()
    fake_recorder.push(
        np.zeros(STREAMING_CHUNK_SAMPLES, dtype=np.float32)
    )
    _drain_worker(wi)
    assert wi._pasted == ["hello"]


def test_key_release_triggers_final_flush(
    wi, fake_recorder, fake_stt_streaming
):
    """松手应 enqueue 一次 is_last=True 的 stream_step,最终 paste + finalize。"""
    call_args: list[tuple] = []

    def stream_step(chunk, state, is_last):
        call_args.append((chunk.size, is_last))
        if is_last:
            return StreamEvent(
                committed_delta="final",
                pending_text="",
                is_final=True,
            )
        return StreamEvent(
            committed_delta="", pending_text="", is_final=False
        )

    fake_stt_streaming.stream_step = stream_step

    wi._do_key_press()
    # 松手(立刻,没有任何音频累积)
    wi._do_key_release()
    _drain_worker(wi)

    # 应该有至少一次 is_last=True 调用
    assert any(is_last for _, is_last in call_args)
    # final paste 触发
    assert "final" in wi._pasted
    # session 结束:state 清掉,_processing=False
    assert wi._stream_state is None
    assert wi._processing is False


def test_stream_step_handles_kv_overflow(
    wi, fake_recorder, fake_stt_streaming
):
    """StreamingKVOverflowError 不应传到 worker 之外;session 被标记 overflow。"""
    fake_stt_streaming.stream_step = MagicMock(
        side_effect=StreamingKVOverflowError("fake overflow")
    )

    wi._do_key_press()
    fake_recorder.push(
        np.zeros(STREAMING_CHUNK_SAMPLES, dtype=np.float32)
    )
    _drain_worker(wi)

    assert wi._stream_overflow_hit is True
    # 后续 chunk 直接被跳过,stream_step 不再被调
    fake_recorder.push(
        np.zeros(STREAMING_CHUNK_SAMPLES, dtype=np.float32)
    )
    _drain_worker(wi)
    assert fake_stt_streaming.stream_step.call_count == 1


# --------------------------------------------------------------------------
# 配置变更
# --------------------------------------------------------------------------


def test_on_config_changed_streaming_mode_toggle(wi):
    """on_config_changed 应该更新 self.streaming_mode 标志位。"""
    assert wi.streaming_mode is True
    wi.on_config_changed({"qwen3.streaming_mode": False})
    assert wi.streaming_mode is False
    wi.on_config_changed({"qwen3.streaming_mode": True})
    assert wi.streaming_mode is True


# --------------------------------------------------------------------------
# init_stream_state 失败回落
# --------------------------------------------------------------------------


def test_key_press_falls_back_to_offline_on_stream_init_failure(
    wi, fake_recorder, fake_stt_streaming
):
    """init_stream_state 抛异常时应回落到累积模式(recorder.start())。"""
    fake_stt_streaming.init_stream_state = MagicMock(
        side_effect=RuntimeError("init failed")
    )

    wi._do_key_press()

    # 回落:stream_state 保持 None,recorder.start (accumulate) 被调
    assert wi._stream_state is None
    assert fake_recorder.start_calls == 1
    assert fake_recorder.start_streaming_calls == 0


# --------------------------------------------------------------------------
# 接近上限警告
# --------------------------------------------------------------------------


def test_near_limit_warning_fires_once(
    wi, fake_recorder, fake_stt_streaming
):
    """累计 ≥ 28s 音频时 _notify_near_limit 触发一次,之后不再重复触发。"""
    fake_stt_streaming.stream_step = MagicMock(
        return_value=StreamEvent(
            committed_delta="", pending_text="", is_final=False
        )
    )
    overlay = MagicMock()
    wi.overlay_enabled = True
    wi.set_overlay(overlay)
    wi._do_key_press()

    # 28s * 16000 = 448000 samples。分 15 次 2s chunks 推过去(共 30s)
    for _ in range(15):
        fake_recorder.push(
            np.zeros(STREAMING_CHUNK_SAMPLES, dtype=np.float32)
        )
    _drain_worker(wi)

    # _notify_near_limit 应该在累计超过 28s 时被调,之后不重复
    assert wi._stream_near_limit_warned is True
    near_limit_calls = [
        c for c in overlay.update.call_args_list
        if "识别上限" in str(c) or "limit" in str(c).lower()
        or "limite" in str(c).lower()
    ]
    assert len(near_limit_calls) == 1


def test_offline_key_release_unchanged(
    wi, fake_recorder, fake_stt_streaming
):
    """离线模式(streaming_mode=False)的松手应走 recorder.stop() + transcribe。"""
    wi.streaming_mode = False
    fake_stt_streaming.transcribe = MagicMock(return_value="offline text")

    wi._do_key_press()
    wi._do_key_release()
    # 等 daemon thread 完成(离线路径是 threading.Thread,不是 worker queue)
    time.sleep(0.2)
    _drain_worker(wi)

    assert fake_recorder.stop_calls == 1
    # fake recorder.stop() 返回 b"fake-wav",transcribe 会被调
    assert fake_stt_streaming.transcribe.called
