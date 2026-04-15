#!/usr/bin/env python3
"""Whisper Input - 语音输入工具

按住快捷键说话，松开后自动将语音识别结果输入到当前焦点窗口。
支持中英文混合输入，使用本地 SenseVoice 模型。

用法:
    python main.py                    # 使用默认配置
    python main.py -k KEY_RIGHTALT    # 使用右Alt键
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

from config_manager import ConfigManager
from hotkey import HotkeyListener
from input_method import type_text
from recorder import AudioRecorder


def create_stt_engine(config: dict):
    """根据配置创建 STT 引擎。"""
    engine = config.get("engine", "sensevoice")
    engine_config = config.get(engine, {})

    from stt import create_stt

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
        from config_manager import _SOUND_SUFFIX

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


def run_tray(wi: WhisperInput, settings_server, on_quit) -> None:
    """运行系统托盘图标。"""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print("[main] pystray/Pillow 未安装，跳过系统托盘")
        return

    # Linux/Xorg pystray 用 latin-1 编码 WM_NAME，不支持非 ASCII
    if sys.platform == "linux":
        status_tips = {
            "loading": "Whisper Input - Loading...",
            "ready": "Whisper Input - Ready",
            "recording": "Whisper Input - Recording",
            "processing": "Whisper Input - Processing...",
        }
    else:
        status_tips = {
            "loading": "Whisper Input - 加载中...",
            "ready": "Whisper Input - 就绪",
            "recording": "Whisper Input - 录音中",
            "processing": "Whisper Input - 识别中...",
        }

    # macOS 菜单栏规范:用模板图(纯黑+透明)由系统自动反色,
    # 仅 recording 状态叠加红点作为活跃指示(非模板图)。
    # 源图画得足够大,配合 Retina setSize_ 才清晰。
    #
    # Linux 侧 pystray 走 AppIndicator,不会做模板反色,纯黑图标
    # 在深色面板里几乎看不见,所以按状态用品牌色:
    #   loading=灰,ready=绿(#4CAF50),processing=橙(#FF9800),
    #   recording=红(#F44336)。macOS 保持纯黑模板图不变。
    icon_src = 128
    is_mac = sys.platform == "darwin"

    def _status_color(status: str) -> tuple[int, int, int, int]:
        if is_mac:
            return (0, 0, 0, 255)
        return {
            "loading": (158, 158, 158, 255),
            "ready": (76, 175, 80, 255),
            "processing": (255, 152, 0, 255),
            "recording": (244, 67, 54, 255),
        }.get(status, (76, 175, 80, 255))

    def _draw_mic(
        draw: ImageDraw.ImageDraw,
        filled: bool,
        color: tuple[int, int, int, int],
    ) -> None:
        width = 12
        if filled:
            draw.rounded_rectangle(
                [40, 16, 88, 76], radius=24, fill=color
            )
        else:
            draw.rounded_rectangle(
                [40, 16, 88, 76],
                radius=24,
                outline=color,
                width=width,
            )
        draw.arc([20, 36, 108, 104], 0, 180, fill=color, width=width)
        draw.line([64, 96, 64, 116], fill=color, width=width)
        draw.line([40, 116, 88, 116], fill=color, width=width)

    def create_icon(status: str = "loading") -> Image.Image:
        img = Image.new("RGBA", (icon_src, icon_src), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        color = _status_color(status)
        if status == "ready":
            _draw_mic(draw, filled=False, color=color)
        elif status == "processing":
            _draw_mic(draw, filled=True, color=color)
        elif status == "loading":
            _draw_mic(draw, filled=False, color=color)
            # 加载中:底部省略号
            dot_color = (*color[:3], 160)
            for cx in (40, 64, 88):
                draw.ellipse([cx - 6, 112, cx + 6, 124], fill=dot_color)
        elif status == "recording":
            _draw_mic(draw, filled=True, color=color)
            if is_mac:
                # macOS 模板图是纯黑的,需要额外红点徽标提示"正在录音"
                draw.ellipse(
                    [84, 4, 124, 44], fill=(244, 67, 54, 255)
                )
        return img

    # recording 状态不能作为 template image(需要保留红色)
    def _is_template(status: str) -> bool:
        return status != "recording"

    def open_settings(icon, item):
        if settings_server:
            settings_server.open_in_browser()

    def quit_app(icon, item):
        icon.stop()
        on_quit()

    from version import __version__

    menu = pystray.Menu(
        pystray.MenuItem(
            f"Whisper Input v{__version__}",
            None,
            enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("设置...", open_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_app),
    )

    icon = pystray.Icon(
        "whisper-input",
        create_icon("loading"),
        status_tips["loading"],
        menu,
    )
    icon._wi_template = True  # loading 状态用模板图

    # macOS: 替换 pystray 的 _assert_image,用 Retina 像素尺寸构建 NSImage
    # 并标记为 template image,让系统按菜单栏主题自动适配。
    if sys.platform == "darwin":
        import io as _io

        import AppKit  # type: ignore
        import Foundation  # type: ignore

        def _patched_assert_image():
            thickness = int(icon._status_bar.thickness())
            scale = 2  # Retina
            px = thickness * scale
            source = icon._icon.resize(
                (px, px), Image.Resampling.LANCZOS
            )
            buf = _io.BytesIO()
            source.save(buf, "png")
            data = Foundation.NSData.dataWithBytes_length_(
                buf.getvalue(), len(buf.getvalue())
            )
            ns_image = AppKit.NSImage.alloc().initWithData_(data)
            # 告诉 AppKit 这是 thickness 点 × 2 像素的高分图
            ns_image.setSize_((thickness, thickness))
            ns_image.setTemplate_(
                bool(getattr(icon, "_wi_template", True))
            )
            icon._icon_image = ns_image
            icon._status_item.button().setImage_(ns_image)

        icon._assert_image = _patched_assert_image

    def on_status_change(status: str) -> None:
        icon._wi_template = _is_template(status)
        icon.icon = create_icon(status)
        icon.title = status_tips.get(
            status, status_tips["ready"]
        )

    wi.set_status_callback(on_status_change)

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
    from config_manager import HOTKEY_CONFIG_KEY

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

    # 初始化录音浮窗
    try:
        from overlay import RecordingOverlay

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
    if not args.no_tray:
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
