#!/usr/bin/env python3
"""叨逼叨 (daobidao) - 语音输入工具

按住快捷键说话，松开后自动将语音识别结果输入到当前焦点窗口。
支持中英文混合输入，使用本地 Qwen3-ASR 模型(0.6B / 1.7B 可切换)。

用法:
    uv run daobidao                 # 使用默认配置
    uv run daobidao -k KEY_RIGHTALT  # 使用右Alt键
"""
# ruff: noqa: E402
# 本文件需要在 import 之间执行环境变量配置（GI_TYPELIB_PATH、
# PYTHONWARNINGS），因此整体豁免 E402。

import argparse
import contextlib
import os
import signal
import sys

# 抑制 tqdm/FunASR 的全局 lock 在退出时触发的
# multiprocessing.resource_tracker 泄漏警告。该警告由 resource_tracker
# 子进程发出，必须通过 PYTHONWARNINGS 环境变量让子进程继承过滤规则；
# 信号量由内核回收，不影响功能，仅为消除控制台噪音。
_RT_FILTER = "ignore:resource_tracker:UserWarning"
if _RT_FILTER not in os.environ.get("PYTHONWARNINGS", ""):
    _existing = os.environ.get("PYTHONWARNINGS", "")
    os.environ["PYTHONWARNINGS"] = (
        f"{_existing},{_RT_FILTER}" if _existing else _RT_FILTER
    )

# Linux: PyGObject 需要指定系统 typelib 路径
if sys.platform == "linux":
    if "GI_TYPELIB_PATH" not in os.environ:
        _typelib_dir = "/usr/lib/girepository-1.0"
        if os.path.isdir(_typelib_dir):
            os.environ["GI_TYPELIB_PATH"] = _typelib_dir

import atexit
import queue
import subprocess
import threading

import numpy as np

from daobidao.config_manager import ConfigManager
from daobidao.hotkey import HotkeyListener
from daobidao.i18n import load_locales, set_language, t
from daobidao.input_method import type_text
from daobidao.logger import configure_logging, get_logger
from daobidao.recorder import AudioRecorder
from daobidao.stt.base import (
    STREAMING_CHUNK_SAMPLES,
    StreamingKVOverflowError,
)
from daobidao.version import __commit__, __version__

logger = get_logger(__name__)

# 流式接近上限警告阈值(28s,留 ~5s 余量给用户松手)。策略 E 的硬上限跟
# audio_features 长度 + committed token 长度有关,大概在 35-38s 之间。
_STREAM_WARN_SAMPLES = 28 * 16000


def create_stt_engine(config: dict):
    """根据配置创建 STT 引擎。"""
    engine = config.get("engine", "qwen3")
    engine_config = config.get(engine, {})

    from daobidao.stt import create_stt

    return create_stt(engine, engine_config)


def play_sound(path: str) -> None:
    """播放提示音。"""
    if path and os.path.exists(path):
        cmd = (
            ["afplay", path]
            if sys.platform == "darwin"
            else ["paplay", path]
        )
        with contextlib.suppress(FileNotFoundError):
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def terminate_portaudio(timeout: float = 2.0) -> bool:
    """主动关闭 PortAudio，抢在 Python finalize 之前跑完。

    sounddevice 模块 import 时会 `atexit.register(_exit_handler)`，它在
    `Py_FinalizeEx` 阶段的主线程上调 `Pa_Terminate` → `AudioUnitStop`，
    在 macOS 上会跟 CoreAudio HAL IO 线程的 mutex 抢占，概率性死锁
    (docs/24-退出路径CoreAudio死锁修复/)。

    修法是在 shutdown 的可控上下文里：
    1. 先 unregister 默认 atexit，避免 finalize 阶段再跑一次撞锁
    2. 起 daemon 线程调 `_terminate()`，主线程 join(timeout) 兜底
    3. 超时返回 False，调用方应该改走 os._exit(0) 强退

    返回 True 表示 PortAudio 已干净关闭或本来就不需要（sounddevice
    没装），返回 False 表示需要强退。
    """
    try:
        import sounddevice as sd
    except ImportError:
        return True  # 没装 sounddevice, 本来就没 atexit 要清

    exit_handler = getattr(sd, "_exit_handler", None)
    if exit_handler is not None:
        with contextlib.suppress(Exception):
            atexit.unregister(exit_handler)

    terminate_fn = getattr(sd, "_terminate", None)
    if terminate_fn is None:
        logger.warning(
            "portaudio_terminate_missing",
            message="sounddevice._terminate 不存在,跳过主动清理",
        )
        return True  # atexit 已 unregister, 即使不 terminate 也不会死锁

    done = threading.Event()
    error: list[BaseException] = []

    def _run() -> None:
        try:
            terminate_fn()
        except BaseException as exc:
            error.append(exc)
        finally:
            done.set()

    thread = threading.Thread(
        target=_run, name="portaudio-terminate", daemon=True
    )
    thread.start()
    if not done.wait(timeout=timeout):
        logger.warning(
            "portaudio_terminate_timeout",
            timeout=timeout,
            message="PortAudio 终止超时,主线程将强退",
        )
        return False
    if error:
        logger.error(
            "portaudio_terminate_error",
            error=repr(error[0]),
            message="PortAudio 终止异常",
        )
        return False
    return True


