#!/usr/bin/env python3
"""Whisper Input - 语音输入工具

按住快捷键说话，松开后自动将语音识别结果输入到当前焦点窗口。
支持中英文混合输入，使用本地 SenseVoice 模型。

用法:
    uv run whisper-input                 # 使用默认配置
    uv run whisper-input -k KEY_RIGHTALT  # 使用右Alt键
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

import subprocess
import threading

from whisper_input.config_manager import ConfigManager
from whisper_input.hotkey import HotkeyListener
from whisper_input.input_method import type_text
from whisper_input.recorder import AudioRecorder


def create_stt_engine(config: dict):
    """根据配置创建 STT 引擎。"""
    engine = config.get("engine", "sensevoice")
    engine_config = config.get(engine, {})

    from whisper_input.stt import create_stt

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


class WhisperInput:
    """语音输入主控制器。"""

    def __init__(self, config: dict):
        self.config = config
        self.recorder = AudioRecorder(
            sample_rate=config.get("audio", {}).get("sample_rate", 16000),
            channels=config.get("audio", {}).get("channels", 1),
        )
        self.stt = create_stt_engine(config)
        self.input_method = config.get("input_method", "clipboard")
        self.sound_enabled = config.get("sound", {}).get("enabled", True)
        from whisper_input.config_manager import _SOUND_SUFFIX

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
                self._overlay.update("识别中...")
            elif status == "ready":
                self._overlay.hide()

    def on_key_press(self) -> None:
        """热键按下 - 开始录音。"""
        if self._processing:
            return
        print("[main] 开始录音...")
        self._notify_status("recording")
        # 连接音量回调到浮窗
        if self._overlay and self.overlay_enabled:
            self.recorder.on_level = self._overlay.set_level
        if self.sound_enabled:
            play_sound(self.sound_start)
        self.recorder.start()

    def on_key_release(self) -> None:
        """热键释放 - 停止录音并识别。"""
        if not self.recorder.is_recording:
            return
        print("[main] 停止录音，识别中...")
        self.recorder.on_level = None
        self._notify_status("processing")
        if self.sound_enabled:
            play_sound(self.sound_stop)

        wav_data = self.recorder.stop()
        if not wav_data:
            print("[main] 未录到音频")
            return

        # 在后台线程中处理识别，避免阻塞热键监听
        self._processing = True
        threading.Thread(
            target=self._process, args=(wav_data,), daemon=True
        ).start()

    def _process(self, wav_data: bytes) -> None:
        """处理识别和输入（在后台线程中运行）。"""
        try:
            text = self.stt.transcribe(wav_data)
            if text:
                print(f"[main] 识别结果: {text}")
                type_text(text, method=self.input_method)
            else:
                print("[main] 未识别到文字")
        except Exception as e:
            print(f"[main] 识别失败: {e}")
        finally:
            self._processing = False
            self._notify_status("ready")

    def on_config_changed(self, changes: dict) -> None:
        """设置页面保存后回调，即时更新可热更新的配置。"""
        if "sound.enabled" in changes:
            self.sound_enabled = changes["sound.enabled"]
            print(f"[main] 提示音已{'开启' if self.sound_enabled else '关闭'}")
        if "input_method" in changes:
            self.input_method = changes["input_method"]
            print(f"[main] 输入方式已切换为: {self.input_method}")
        if "sensevoice.language" in changes:
            self.stt.language = changes["sensevoice.language"]
            print(f"[main] 识别语言已切换为: {self.stt.language}")
        if "overlay.enabled" in changes:
            self.overlay_enabled = changes["overlay.enabled"]
            print(
                f"[main] 录音浮窗已"
                f"{'开启' if self.overlay_enabled else '关闭'}"
            )
        if "tray_status.enabled" in changes:
            self.tray_status_enabled = changes["tray_status.enabled"]
            print(
                f"[main] 托盘状态已"
                f"{'开启' if self.tray_status_enabled else '关闭'}"
            )

    def preload_model(self) -> None:
        """预加载模型(让首次按热键时不要卡在加载)。"""
        print("[main] 预加载 STT 模型...")
        self.stt.load()
        self._notify_status("ready")


def main():
    parser = argparse.ArgumentParser(
        description="Whisper Input - 语音输入工具"
    )
    parser.add_argument("-c", "--config", help="配置文件路径")
    parser.add_argument("-k", "--hotkey", help="热键 (如 KEY_RIGHTCTRL)")
    parser.add_argument("--no-tray", action="store_true", help="禁用系统托盘")
    parser.add_argument(
        "--no-preload", action="store_true", help="不预加载模型"
    )
    args = parser.parse_args()

    # 加载配置
    config_mgr = ConfigManager(args.config)
    config = config_mgr.config

    # 命令行参数覆盖配置
    from whisper_input.config_manager import HOTKEY_CONFIG_KEY

    if args.hotkey:
        config[HOTKEY_CONFIG_KEY] = args.hotkey

    hotkey = config.get(HOTKEY_CONFIG_KEY, "KEY_RIGHTCTRL")
    engine = config.get("engine", "sensevoice")

    print("=" * 50)
    print("  Whisper Input - 语音输入")
    print("=" * 50)
    print(f"  引擎: {engine}")
    print(f"  热键: {hotkey} (按住说话，松开输入)")
    print(f"  输入: {config.get('input_method', 'clipboard')}")
    print("=" * 50)

    # macOS: 启动前检查辅助功能和输入监控权限
    if sys.platform == "darwin":
        from whisper_input.backends.hotkey_macos import check_macos_permissions

        if not check_macos_permissions():
            print("[main] 权限不足，请授权后重新启动程序")
            sys.exit(1)

    # 创建主控制器
    wi = WhisperInput(config)

    # 启动设置服务器
    from whisper_input.settings_server import SettingsServer

    settings_server = SettingsServer(
        config_manager=config_mgr,
        on_config_changed=wi.on_config_changed,
        port=config.get("settings_port", 51230),
    )
    settings_server.start()

    # 初始化录音浮窗
    try:
        from whisper_input.overlay import RecordingOverlay

        wi.set_overlay(RecordingOverlay())
    except ImportError:
        print("[main] 录音浮窗不可用")

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

    def shutdown():
        nonlocal _shutting_down
        if _shutting_down:
            return
        _shutting_down = True
        print("\n[main] 正在退出...")
        with contextlib.suppress(Exception):
            settings_server.stop()
        with contextlib.suppress(Exception):
            listener.stop()
        _shutdown_event.set()

    def signal_handler(sig, frame):
        shutdown()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    listener.start()

    print("[main] 就绪！按住热键开始说话")
    print("[main] Ctrl+C 退出")

    # 启动系统托盘
    run_tray = None
    if not args.no_tray:
        try:
            from whisper_input.tray import run_tray
        except ImportError:
            print("[main] pystray/Pillow 未安装，跳过系统托盘")

    if run_tray is not None:
        tray_icon = run_tray(wi, settings_server, on_quit=shutdown)
        # 模型已预加载完，同步状态到托盘图标
        if not args.no_preload:
            wi._notify_status("ready")
        if tray_icon is not None:
            # macOS: icon.run() 阻塞主线程（AppKit 要求）
            tray_icon.run()
            return

    # Linux 或 --no-tray: 主线程等 shutdown 事件（信号或托盘 quit 回调触发）
    _shutdown_event.wait()
    sys.exit(0)


if __name__ == "__main__":
    main()
