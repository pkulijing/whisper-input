"""Audio recorder using sounddevice."""

import io
import threading
import wave
from collections.abc import Callable

import numpy as np
import sounddevice as sd

from whisper_input.logger import get_logger

logger = get_logger(__name__)


class AudioRecorder:
    """按住录音，松开停止。

    两种模式,互斥:
    - **累积模式** (``start`` / ``stop``):所有音频存内部 buffer,``stop()``
      返回完整 WAV bytes。28 轮前的离线路径用这个。
    - **流式模式** (``start_streaming`` / ``stop_streaming``):每次 sd callback
      把 indata 转成 float32 1D array 回调给 on_chunk,不留 buffer。28 轮新增,
      供 ``WhisperInput._do_stream_step`` 按 2s 累积驱动 STT。

    两种模式下 ``on_level``(RMS 音量浮窗回调)都照常工作。
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._recording = False
        self._on_chunk_cb: Callable[[np.ndarray], None] | None = None
        self.on_level = None  # 实时音量回调: (rms: float) -> None

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        """开始累积模式录音(离线路径用)。"""
        with self._lock:
            if self._recording:
                return
            self._frames.clear()
            self._on_chunk_cb = None
            self._recording = True
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                callback=self._audio_callback,
            )
            self._stream.start()

    def stop(self) -> bytes:
        """停止录音,返回 WAV 格式的字节数据(累积模式)。"""
        with self._lock:
            if not self._recording:
                return b""
            self._recording = False
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            return self._to_wav()

    def start_streaming(
        self, on_chunk: Callable[[np.ndarray], None]
    ) -> None:
        """开始流式模式录音。

        每次 sd callback 到达,把 ``indata`` 转成 float32 [-1,1] 1D array
        (mono)回调给 ``on_chunk``,不留 buffer。callback 里会同步先算 RMS
        调 ``on_level``(浮窗音量),再调 on_chunk。

        **重要**:``on_chunk`` 运行在 PortAudio 线程,必须 lightweight——
        只做 append 到队列/buffer,真正耗时工作 enqueue 到别处。
        """
        with self._lock:
            if self._recording:
                return
            self._frames.clear()  # 流式模式不用,但也清一下防意外
            self._on_chunk_cb = on_chunk
            self._recording = True
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                callback=self._audio_callback,
            )
            self._stream.start()

    def stop_streaming(self) -> None:
        """停止流式录音。数据已通过 ``on_chunk`` 交出去,本方法不返回。"""
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            self._on_chunk_cb = None
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None

    def _audio_callback(
        self, indata: np.ndarray, frames: int, time_info, status
    ) -> None:
        if status:
            from whisper_input.i18n import t

            logger.warning(
                "stream_status",
                status=str(status),
                message=t("recorder.status", status=status),
            )
        # RMS 给音量浮窗(两种模式共用)
        if self.on_level:
            rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
            self.on_level(rms)

        if self._on_chunk_cb is not None:
            # 流式:int16 → float32 [-1,1],展平成 1D mono
            chunk = (
                indata.astype(np.float32).reshape(-1) / 32768.0
            )
            self._on_chunk_cb(chunk)
        else:
            # 累积:老路径,保持不变
            self._frames.append(indata.copy())

    def _to_wav(self) -> bytes:
        if not self._frames:
            return b""
        audio = np.concatenate(self._frames, axis=0)
        duration = len(audio) / self.sample_rate
        rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
        logger.debug(
            "recording_stats",
            duration_s=round(duration, 2),
            samples=len(audio),
            rms=round(float(rms), 1),
        )
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio.tobytes())
        return buf.getvalue()