class WhisperInput:
    """语音输入主控制器。"""

    def __init__(self, config: dict):
        self.config = config
        self.recorder = AudioRecorder(
            sample_rate=config.get("audio", {}).get("sample_rate", 16000),
            channels=config.get("audio", {}).get("channels", 1),
        )
        self.stt = create_stt_engine(config)
        self.sound_enabled = config.get("sound", {}).get("enabled", True)
        from daobidao.config_manager import _SOUND_SUFFIX

        sound = config.get("sound", {})
        self.sound_start = sound.get(f"start{_SOUND_SUFFIX}", "")
        self.sound_stop = sound.get(f"stop{_SOUND_SUFFIX}", "")
        self._processing = False
        self._status_callback = None
        self.tray_status_enabled = config.get(
            "tray_status", {}
        ).get("enabled", True)
        self.overlay_enabled = config.get(
            "overlay", {}
        ).get("enabled", True)
        self._overlay = None

        # 单线程 worker：所有由热键触发的实际动作（录音 start/stop、提示音、
        # 浮窗状态切换）都在这里串行执行。
        # 为什么要隔离：macOS 下 pynput 的回调跑在 CGEventTap 线程里，在那里
        # 同步调 portaudio 的 AudioUnitStop 会跟 CoreAudio 自己的 HAL IO 线程
        # 抢 HAL mutex，概率性死锁（docs/22-热键回调死锁修复/）。
        self._event_queue: queue.Queue = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._worker_sentinel = object()

        # STT 热切换状态(设置页面改 variant 时在后台线程换模型)
        self._stt_switch_lock = threading.Lock()
        self._stt_switch_state: dict = {
            "switching": False,
            "target_variant": None,
            "error": None,
        }

        # --- 流式识别(28 轮) ---
        # streaming_mode 是否开启;关的话走离线 transcribe()
        qwen3_cfg = config.get("qwen3", {})
        self.streaming_mode = qwen3_cfg.get("streaming_mode", True)
        # 一次按键 → 说话 → 松手周期的引擎私有状态
        self._stream_state = None
        # PortAudio 线程回调的 float32 chunk 累积器;凑够 ~2s 再触发 stream_step
        self._stream_audio_buffer: list[np.ndarray] = []
        self._stream_audio_samples = 0
        self._stream_buffer_lock = threading.Lock()
        # 累计样本数,用来 28s 时弹"接近上限"提示(Plan agent 建议)
        self._stream_total_samples = 0
        self._stream_near_limit_warned = False
        # 一次 session 内发生过 KV overflow 的标记,后续 chunks 跳过推理
        self._stream_overflow_hit = False

    def set_status_callback(self, callback) -> None:
        """设置状态变化回调 (status: str) -> None。"""
        self._status_callback = callback

    def set_overlay(self, overlay) -> None:
        """设置录音浮窗实例。"""
        self._overlay = overlay

    def _notify_status(self, status: str) -> None:
        """通知状态变化。"""
        if self.tray_status_enabled and self._status_callback:
            self._status_callback(status)
        if self._overlay:
            if status == "recording" and self.overlay_enabled:
                self._overlay.show()
            elif status == "processing" and self.overlay_enabled:
                self._overlay.update(t("main.processing"))
            elif status == "ready":
                self._overlay.hide()

    def on_key_press(self) -> None:
        """热键按下回调。由 HotkeyListener 直接在系统回调线程里调用，
        必须立刻返回 —— 实际动作扔给 worker 串行执行。"""
        self._event_queue.put(self._do_key_press)

    def on_key_release(self) -> None:
        """热键释放回调，同 on_key_press。"""
        self._event_queue.put(self._do_key_release)

    def _should_stream(self) -> bool:
        """本次按键是否走流式路径。

        条件:用户开了流式 toggle 并且当前 STT 引擎声明支持流式。任一不满足
        就回落到离线 transcribe()。

        用 ``is True`` 严格检查 supports_streaming,而不是 truthy 判断 —— 这样
        MagicMock 的自动属性(返回 MagicMock 对象,truthy)不会被误判成"支持",
        现有 MagicMock-based worker 测试就能正常 fallback 到离线路径。
        """
        return (
            self.streaming_mode
            and getattr(self.stt, "supports_streaming", False) is True
        )

    def _do_key_press(self) -> None:
        """热键按下的真正处理 - 开始录音。"""
        if self._processing:
            return
        logger.info("recording_start", message=t("main.recording_start"))
        self._notify_status("recording")
        # 连接音量回调到浮窗
        if self._overlay and self.overlay_enabled:
            self.recorder.on_level = self._overlay.set_level
        if self.sound_enabled:
            play_sound(self.sound_start)

        if self._should_stream():
            # 流式路径:init state,开启流式录音,由 on_chunk 驱动
            try:
                self._stream_state = self.stt.init_stream_state()
            except Exception:
                logger.exception("stream_init_failed")
                self._stream_state = None
                # 回落:按离线路径录
                self.recorder.start()
                return
            self._reset_stream_accumulator()
            self._stream_total_samples = 0
            self._stream_near_limit_warned = False
            self._stream_overflow_hit = False
            self.recorder.start_streaming(on_chunk=self._on_stream_chunk)
        else:
            self.recorder.start()

    def _do_key_release(self) -> None:
        """热键释放的真正处理 - 停止录音并识别。"""
        if not self.recorder.is_recording:
            return
        logger.info("recording_stop", message=t("main.recording_stop"))
        self.recorder.on_level = None
        self._notify_status("processing")
        if self.sound_enabled:
            play_sound(self.sound_stop)

        if self._stream_state is not None:
            # 流式路径:停流 + enqueue 最终 flush(is_last=True)
            self.recorder.stop_streaming()
            remaining = self._take_stream_accumulator()
            self._processing = True
            self._event_queue.put(
                lambda chunk=remaining: self._do_stream_step(
                    chunk, is_last=True
                )
            )
            return

        # 离线路径(旧行为)
        wav_data = self.recorder.stop()
        if not wav_data:
            logger.warning("no_audio", message=t("main.no_audio"))
            return
        self._processing = True
        threading.Thread(
            target=self._process, args=(wav_data,), daemon=True
        ).start()

    # ------------------------------------------------------------------
    # 流式 chunk 通路
    # ------------------------------------------------------------------

    def _reset_stream_accumulator(self) -> None:
        with self._stream_buffer_lock:
            self._stream_audio_buffer.clear()
            self._stream_audio_samples = 0

    def _take_stream_accumulator(self) -> np.ndarray:
        """把 accumulator 里的全部音频取出来并清空 buffer。"""
        with self._stream_buffer_lock:
            if not self._stream_audio_buffer:
                return np.zeros(0, dtype=np.float32)
            chunk = np.concatenate(self._stream_audio_buffer)
            self._stream_audio_buffer.clear()
            self._stream_audio_samples = 0
        return chunk

    def _on_stream_chunk(self, audio_chunk: np.ndarray) -> None:
        """PortAudio 线程回调。必须 lightweight:累积到 ~2s 就 enqueue 到 worker。

        累计样本数用于 28s"接近上限"提示 —— 只做布尔翻转,通知落在 worker 里。
        """
        swap_out: np.ndarray | None = None
        trigger_near_limit_warning = False
        with self._stream_buffer_lock:
            if not self._recording_streaming():
                return
            self._stream_audio_buffer.append(audio_chunk)
            self._stream_audio_samples += audio_chunk.size
            self._stream_total_samples += audio_chunk.size

            if self._stream_audio_samples >= STREAMING_CHUNK_SAMPLES:
                swap_out = np.concatenate(self._stream_audio_buffer)
                self._stream_audio_buffer.clear()
                self._stream_audio_samples = 0

            # 28s 接近 KV cache 上限(~33-38s),提示用户松手
            if (
                not self._stream_near_limit_warned
                and self._stream_total_samples
                >= _STREAM_WARN_SAMPLES
            ):
                self._stream_near_limit_warned = True
                trigger_near_limit_warning = True

        if swap_out is not None:
            self._event_queue.put(
                lambda chunk=swap_out: self._do_stream_step(
                    chunk, is_last=False
                )
            )
        if trigger_near_limit_warning:
            self._event_queue.put(self._notify_near_limit)

    def _recording_streaming(self) -> bool:
        """辅助判断:是否在流式录音中(跟 recorder.is_recording 相关,但在
        流式专用路径里还要求 _stream_state 非 None)。"""
        return (
            self._stream_state is not None
            and self.recorder.is_recording
        )

    def _notify_near_limit(self) -> None:
        """浮窗显示"接近识别上限",用户可以决定松手。"""
        if self._overlay and self.overlay_enabled:
            self._overlay.update(t("main.streaming_near_limit"))
        logger.info(
            "streaming_near_limit",
            total_samples=self._stream_total_samples,
        )

    def _do_stream_step(
        self, audio_chunk: np.ndarray, is_last: bool
    ) -> None:
        """Worker 线程里跑一次 stream_step,把 committed_delta 粘贴到焦点。"""
        # 先前已 overflow,剩余 chunk 直接丢
        if self._stream_overflow_hit:
            if is_last:
                self._finalize_stream_session()
            return

        if self._stream_state is None:
            # 正常结束或 race:final flush 已处理过
            if is_last:
                self._finalize_stream_session()
            return

        try:
            evt = self.stt.stream_step(
                audio_chunk, self._stream_state, is_last=is_last
            )
        except StreamingKVOverflowError:
            logger.warning(
                "stream_kv_overflow",
                total_samples=self._stream_total_samples,
            )
            self._stream_overflow_hit = True
            if self._overlay and self.overlay_enabled:
                self._overlay.update(t("main.streaming_overflow"))
            if is_last:
                self._finalize_stream_session()
            return
        except Exception:
            logger.exception("stream_step_failed")
            if is_last:
                self._finalize_stream_session()
            return

        if evt.committed_delta:
            logger.info(
                "streaming_commit",
                delta=evt.committed_delta,
                is_final=evt.is_final,
            )
            try:
                type_text(evt.committed_delta)
            except Exception:
                logger.exception("streaming_paste_failed")

        if evt.is_final:
            self._finalize_stream_session()

    def _finalize_stream_session(self) -> None:
        """流式 session 结束:清状态 + 恢复浮窗 ready。"""
        self._stream_state = None
        self._reset_stream_accumulator()
        self._stream_total_samples = 0
        self._stream_near_limit_warned = False
        self._stream_overflow_hit = False
        self._processing = False
        self._notify_status("ready")

    def _worker_loop(self) -> None:
        while True:
            item = self._event_queue.get()
            if item is self._worker_sentinel:
                return
            try:
                item()
            except Exception:
                logger.exception("event_worker_error")

    def start_worker(self) -> None:
        """启动事件 worker 线程。"""
        if self._worker_thread is not None:
            return
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="daobidao-event-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def stop_worker(self, timeout: float = 2.0) -> None:
        """通知 worker 退出并等待结束。"""
        if self._worker_thread is None:
            return
        self._event_queue.put(self._worker_sentinel)
        self._worker_thread.join(timeout=timeout)
        self._worker_thread = None

    def _process(self, wav_data: bytes) -> None:
        """处理识别和输入（在后台线程中运行）。"""
        try:
            text = self.stt.transcribe(wav_data)
            if text:
                logger.info(
                    "transcription_complete",
                    message=t("main.result", text=text),
                    text_length=len(text),
                )
                type_text(text)
            else:
                logger.warning(
                    "no_text_recognized", message=t("main.no_text")
                )
        except Exception:
            logger.exception(
                "recognize_failed", message=t("main.recognize_fail", error="")
            )
        finally:
            self._processing = False
            self._notify_status("ready")

    def on_config_changed(self, changes: dict) -> None:
        """设置页面保存后回调，即时更新可热更新的配置。"""
        if "sound.enabled" in changes:
            self.sound_enabled = changes["sound.enabled"]
            key = "main.sound_on" if self.sound_enabled else "main.sound_off"
            logger.info(
                "config_toggle",
                setting="sound",
                enabled=self.sound_enabled,
                message=t(key),
            )
        if "overlay.enabled" in changes:
            self.overlay_enabled = changes["overlay.enabled"]
            key = (
                "main.overlay_on"
                if self.overlay_enabled
                else "main.overlay_off"
            )
            logger.info(
                "config_toggle",
                setting="overlay",
                enabled=self.overlay_enabled,
                message=t(key),
            )
        if "tray_status.enabled" in changes:
            self.tray_status_enabled = changes["tray_status.enabled"]
            key = (
                "main.tray_on"
                if self.tray_status_enabled
                else "main.tray_off"
            )
            logger.info(
                "config_toggle",
                setting="tray_status",
                enabled=self.tray_status_enabled,
                message=t(key),
            )
        if "ui.language" in changes:
            set_language(changes["ui.language"])
        if "qwen3.variant" in changes:
            self._switch_stt_variant(changes["qwen3.variant"])
        if "qwen3.streaming_mode" in changes:
            # 只改标志位,下次按键生效(按住录音中不中断切模式)
            self.streaming_mode = changes["qwen3.streaming_mode"]
            key = (
                "main.streaming_on"
                if self.streaming_mode
                else "main.streaming_off"
            )
            logger.info(
                "config_toggle",
                setting="streaming_mode",
                enabled=self.streaming_mode,
                message=t(key),
            )

    def preload_model(self) -> None:
        """预加载模型(让首次按热键时不要卡在加载)。"""
        logger.info("model_preload_start", message=t("main.preload"))
        self.stt.load()
        self._notify_status("ready")

    # ------------------------------------------------------------------
    # STT 热切换(设置页面"识别模型"下拉触发)
    # ------------------------------------------------------------------

    def stt_switch_status(self) -> dict:
        """返回当前切换状态的浅拷贝,供 settings_server 查询。"""
        with self._stt_switch_lock:
            return dict(self._stt_switch_state)

    def _switch_stt_variant(self, new_variant: str) -> None:
        """在后台线程把 STT 切到新 variant,加载完成后原子替换 self.stt。

        并发约束:正在切换时再次请求会被忽略(UI 层也会禁用下拉阻止)。
        相同 variant:立刻返回,不做任何事。
        失败:旧 self.stt 保持不变,error 字段由 UI 轮询读到后提示用户。
        """
        current = getattr(self.stt, "variant", None)
        with self._stt_switch_lock:
            if self._stt_switch_state["switching"]:
                logger.warning(
                    "stt_switch_already_in_progress",
                    target=self._stt_switch_state["target_variant"],
                )
                return
            if current == new_variant:
                return
            self._stt_switch_state.update(
                switching=True,
                target_variant=new_variant,
                error=None,
            )

        logger.info(
            "stt_switch_start",
            from_variant=current,
            to_variant=new_variant,
        )

        def _worker() -> None:
            import gc

            from daobidao.stt.qwen3 import Qwen3ASRSTT

            try:
                new_stt = Qwen3ASRSTT(variant=new_variant)
                new_stt.load()
                old_stt = self.stt
                self.stt = new_stt
                del old_stt
                gc.collect()
                with self._stt_switch_lock:
                    self._stt_switch_state.update(
                        switching=False,
                        target_variant=None,
                        error=None,
                    )
                logger.info(
                    "stt_switch_done", to_variant=new_variant
                )
            except Exception as exc:
                logger.exception(
                    "stt_switch_failed", to_variant=new_variant
                )
                with self._stt_switch_lock:
                    self._stt_switch_state.update(
                        switching=False,
                        target_variant=None,
                        error=str(exc),
                    )

        threading.Thread(
            target=_worker, name="stt-switch", daemon=True
        ).start()


