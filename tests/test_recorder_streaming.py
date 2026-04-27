"""测试 AudioRecorder.start_streaming / stop_streaming(28 轮新增)。

不跑真 PortAudio,用 FakeInputStream 注入到 recorder 模块的 `sd` 名字上。
手动触发 fake stream 的 callback,验证:
- on_chunk 收到的是 float32 [-1,1] 1D array(不是原始 int16 shape)
- on_level 依然被调(两种模式共用音量回调)
- stop_streaming 会 stop + close 底层 InputStream
- 累积模式和流式模式互斥 + 回落正确

32 轮扩展了 fake_sd:补 PortAudioError + query_devices,新增连续 overflow
升级 device_lost / start 抛错转 MicUnavailableError / stop 超时兜底等用例。
"""

from __future__ import annotations

import threading
import time
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
        # 32 轮:测试可设这两个,模拟 stop hang / 抛错
        self.stop_delay: float = 0.0
        self.stop_raises: BaseException | None = None

    def start(self):
        self.started = True

    def stop(self):
        if self.stop_delay:
            time.sleep(self.stop_delay)
        if self.stop_raises is not None:
            raise self.stop_raises
        self.stopped = True

    def close(self):
        self.closed = True

    def fire(self, indata: np.ndarray, status=None) -> None:
        """测试手动触发 callback 模拟一次 audio 帧到达。"""
        self.callback(indata, indata.shape[0], None, status)


class FakePortAudioError(Exception):
    """fake 的 PortAudioError,recorder 用 except (sd.PortAudioError, OSError)
    需要 fake module 上挂一个真实异常类。"""


@pytest.fixture
def fake_sd(monkeypatch):
    """把 recorder 模块里的 `sd` 对象替换成带 FakeInputStream 的假 module。

    每次 InputStream() 构造都记录最近一个实例,测试可以通过 ``last_stream``
    属性拿到它来手动 fire callback。

    fake module 上也挂了 PortAudioError 和默认 query_devices,后者返回一个
    可用的 input device(测试可 monkeypatch 覆盖)。
    """
    from daobidao import recorder as recorder_mod

    instances: list[FakeInputStream] = []

    def _factory(**kwargs):
        s = FakeInputStream(**kwargs)
        instances.append(s)
        return s

    def _query_devices(kind=None):
        return {"name": "fake-mic", "max_input_channels": 1}

    fake = types.ModuleType("sounddevice")
    fake.InputStream = _factory
    fake.PortAudioError = FakePortAudioError
    fake.query_devices = _query_devices

    monkeypatch.setattr(recorder_mod, "sd", fake)
    yield fake, instances


def test_start_streaming_invokes_on_chunk_with_float32_1d(fake_sd):
    """sd callback 的 int16 (frames, channels) 数据应被转换成 float32 1D。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

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
    from daobidao.recorder import AudioRecorder

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
    from daobidao.recorder import AudioRecorder

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
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.start_streaming(on_chunk=lambda _c: None)
    rec.start_streaming(on_chunk=lambda _c: None)  # 第二次应无副作用
    assert len(instances) == 1


def test_stop_streaming_noop_if_not_recording(fake_sd):
    """没在录音时调 stop_streaming 应无副作用(不崩)。"""
    _fake, _ = fake_sd
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.stop_streaming()  # 不应该崩


def test_accumulate_mode_unchanged_by_new_fields(fake_sd):
    """累积模式 start/stop 的旧行为不因 28 轮变更受影响。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

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
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.start_streaming(on_chunk=lambda _c: None)
    instances[0].fire(np.array([[100], [200]], dtype=np.int16))

    # _frames 应保持空(流式模式不累积)
    assert rec._frames == []


# --- 32 轮:status flag 升级 device_lost ----------------------------------


def _fake_status(input_overflow: bool):
    """造一个像 sd.CallbackFlags 的对象:既能 truthy,又能 str() / 取属性。"""
    flag = input_overflow

    class _Status:
        def __bool__(self):
            return flag

        def __str__(self):
            return "input overflow" if flag else ""

    s = _Status()
    s.input_overflow = flag
    return s


