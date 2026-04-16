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
from whisper_input.i18n import load_locales, set_language, t
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
                self._overlay.update(t("main.processing"))
            elif status == "ready":
                self._overlay.hide()

    def on_key_press(self) -> None:
        """热键按下 - 开始录音。"""
        if self._processing:
            return
        print(f"[main] {t('main.recording_start')}")
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
        print(f"[main] {t('main.recording_stop')}")
        self.recorder.on_level = None
        self._notify_status("processing")
        if self.sound_enabled:
            play_sound(self.sound_stop)

        wav_data = self.recorder.stop()
        if not wav_data:
            print(f"[main] {t('main.no_audio')}")
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
                print(f"[main] {t('main.result', text=text)}")
                type_text(text)
            else:
                print(f"[main] {t('main.no_text')}")
        except Exception as e:
            print(f"[main] {t('main.recognize_fail', error=e)}")
        finally:
            self._processing = False
            self._notify_status("ready")

    def on_config_changed(self, changes: dict) -> None:
        """设置页面保存后回调，即时更新可热更新的配置。"""
        if "sound.enabled" in changes:
            self.sound_enabled = changes["sound.enabled"]
            key = "main.sound_on" if self.sound_enabled else "main.sound_off"
            print(f"[main] {t(key)}")
        if "overlay.enabled" in changes:
            self.overlay_enabled = changes["overlay.enabled"]
            key = (
                "main.overlay_on"
                if self.overlay_enabled
                else "main.overlay_off"
            )
            print(f"[main] {t(key)}")
        if "tray_status.enabled" in changes:
            self.tray_status_enabled = changes["tray_status.enabled"]
            key = (
                "main.tray_on"
                if self.tray_status_enabled
                else "main.tray_off"
            )
            print(f"[main] {t(key)}")
        if "ui.language" in changes:
            set_language(changes["ui.language"])

    def preload_model(self) -> None:
        """预加载模型(让首次按热键时不要卡在加载)。"""
        print(f"[main] {t('main.preload')}")
        self.stt.load()
        self._notify_status("ready")


def main():
    # 先加载 i18n（argparse 之前需要用到翻译）
    load_locales()

    # 先用默认配置解析命令行（获取 -c 指定的配置文件路径）
    parser = argparse.ArgumentParser(
        description=t("cli.description")
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
    if sys.platform == "darwin":
        parser.add_argument(
            "--uninstall",
            action="store_true",
            help=t("cli.uninstall_help"),
        )
    args = parser.parse_args()

    # --init: 一次性完成安装后初始化
    if args.init:
        print(f"[init] {t('init.start')}")

        # macOS: 安装 .app bundle
        if sys.platform == "darwin":
            from whisper_input.backends.app_bundle_macos import (
                install_app_bundle,
            )

            install_app_bundle()

        # 下载 STT 模型
        print(f"[init] {t('init.download_model')}")
        config_mgr = ConfigManager(args.config)
        stt = create_stt_engine(config_mgr.config)
        stt.load()
        print(f"[init] {t('init.model_ready')}")

        print()
        print(t("init.done"))
        return

    # macOS: 处理 --install-app 和 bundle 自动安装/重定向
    if sys.platform == "darwin":
        from whisper_input.backends.app_bundle_macos import (
            BUNDLE_ENV_KEY,
            install_app_bundle,
            is_app_bundle_installed,
            is_app_bundle_outdated,
            launch_via_bundle,
            update_venv_path,
        )

        if getattr(args, "uninstall", False):
            from whisper_input.backends.app_bundle_macos import (
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

    # 从配置更新语言（覆盖默认值）
    set_language(config.get("ui", {}).get("language", "zh"))

    # 命令行参数覆盖配置
    from whisper_input.config_manager import HOTKEY_CONFIG_KEY

    if args.hotkey:
        config[HOTKEY_CONFIG_KEY] = args.hotkey

    hotkey = config.get(HOTKEY_CONFIG_KEY, "KEY_RIGHTCTRL")
    engine = config.get("engine", "sensevoice")

    print("=" * 50)
    print(f"  {t('main.banner')}")
    print("=" * 50)
    print(f"  {t('main.engine', engine=engine)}")
    print(f"  {t('main.hotkey', hotkey=hotkey)}")
    print("=" * 50)

    # macOS: 启动前检查辅助功能和输入监控权限
    if sys.platform == "darwin":
        from whisper_input.backends.hotkey_macos import check_macos_permissions

        if not check_macos_permissions():
            print(f"[main] {t('main.perm_fail')}")
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
        print(f"[main] {t('main.overlay_unavail')}")

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
        print(f"\n[main] {t('main.shutting_down')}")
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

    print(f"[main] {t('main.ready')}")
    print(f"[main] {t('main.exit_hint')}")

    # 启动系统托盘
    run_tray = None
    if not args.no_tray:
        try:
            from whisper_input.tray import run_tray
        except ImportError:
            print(f"[main] {t('main.no_tray')}")

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
