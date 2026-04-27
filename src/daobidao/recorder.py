"""Audio recorder using sounddevice."""

import io
import subprocess
import sys
import threading
import wave
from collections.abc import Callable

import numpy as np
import sounddevice as sd

from daobidao.logger import get_logger

logger = get_logger(__name__)

# 32 轮:连续 N 次 callback 都带 input_overflow 才升级为 device_lost。
# callback 周期 ~30ms,5 次 ≈ 150ms,既能过滤 1-2 次的临时 overload,
# 又能在设备真消失时及时报警。实测发现不灵敏 / 太敏感时再调。
#
# 注:在 Linux + PipeWire 上,物理麦拔了之后 PipeWire 仍发"虚拟静音流",
# callback 干净不带 status flag,这条路径**抓不到**。Linux 主要靠 probe()
# 里的 pactl 端口状态检测兜底。该阈值仍保留,服务于 macOS / 纯 ALSA / 真
# overload 场景。
_OVERFLOW_DEVICE_LOST_THRESHOLD = 5

# 32 轮:pactl 调用超时(s)。本机实测 ~50ms,留 10 倍裕量。
_PACTL_TIMEOUT_S = 0.5


class PactlUnavailableError(RuntimeError):
    """``pactl`` 命令本身不可用(没装 / 调用失败 / 输出无法解析)。

    与"pactl 看到没麦"严格区分:前者要求用户装 pulseaudio-utils,后者是
    用户操作问题(物理麦没插好)。
    """