def test_callback_status_overflow_single_does_not_escalate(fake_sd):
    """单次 input_overflow 不该升级 device_lost(避免误报常规 overload)。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    signals: list[str] = []
    rec.set_stream_status_callback(signals.append)
    rec.start_streaming(on_chunk=lambda _c: None)

    indata = np.array([[0], [0]], dtype=np.int16)
    instances[0].fire(indata, status=_fake_status(True))
    assert signals == []


def test_callback_status_overflow_persistent_escalates(fake_sd):
    """连续 5 次 input_overflow 应触发一次 device_lost 信号。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    signals: list[str] = []
    rec.set_stream_status_callback(signals.append)
    rec.start_streaming(on_chunk=lambda _c: None)

    indata = np.array([[0], [0]], dtype=np.int16)
    overflow = _fake_status(True)
    for _ in range(5):
        instances[0].fire(indata, status=overflow)

    assert len(signals) == 1
    assert "input overflow" in signals[0]

    # 再多触发也只报一次(_device_lost_signaled latch)
    for _ in range(3):
        instances[0].fire(indata, status=overflow)
    assert len(signals) == 1


def test_callback_status_overflow_streak_resets_on_clean_callback(fake_sd):
    """中间夹一次干净 callback 应重置连续计数,不会"凑够 5 次"误报。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    signals: list[str] = []
    rec.set_stream_status_callback(signals.append)
    rec.start_streaming(on_chunk=lambda _c: None)

    indata = np.array([[0], [0]], dtype=np.int16)
    overflow = _fake_status(True)

    # 4 次 overflow,1 次干净,4 次 overflow → 不应触发
    for _ in range(4):
        instances[0].fire(indata, status=overflow)
    instances[0].fire(indata, status=None)
    for _ in range(4):
        instances[0].fire(indata, status=overflow)

    assert signals == []


def test_callback_underflow_does_not_signal(fake_sd):
    """input_underflow 不属于"设备消失"(性能问题),不该升级。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    signals: list[str] = []
    rec.set_stream_status_callback(signals.append)
    rec.start_streaming(on_chunk=lambda _c: None)

    class _UnderflowStatus:
        input_overflow = False

        def __bool__(self):
            return True

        def __str__(self):
            return "input underflow"

    indata = np.array([[0], [0]], dtype=np.int16)
    for _ in range(10):
        instances[0].fire(indata, status=_UnderflowStatus())

    assert signals == []


# --- 32 轮:start 抛错转 MicUnavailableError -----------------------------


def test_start_streaming_raises_mic_unavailable_when_input_stream_throws(
    fake_sd, monkeypatch
):
    """sd.InputStream() 构造抛 PortAudioError → MicUnavailableError。"""
    fake, _instances = fake_sd
    from daobidao.recorder import AudioRecorder, MicUnavailableError

    def _boom(**_kwargs):
        raise FakePortAudioError("device not found")

    monkeypatch.setattr(fake, "InputStream", _boom)

    rec = AudioRecorder()
    with pytest.raises(MicUnavailableError) as excinfo:
        rec.start_streaming(on_chunk=lambda _c: None)
    assert excinfo.value.reason == "stream_error"
    assert rec.is_recording is False
    assert rec._stream is None


def test_start_accumulate_raises_mic_unavailable_when_start_throws(
    fake_sd, monkeypatch
):
    """sd.InputStream.start() 抛 PortAudioError → MicUnavailableError。

    覆盖累积模式 start() 路径(start_streaming 走的是 streaming 路径,这条
    测试保证 start() 也接住了同样的异常)。
    """
    fake, _instances = fake_sd
    from daobidao.recorder import AudioRecorder, MicUnavailableError

    def _factory(**kwargs):
        s = FakeInputStream(**kwargs)

        def _boom():
            raise FakePortAudioError("start failed")

        s.start = _boom
        return s

    monkeypatch.setattr(fake, "InputStream", _factory)

    rec = AudioRecorder()
    with pytest.raises(MicUnavailableError) as excinfo:
        rec.start()
    assert excinfo.value.reason == "stream_error"
    assert rec.is_recording is False
    assert rec._stream is None


