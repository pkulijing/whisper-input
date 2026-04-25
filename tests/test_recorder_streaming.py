"""测试 AudioRecorder.start_streaming / stop_streaming(28 轮新增)。

不跑真 PortAudio,用 FakeInputStream 注入到 recorder 模块的 `sd` 名字上。
手动触发 fake stream 的 callback,验证:
- on_chunk 收到的是 float32 [-1,1] 1D array(不是原始 int16 shape)
- on_level 依然被调(两种模式共用音量回调)
- stop_streaming 会 stop + close 底层 InputStream
- 累积模式和流式模式互斥 + 回落正确
"""

from __future__ import annotations

import types

import numpy as np
import pytest


class FakeInputStream:
    """Mock of ``sd.InputStream``。保留 callback,支持手动 fire 模拟音频到达。"""

    def __init__(self, samplerate, channels, dtype, callback):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.callback = callback
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def fire(self, indata: np.ndarray, status=None) -> None:
        """测试手动触发 callback 模拟一次 audio 帧到达。"""
        self.callback(indata, indata.shape[0], None, status)


@pytest.fixture
def fake_sd(monkeypatch):
    """把 recorder 模块里的 `sd` 对象替换成带 FakeInputStream 的假 module。

    每次 InputStream() 构造都记录最近一个实例,测试可以通过 ``last_stream``
    属性拿到它来手动 fire callback。
    """
    from whisper_input import recorder as recorder_mod

    instances: list[FakeInputStream] = []

    def _factory(**kwargs):
        s = FakeInputStream(**kwargs)
        instances.append(s)
        return s

    fake = types.ModuleType("sounddevice")
    fake.InputStream = _factory

    monkeypatch.setattr(recorder_mod, "sd", fake)
    yield fake, instances


def test_start_streaming_invokes_on_chunk_with_float32_1d(fake_sd):
    """sd callback 的 int16 (frames, channels) 数据应被转换成 float32 1D。"""
    _fake, instances = fake_sd
    from whisper_input.recorder import AudioRecorder

    rec = AudioRecorder(sample_rate=16000, channels=1)

    received: list[np.ndarray] = []
    rec.start_streaming(on_chunk=received.append)
    assert rec.is_recording is True

    # 模拟 PortAudio 传来 int16 (frames=8, channels=1) 的 indata
    indata = np.array(
        [[-32768], [-16384], [0], [16384], [32767], [0], [0], [0]],
        dtype=np.int16,
    )
    instances[0].fire(indata)

    assert len(received) == 1
    chunk = received[0]
    assert chunk.dtype == np.float32
    assert chunk.ndim == 1
    assert chunk.shape == (8,)
    assert chunk[0] == pytest.approx(-1.0, abs=1e-4)
    assert chunk[4] == pytest.approx(1.0, abs=1e-3)


def test_on_level_called_in_streaming_mode(fake_sd):
    """音量浮窗回调在流式模式下也应工作(on_level 两种模式共用)。"""
    _fake, instances = fake_sd
    from whisper_input.recorder import AudioRecorder

    rec = AudioRecorder()
    levels: list[float] = []
    rec.on_level = levels.append
    rec.start_streaming(on_chunk=lambda _c: None)

    indata = np.array([[16384], [-16384], [8192]], dtype=np.int16)
    instances[0].fire(indata)

    assert len(levels) == 1
    assert levels[0] > 0  # RMS > 0 for non-silence


def test_stop_streaming_closes_stream(fake_sd):
    """stop_streaming 会调 underlying stream 的 stop + close。"""
    _fake, instances = fake_sd
    from whisper_input.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.start_streaming(on_chunk=lambda _c: None)
    assert instances[0].started is True

    rec.stop_streaming()
    assert rec.is_recording is False
    assert instances[0].stopped is True
    assert instances[0].closed is True


def test_start_streaming_noop_if_already_recording(fake_sd):
    """按住 + 流式模式已启动时,再调 start_streaming 应是 no-op。"""
    _fake, instances = fake_sd
    from whisper_input.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.start_streaming(on_chunk=lambda _c: None)
    rec.start_streaming(on_chunk=lambda _c: None)  # 第二次应无副作用
    assert len(instances) == 1


def test_stop_streaming_noop_if_not_recording(fake_sd):
    """没在录音时调 stop_streaming 应无副作用(不崩)。"""
    _fake, _ = fake_sd
    from whisper_input.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.stop_streaming()  # 不应该崩


def test_accumulate_mode_unchanged_by_new_fields(fake_sd):
    """累积模式 start/stop 的旧行为不因 28 轮变更受影响。"""
    _fake, instances = fake_sd
    from whisper_input.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.start()  # 累积模式
    indata = np.array([[100], [200]], dtype=np.int16)
    instances[0].fire(indata)

    wav_bytes = rec.stop()
    assert wav_bytes, "累积模式应产出非空 WAV"
    # WAV 必须有 RIFF/WAVE 头
    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"


def test_streaming_mode_does_not_keep_frames(fake_sd):
    """流式模式下 callback 不应往 _frames buffer 里塞数据(以免 stop() 生成 WAV)。"""
    _fake, instances = fake_sd
    from whisper_input.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.start_streaming(on_chunk=lambda _c: None)
    instances[0].fire(np.array([[100], [200]], dtype=np.int16))

    # _frames 应保持空(流式模式不累积)
    assert rec._frames == []
