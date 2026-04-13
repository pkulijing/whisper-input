"""Audio recorder using sounddevice."""

import io
import threading
import wave

import numpy as np
import sounddevice as sd


class AudioRecorder:
    """按住录音，松开停止。录音数据保存为 16kHz 单声道 WAV。"""

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._recording = False
        self.on_level = None  # 实时音量回调: (rms: float) -> None

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        """开始录音。"""
        with self._lock:
            if self._recording:
                return
            self._frames.clear()
            self._recording = True
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                callback=self._audio_callback,
            )
            self._stream.start()

    def stop(self) -> bytes:
        """停止录音，返回 WAV 格式的字节数据。"""
        with self._lock:
            if not self._recording:
                return b""
            self._recording = False
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            return self._to_wav()

    def _audio_callback(
        self, indata: np.ndarray, frames: int, time_info, status
    ) -> None:
        if status:
            print(f"[recorder] {status}")
        self._frames.append(indata.copy())
        if self.on_level:
            rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
            self.on_level(rms)

    def _to_wav(self) -> bytes:
        if not self._frames:
            return b""
        audio = np.concatenate(self._frames, axis=0)
        duration = len(audio) / self.sample_rate
        rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
        print(
            f"[recorder] 录音 {duration:.1f}s, "
            f"{len(audio)} 采样, RMS={rms:.0f}"
        )
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio.tobytes())
        return buf.getvalue()