def main():
    # 先加载 i18n（argparse 之前需要用到翻译）
    load_locales()

    # 老 whisper-input 用户的本地数据 best-effort 搬运,幂等(用 marker 文件)。
    # 必须在 ConfigManager / configure_logging 触达任何路径之前完成,否则
    # 新路径会被先创建,后面的 _move_dir 检查到"目标已存在"就拒绝迁移。
    with contextlib.suppress(Exception):
        from daobidao._legacy_migration import migrate_once

        migrate_once()

    # logger 尽早初始化:配置文件还没加载,先用默认 INFO。
    # 配置读入后会再次调用 configure_logging(config["log_level"]) 生效(幂等)。
    configure_logging("INFO")

    # 先用默认配置解析命令行（获取 -c 指定的配置文件路径）
    parser = argparse.ArgumentParser(
        description=t("cli.description")
    )
    _commit_suffix = f" (commit {__commit__[:7]})" if __commit__ else ""
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"daobidao {__version__}{_commit_suffix}",
    )
    parser.add_argument(
        "-c", "--config", help=t("cli.config_help")
    )
    parser.add_argument(
        "-k", "--hotkey", help=t("cli.hotkey_help")
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help=t("cli.no_tray_help"),
    )
    parser.add_argument(
        "--no-preload",
        action="store_true",
        help=t("cli.no_preload_help"),
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help=t("cli.init_help"),
    )
    parser.add_argument(
        "--allow-multiple",
        action="store_true",
        help=t("cli.allow_multiple_help"),
    )
    if sys.platform == "darwin":
        parser.add_argument(
            "--uninstall",
            action="store_true",
            help=t("cli.uninstall_help"),
        )
    args = parser.parse_args()

    # --init: 一次性完成安装后初始化
    if args.init:
        logger.info("init_start", message=t("init.start"))

        # macOS: 安装 .app bundle
        if sys.platform == "darwin":
            from daobidao.backends.app_bundle_macos import (
                install_app_bundle,
            )

            install_app_bundle()

        # 下载 STT 模型
        logger.info(
            "init_download_model", message=t("init.download_model")
        )
        config_mgr = ConfigManager(args.config)
        stt = create_stt_engine(config_mgr.config)
        stt.load()
        logger.info("init_model_ready", message=t("init.model_ready"))

        logger.info("init_done", message=t("init.done"))
        return

    # macOS: 处理 --install-app 和 bundle 自动安装/重定向
    if sys.platform == "darwin":
        from daobidao.backends.app_bundle_macos import (
            BUNDLE_ENV_KEY,
            install_app_bundle,
            is_app_bundle_installed,
            is_app_bundle_outdated,
            launch_via_bundle,
            update_venv_path,
        )

        if getattr(args, "uninstall", False):
            from daobidao.backends.app_bundle_macos import (
                uninstall_cleanup,
            )

            uninstall_cleanup()
            return

        if not os.environ.get(BUNDLE_ENV_KEY):
            if (
                not is_app_bundle_installed()
                or is_app_bundle_outdated()
            ):
                # 首次运行或版本升级：安装/更新 .app bundle
                install_app_bundle()
            # 每次都更新 venv 路径（适应 uv tool upgrade）
            update_venv_path()
            launch_via_bundle(sys.argv[1:])

    # 加载配置
    config_mgr = ConfigManager(args.config)
    config = config_mgr.config

    # 用户配置的 log_level 覆盖早期默认(idempotent re-configure)
    configure_logging(config.get("log_level", "INFO"))

    # 从配置更新语言（覆盖默认值）
    set_language(config.get("ui", {}).get("language", "zh"))

    # 命令行参数覆盖配置
    from daobidao.config_manager import HOTKEY_CONFIG_KEY

    if args.hotkey:
        config[HOTKEY_CONFIG_KEY] = args.hotkey

    hotkey = config.get(HOTKEY_CONFIG_KEY, "KEY_RIGHTCTRL")
    engine = config.get("engine", "qwen3")

    logger.info(
        "startup_banner",
        engine=engine,
        hotkey=hotkey,
        message=t("main.banner"),
    )

    # macOS: 启动前检查辅助功能权限
    # check_macos_permissions() 要么返回 True，要么阻塞等待授权然后重启
    if sys.platform == "darwin":
        from daobidao.backends.hotkey_macos import check_macos_permissions

        check_macos_permissions()

    # 启动序列前置:检测并清理占着 settings_port 的旧实例。
    # 避免 SettingsServer.start() 因端口被占抛 OSError 静默崩溃,
    # 实现"双击启动 = 重启"的 UX。详见 docs/31-启动时清理已有实例/。
    if not args.allow_multiple:
        from daobidao.single_instance import kill_stale_instance

        settings_port = config.get("settings_port", 51230)
        if not kill_stale_instance(settings_port):
            logger.error(
                "stale_instance_blocked_startup",
                port=settings_port,
                message=t("main.stale_instance_blocked", port=settings_port),
            )
            sys.exit(1)

    # 创建主控制器
    wi = WhisperInput(config)
    wi.start_worker()

    # 启动设置服务器
    from daobidao.settings_server import SettingsServer

    settings_server = SettingsServer(
        config_manager=config_mgr,
        on_config_changed=wi.on_config_changed,
        port=config.get("settings_port", 51230),
        stt_switch_status_getter=wi.stt_switch_status,
    )
    settings_server.start()

    # 初始化录音浮窗
    try:
        from daobidao.overlay import RecordingOverlay

        wi.set_overlay(RecordingOverlay())
    except ImportError:
        logger.warning(
            "overlay_unavailable", message=t("main.overlay_unavail")
        )

    # 预加载模型
    if not args.no_preload:
        wi.preload_model()

    # 启动热键监听
    listener = HotkeyListener(
        hotkey=hotkey,
        on_press=wi.on_key_press,
        on_release=wi.on_key_release,
    )

    # 优雅退出：托盘菜单和信号共用一套清理逻辑
    #
    # 注意：Linux 下托盘菜单的 quit 回调跑在 pystray daemon 线程里，
    # 在那里调用 sys.exit() 只会干掉该线程、不会让主线程退出。
    # 用 Event 把"该退出了"信号从任意线程传回主线程，由主线程统一 sys.exit。
    _shutting_down = False
    _shutdown_event = threading.Event()
    # terminate_portaudio 超时时置位,末尾走 os._exit(0) 跳过 atexit。
    # 见 docs/24-退出路径CoreAudio死锁修复/。
    _force_exit = False

    def shutdown():
        nonlocal _shutting_down, _force_exit
        if _shutting_down:
            return
        _shutting_down = True
        logger.info("shutting_down", message=t("main.shutting_down"))
        with contextlib.suppress(Exception):
            listener.stop()
        with contextlib.suppress(Exception):
            wi.stop_worker()
        # worker 停完再关 PortAudio,保证此时没有 start/stop stream 在飞。
        if not terminate_portaudio(timeout=2.0):
            _force_exit = True
        with contextlib.suppress(Exception):
            settings_server.stop()
        _shutdown_event.set()

    def _final_exit() -> None:
        if _force_exit:
            logger.warning(
                "force_exit",
                message="PortAudio 未能及时终止,跳过 Python finalize 强退",
            )
            os._exit(0)
        sys.exit(0)

    def signal_handler(sig, frame):
        shutdown()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    listener.start()

    logger.info(
        "ready",
        message=t("main.ready"),
        exit_hint=t("main.exit_hint"),
    )

    # 启动系统托盘
    run_tray = None
    if not args.no_tray:
        try:
            from daobidao.tray import run_tray
        except ImportError:
            logger.warning(
                "tray_unavailable", message=t("main.no_tray")
            )

    if run_tray is not None:
        tray_icon = run_tray(wi, settings_server, on_quit=shutdown)
        # 模型已预加载完，同步状态到托盘图标
        if not args.no_preload:
            wi._notify_status("ready")
        if tray_icon is not None:
            # macOS: icon.run() 阻塞主线程（AppKit 要求）
            tray_icon.run()
            _final_exit()
            return

    # Linux 或 --no-tray: 主线程等 shutdown 事件（信号或托盘 quit 回调触发）
    _shutdown_event.wait()
    _final_exit()


if __name__ == "__main__":
    main()