def _check_pactl_input_available() -> bool:
    """通过 ``pactl list sources`` 检查物理 input 是否可用(Linux 专用)。

    PipeWire / PulseAudio 在 ``Ports:`` 字段把 ALSA HDA codec 的 jack-detect
    状态暴露成 ``available`` / ``not available`` / ``availability unknown``。
    Chrome 的 ``getUserMedia`` 也是看这一信号判定"未检测到麦克风"。

    sounddevice 走 PortAudio,在 PipeWire 上看到的永远是 default 虚拟设备
    (无论物理麦在不在),``sd.query_devices`` 区分不出"真没麦",所以
    Linux 上本函数是 probe 的**唯一权威**(docs/32-录音麦克风离线检测/)。

    Returns:
        True   - 至少一个 alsa_input.* source 的某 port 是 available 或
                 availability unknown(后者:无 jack-detect 电路的内置 mic)
        False  - 找到 alsa_input.* source 但其全部 port 都 "not available";
                 或一个 alsa_input.* 都没有(系统真没物理输入硬件)

    Raises:
        PactlUnavailableError - pactl 命令不存在 / 调用失败 / 输出非预期结构。
                          调用方应当作"probe_failed"上报,提示用户安装
                          ``pulseaudio-utils`` 包(setup.sh / install.sh
                          的 APT_PKGS 已默认包含)。
    """
    try:
        proc = subprocess.run(
            ["pactl", "list", "sources"],
            capture_output=True,
            text=True,
            timeout=_PACTL_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise PactlUnavailableError("pactl command not found") from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise PactlUnavailableError(f"pactl call failed: {exc!r}") from exc
    if proc.returncode != 0:
        raise PactlUnavailableError(
            f"pactl returncode={proc.returncode}, "
            f"stderr={proc.stderr.strip()!r}"
        )

    found_input = False
    found_available = False
    current_is_input = False
    in_ports_block = False

    for raw in proc.stdout.splitlines():
        if not raw.startswith("\t"):
            # 顶层 "Source #N" 行 / 空行 → 新一段 source 开始
            current_is_input = False
            in_ports_block = False
            continue
        stripped = raw.strip()
        if stripped.startswith("Name: "):
            name = stripped[len("Name: ") :]
            # 只关心物理 alsa_input.*;.monitor 是 sink 回环不算
            current_is_input = name.startswith(
                "alsa_input."
            ) and not name.endswith(".monitor")
            in_ports_block = False
        elif stripped == "Ports:":
            in_ports_block = current_is_input
        elif (
            in_ports_block
            and raw.startswith("\t\t")
            and "(" in raw
            and ")" in raw
        ):
            # port 行:"\t\t<name>: <desc> (type: ..., ..., <availability>)"
            found_input = True
            lp = raw.rfind("(")
            rp = raw.rfind(")")
            if lp < 0 or rp <= lp:
                continue
            attrs = raw[lp + 1 : rp]
            availability = attrs.split(",")[-1].strip().lower()
            if availability in ("available", "availability unknown"):
                found_available = True
        elif raw.startswith("\t") and not raw.startswith("\t\t"):
            # 同 source 内的别的字段(Active Port / Formats / Properties)
            in_ports_block = False

    return found_input and found_available


class MicUnavailableError(RuntimeError):
    """录音前 probe / 启动 stream / 中途监控发现麦克风不可用。

    reason 取值(开发者枚举,不进 i18n):
        - "probe_failed":  query_devices 报错 / 默认 input 不存在 / 超时
        - "stream_error":  sd.InputStream 构造或 start() 抛 PortAudioError / OSError
        - "device_lost":   录音中 callback status 含设备消失类 flag
                           (这条主要由 WhisperInput 在归因日志时使用,recorder
                           本身只通过 status callback 把 raw flag 字符串送出去)
    """

    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


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
        # 32 轮:callback 里检测到"设备消失"时调一次的回调,运行在 PortAudio
        # 线程,必须 lightweight(只 enqueue)。
        self._stream_status_cb: Callable[[str], None] | None = None
        self._consecutive_overflow_count = 0
        self._device_lost_signaled = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    def set_stream_status_callback(
        self, cb: Callable[[str], None] | None
    ) -> None:
        """设置"录音中检测到设备消失"信号的回调。

        cb(status_str) 会在 PortAudio 线程同步调用一次(每个 session 至多一次),
        实现必须 lightweight —— 只做 enqueue,不要在里头 stop stream。
        """
        self._stream_status_cb = cb

    def probe(self, timeout: float = 0.2) -> None:
        """录音前快速校验默认输入设备可用。

        Linux: ``pactl list sources`` 是**唯一权威**。`sd.query_devices`
        在 PipeWire 上完全不可靠 —— 物理麦拔了之后 PipeWire 仍把 default
        当成可用设备暴露,callback 也照常发静音流;只有 PipeWire 自己的
        Ports jack-detect 字段能反映真相(Chrome 也是这么判定的)。
        pactl 不存在 / 调用失败 / 全部 port "not available" → 都视为
        ``MicUnavailableError(probe_failed)``。``pulseaudio-utils`` 是
        Linux 安装路径的系统依赖(setup.sh / install.sh 的 APT_PKGS)。

        其他平台: 走 ``query_devices`` 路径(macOS CoreAudio 反映真实状态),
        ``timeout`` 兜底冷启动阻塞。

        失败 → ``MicUnavailableError(reason="probe_failed")``。
        """
        if sys.platform.startswith("linux"):
            try:
                available = _check_pactl_input_available()
            except PactlUnavailableError as exc:
                raise MicUnavailableError(
                    "probe_failed",
                    f"pactl unavailable (need pulseaudio-utils): {exc}",
                ) from exc
            if not available:
                raise MicUnavailableError(
                    "probe_failed",
                    "no available input port (jack-detect: not available)",
                )
            return

        # macOS / 其他: query_devices 兜底
        result_box: list = []
        err_box: list[BaseException] = []
        done = threading.Event()

        def _run() -> None:
            try:
                devices = sd.query_devices(kind="input")
                result_box.append(devices)
            except BaseException as exc:
                err_box.append(exc)
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True, name="mic-probe").start()
        if not done.wait(timeout):
            raise MicUnavailableError(
                "probe_failed", f"timeout after {timeout}s"
            )
        if err_box:
            raise MicUnavailableError("probe_failed", repr(err_box[0]))
        info = result_box[0]
        if not info:
            raise MicUnavailableError("probe_failed", "no input device")
        if isinstance(info, list):
            info = info[0]
        max_in = (
            info.get("max_input_channels", 0) if isinstance(info, dict) else 0
        )
        if max_in <= 0:
            raise MicUnavailableError(
                "probe_failed", f"max_input_channels={max_in}"
            )

    def start(self) -> None:
        """开始累积模式录音(离线路径用)。"""
        with self._lock:
            if self._recording:
                return
            self._frames.clear()
            self._on_chunk_cb = None
            self._reset_status_state()
            self._recording = True
            try:
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="int16",
                    callback=self._audio_callback,
                )
                self._stream.start()
            except (sd.PortAudioError, OSError) as exc:
                self._recording = False
                self._stream = None
                raise MicUnavailableError("stream_error", repr(exc)) from exc

    def stop(self) -> bytes:
        """停止录音,返回 WAV 格式的字节数据(累积模式)。"""
        with self._lock:
            if not self._recording:
                return b""
            self._recording = False
            self._stop_stream_with_timeout()
            return self._to_wav()

    def start_streaming(self, on_chunk: Callable[[np.ndarray], None]) -> None:
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
            self._reset_status_state()
            self._recording = True
            try:
                self._stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="int16",
                    callback=self._audio_callback,
                )
                self._stream.start()
            except (sd.PortAudioError, OSError) as exc:
                self._recording = False
                self._stream = None
                self._on_chunk_cb = None
                raise MicUnavailableError("stream_error", repr(exc)) from exc

    def stop_streaming(self) -> None:
        """停止流式录音。数据已通过 ``on_chunk`` 交出去,本方法不返回。"""
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            self._on_chunk_cb = None
            self._stop_stream_with_timeout()

    def _audio_callback(
        self, indata: np.ndarray, frames: int, time_info, status
    ) -> None:
        if status:
            from daobidao.i18n import t

            logger.warning(
                "stream_status",
                status=str(status),
                message=t("recorder.status", status=status),
            )
            # 32 轮:连续 input_overflow 视为"设备消失",升级提示。单次 / 偶发
            # 不报,避免误判正常 overload。
            self._maybe_signal_device_lost(status)
        else:
            # 一旦有一次干净的 callback,就重置连续计数
            self._consecutive_overflow_count = 0

        # RMS 给音量浮窗(两种模式共用)
        if self.on_level:
            rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
            self.on_level(rms)

        if self._on_chunk_cb is not None:
            # 流式:int16 → float32 [-1,1],展平成 1D mono
            chunk = indata.astype(np.float32).reshape(-1) / 32768.0
            self._on_chunk_cb(chunk)
        else:
            # 累积:老路径,保持不变
            self._frames.append(indata.copy())

    def _maybe_signal_device_lost(self, status) -> None:
        """callback 内部判断是否触发 device_lost 信号。在 PortAudio 线程跑。"""
        if self._device_lost_signaled:
            return
        # sounddevice CallbackFlags 的 input_overflow 属性是 bool;字符串化也
        # 包含 "input overflow"。两种渠道都接,鲁棒一点。
        is_overflow = bool(getattr(status, "input_overflow", False)) or (
            "input overflow" in str(status)
        )
        if is_overflow:
            self._consecutive_overflow_count += 1
        else:
            self._consecutive_overflow_count = 0
        if self._consecutive_overflow_count >= _OVERFLOW_DEVICE_LOST_THRESHOLD:
            self._device_lost_signaled = True
            cb = self._stream_status_cb
            if cb is not None:
                try:
                    cb(str(status))
                except Exception:
                    logger.exception("stream_status_cb_failed")

    def _reset_status_state(self) -> None:
        self._consecutive_overflow_count = 0
        self._device_lost_signaled = False

    def _stop_stream_with_timeout(self, timeout: float = 0.5) -> bool:
        """停 + close 当前 stream,带超时兜底防 PortAudio hang(24 轮教训)。

        正常情况下 0.5s 充足。超时分支只 log,把 ``self._stream`` 解引用让
        PortAudio 自己慢慢回收,避免主线程被 hang。

        约定:调用方持有 ``self._lock``,本方法不再加锁。
        """
        if self._stream is None:
            return True
        stream = self._stream
        self._stream = None
        done = threading.Event()
        err_box: list[BaseException] = []

        def _run() -> None:
            try:
                stream.stop()
                stream.close()
            except BaseException as exc:
                err_box.append(exc)
            finally:
                done.set()

        threading.Thread(target=_run, daemon=True, name="recorder-stop").start()
        if not done.wait(timeout):
            logger.warning("stream_stop_timeout", timeout=timeout)
            return False
        if err_box:
            logger.warning("stream_stop_error", error=repr(err_box[0]))
            return False
        return True

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