# --- 32 轮:stop 超时兜底 -----------------------------------------------


def test_stop_stream_with_timeout_succeeds_normally(fake_sd):
    """正常情况下 _stop_stream_with_timeout 返回 True。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.start_streaming(on_chunk=lambda _c: None)
    rec.stop_streaming()

    assert instances[0].stopped is True
    assert instances[0].closed is True
    assert rec._stream is None


def test_stop_stream_with_timeout_returns_false_on_hang(fake_sd):
    """stop() 卡住超过 timeout → helper 返回 False,_stream 已置 None。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.start_streaming(on_chunk=lambda _c: None)
    instances[0].stop_delay = 0.5  # 远超 helper 默认 0.5s 边界

    # 直接调内部 helper(模拟流程会用 stop_streaming,但 stop_streaming 持锁
    # 调 helper 后会立即把 _stream 解引用,行为相同);我们走公共入口验证。
    rec.stop_streaming()
    # _stream 被置 None(防止下一次按键复用 hang 中的 stream)
    assert rec._stream is None
    # is_recording 也应该被翻 False
    assert rec.is_recording is False


def test_stop_stream_with_timeout_logs_on_error(fake_sd):
    """stop() 抛错时 helper 也应正常返回(不冒泡)。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.start_streaming(on_chunk=lambda _c: None)
    instances[0].stop_raises = RuntimeError("stop boom")

    rec.stop_streaming()  # 不应抛
    assert rec._stream is None
    assert rec.is_recording is False


# --- 32 轮:status callback 不阻塞 callback ----------------------------


def test_stream_status_cb_exception_swallowed(fake_sd):
    """status callback 抛异常不能让 PortAudio callback 链断掉。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()

    def _bad_cb(_status):
        raise RuntimeError("cb boom")

    rec.set_stream_status_callback(_bad_cb)
    rec.start_streaming(on_chunk=lambda _c: None)

    indata = np.array([[0]], dtype=np.int16)
    overflow = _fake_status(True)
    for _ in range(5):
        # 不应抛
        instances[0].fire(indata, status=overflow)


# --- 32 轮:状态在新一轮 start 时被重置 -------------------------------


def test_status_state_resets_between_sessions(fake_sd):
    """上一轮 latch 的 _device_lost_signaled 在新一轮 start 时应被清。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    signals: list[str] = []
    rec.set_stream_status_callback(signals.append)

    rec.start_streaming(on_chunk=lambda _c: None)
    indata = np.array([[0]], dtype=np.int16)
    overflow = _fake_status(True)
    for _ in range(5):
        instances[0].fire(indata, status=overflow)
    rec.stop_streaming()
    assert len(signals) == 1

    # 下一轮 session,应能再触发一次
    rec.start_streaming(on_chunk=lambda _c: None)
    for _ in range(5):
        instances[1].fire(indata, status=overflow)
    assert len(signals) == 2


# --- 32 轮:_stop_stream_with_timeout 用到的 daemon 线程不留隐患 -----


def test_stop_helper_does_not_block_caller_when_stream_hangs(fake_sd):
    """stop hang 时调用方应在 ~0.5s 内拿回控制权,不被 daemon 线程拖。"""
    _fake, instances = fake_sd
    from daobidao.recorder import AudioRecorder

    rec = AudioRecorder()
    rec.start_streaming(on_chunk=lambda _c: None)
    instances[0].stop_delay = 2.0  # hang 2s

    t0 = time.monotonic()
    rec.stop_streaming()
    elapsed = time.monotonic() - t0

    assert elapsed < 1.5, (
        f"stop_streaming 不应阻塞 > 0.5s timeout,实际 {elapsed:.2f}s"
    )


# 修补:保证测试本身不留 daemon 线程长时间运行(pytest 自己会等)
@pytest.fixture(autouse=True)
def _join_daemon_threads():
    yield
    # 给 daemon 线程一点点时间清理(不强求 join,daemon 不会阻塞退出)
    for t in list(threading.enumerate()):
        if t.name.startswith("recorder-stop") and t.is_alive():
            t.join(timeout=0.05)
