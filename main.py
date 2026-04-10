#!/usr/bin/env python3
"""Whisper Input - 语音输入工具

按住快捷键说话，松开后自动将语音识别结果输入到当前焦点窗口。
支持中英文混合输入，使用本地 SenseVoice 模型。

用法:
    python main.py                    # 使用默认配置
    python main.py -k KEY_RIGHTALT    # 使用右Alt键
"""

import argparse
import contextlib
import os
import signal
import sys

# Linux: PyGObject 需要指定系统 typelib 路径
if sys.platform == "linux":
    if "GI_TYPELIB_PATH" not in os.environ:
        _typelib_dir = "/usr/lib/girepository-1.0"
        if os.path.isdir(_typelib_dir):
            os.environ["GI_TYPELIB_PATH"] = _typelib_dir

import subprocess
import threading

from config_manager import ConfigManager
from hotkey import HotkeyListener
from input_method import type_text
from recorder import AudioRecorder


def create_stt_engine(config: dict):
    """根据配置创建 STT 引擎。"""
    engine = config.get("engine", "sensevoice")

    if engine == "sensevoice":
        from stt_sensevoice import SenseVoiceSTT

        sv_config = config.get("sensevoice", {})
        return SenseVoiceSTT(
            model=sv_config.get("model", "iic/SenseVoiceSmall"),
            device_priority=sv_config.get(
                "device_priority", ["cuda", "mps", "cpu"]
            ),
            language=sv_config.get("language", "auto"),
        )
    else:
        raise ValueError(f"未知的 STT 引擎: {engine}")


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
        self.sound_start = config.get("sound", {}).get("start", "")
        self.sound_stop = config.get("sound", {}).get("stop", "")
        self._processing = False

    def on_key_press(self) -> None:
        """热键按下 - 开始录音。"""
        if self._processing:
            return
        print("[main] 开始录音...")
        if self.sound_enabled:
            play_sound(self.sound_start)
        self.recorder.start()

    def on_key_release(self) -> None:
        """热键释放 - 停止录音并识别。"""
        if not self.recorder.is_recording:
            return
        print("[main] 停止录音，识别中...")
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

    def preload_model(self) -> None:
        """预加载模型（仅本地引擎需要）。"""
        if self.config.get("engine") == "sensevoice":
            cache_dir = os.environ.get(
                "MODELSCOPE_CACHE", "~/.cache/modelscope/hub"
            )
            print(f"[main] 预加载 SenseVoice 模型 (缓存: {cache_dir})")
            self.stt._ensure_model()


def run_tray(wi: WhisperInput, settings_server) -> None:
    """运行系统托盘图标。"""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print("[main] pystray/Pillow 未安装，跳过系统托盘")
        return

    def create_icon(color: str = "green") -> Image.Image:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        colors = {
            "green": "#4CAF50",
            "red": "#F44336",
            "gray": "#9E9E9E",
        }
        fill = colors.get(color, colors["green"])
        draw.ellipse([8, 8, 56, 56], fill=fill)
        draw.rectangle([24, 16, 40, 38], fill="white")
        draw.arc([20, 28, 44, 52], 0, 180, fill="white", width=3)
        draw.line([32, 52, 32, 58], fill="white", width=3)
        return img

    def open_settings(icon, item):
        if settings_server:
            settings_server.open_in_browser()

    def quit_app(icon, item):
        icon.stop()
        os.kill(os.getpid(), signal.SIGTERM)

    menu = pystray.Menu(
        pystray.MenuItem("设置...", open_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )

    icon = pystray.Icon("whisper-input", create_icon(), "Whisper Input", menu)
    if sys.platform == "darwin":
        # macOS: AppKit 要求 NSApplication 在主线程运行，
        # icon.run() 必须在主线程调用（由 main() 负责）
        return icon
    else:
        # Linux: appindicator 后端下 run_detached() 不显示图标，
        # 用 daemon 线程运行 run()
        threading.Thread(target=icon.run, daemon=True).start()
        return None


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
    if args.hotkey:
        config["hotkey"] = args.hotkey

    hotkey = config.get("hotkey", "KEY_RIGHTCTRL")
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
        from backends.hotkey_macos import check_macos_permissions

        if not check_macos_permissions():
            print("[main] 权限不足，请授权后重新启动程序")
            sys.exit(1)

    # 创建主控制器
    wi = WhisperInput(config)

    # 启动设置服务器
    from settings_server import SettingsServer

    settings_server = SettingsServer(
        config_manager=config_mgr,
        on_config_changed=wi.on_config_changed,
        port=config.get("settings_port", 51230),
    )
    settings_server.start()

    # 预加载模型
    if not args.no_preload:
        wi.preload_model()

    # 启动热键监听
    listener = HotkeyListener(
        hotkey=hotkey,
        on_press=wi.on_key_press,
        on_release=wi.on_key_release,
    )

    # 优雅退出
    def signal_handler(sig, frame):
        print("\n[main] 正在退出...")
        settings_server.stop()
        listener.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    listener.start()

    print("[main] 就绪！按住热键开始说话")
    print("[main] Ctrl+C 退出")

    # 启动系统托盘
    if not args.no_tray:
        tray_icon = run_tray(wi, settings_server)
        if tray_icon is not None:
            # macOS: icon.run() 阻塞主线程（AppKit 要求）
            tray_icon.run()
            return

    # Linux 或 --no-tray: 主线程等待信号
    signal.pause()


if __name__ == "__main__":
    main()
