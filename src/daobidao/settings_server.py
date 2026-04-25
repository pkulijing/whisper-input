"""设置页面 Web 服务 - 提供浏览器设置界面和 REST API。"""

import json
import os
import signal
import subprocess
import sys
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from string import Template

from daobidao.backends import IS_MACOS
from daobidao.config_manager import ConfigManager
from daobidao.i18n import get_all_locales, get_language, t
from daobidao.logger import get_log_dir, get_logger
from daobidao.updater import UpdateChecker, apply_upgrade

logger = get_logger(__name__)

# 支持的热键 key code 列表（按平台不同，标签由前端从 locale 查找）
if IS_MACOS:
    SUPPORTED_KEY_CODES = [
        "KEY_RIGHTCTRL",
        "KEY_LEFTCTRL",
        "KEY_RIGHTALT",
        "KEY_LEFTALT",
        "KEY_RIGHTMETA",
        "KEY_LEFTMETA",
        "KEY_CAPSLOCK",
        "KEY_F1",
        "KEY_F2",
        "KEY_F5",
        "KEY_F12",
    ]
else:
    SUPPORTED_KEY_CODES = [
        "KEY_RIGHTCTRL",
        "KEY_LEFTCTRL",
        "KEY_CAPSLOCK",
        "KEY_F1",
        "KEY_F2",
        "KEY_F12",
    ]

# 自启动：委托给平台后端
if IS_MACOS:
    from daobidao.backends.autostart_macos import (  # noqa: I001
        is_autostart_enabled as _is_autostart_enabled,
        set_autostart as _set_autostart,
    )
else:
    from daobidao.backends.autostart_linux import (  # noqa: I001
        is_autostart_enabled as _is_autostart_enabled,
        set_autostart as _set_autostart,
    )


_SETTINGS_TEMPLATE: Template | None = None


def _load_settings_template() -> Template:
    """从 assets/settings.html 加载模板（首次调用时缓存）。"""
    global _SETTINGS_TEMPLATE
    if _SETTINGS_TEMPLATE is None:
        src = (
            files("daobidao.assets")
            .joinpath("settings.html")
            .read_text(encoding="utf-8")
        )
        _SETTINGS_TEMPLATE = Template(src)
    return _SETTINGS_TEMPLATE


def _get_settings_html() -> str:
    """生成设置页面 HTML，注入选项数据和 locale 翻译。"""
    from daobidao.config_manager import HOTKEY_CONFIG_KEY
    from daobidao.version import __commit__, __version__

    # commit 链接 —— 指向 tree/<sha> 而非 commit/<sha>
    # （tree 是该 commit 时刻的文件浏览页，更贴近"我现在跑的代码长什么样"的意图）
    if __commit__:
        short = __commit__[:7]
        commit_html = (
            f'(<a href="https://github.com/pkulijing/'
            f'daobidao/tree/{__commit__}"'
            f' target="_blank">{short}</a>)'
        )
    else:
        commit_html = ""

    # safe_substitute 不会对 locale JSON 中的 $USER 等误解析
    return _load_settings_template().safe_substitute(
        hotkey_codes=json.dumps(SUPPORTED_KEY_CODES),
        hotkey_key=HOTKEY_CONFIG_KEY,
        hotkey_default=(
            "KEY_RIGHTMETA" if IS_MACOS else "KEY_RIGHTCTRL"
        ),
        version=__version__,
        commit=commit_html,
        locale_data=json.dumps(
            get_all_locales(), ensure_ascii=False
        ),
        current_language=get_language(),
    )


class _SettingsHandler(BaseHTTPRequestHandler):
    """设置页面 HTTP 请求处理器。"""

    def log_message(self, format, *args):
        """静默 HTTP 日志。"""

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def do_GET(self):
        if self.path == "/":
            self._send_html(_get_settings_html())
        elif self.path == "/api/config":
            config_mgr: ConfigManager = self.server.config_manager
            config_mgr.load()
            self._send_json(config_mgr.config)
        elif self.path == "/api/autostart":
            self._send_json({"enabled": _is_autostart_enabled()})
        elif self.path == "/api/audio-devices":
            self._handle_audio_devices()
        elif self.path == "/api/update/check":
            self._handle_update_check()
        elif self.path == "/api/stt/switch_status":
            self._handle_stt_switch_status()
        elif self.path == "/api/pid":
            # 单实例检测专用:新启动的实例用这个端点验证占了
            # settings_port 的进程"是不是我们自己的 daobidao",再决定是否
            # SIGTERM/SIGKILL。详见 docs/31-启动时清理已有实例/。
            self._send_json({"pid": os.getpid()})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/config":
            self._handle_save_config()
        elif self.path == "/api/config/reset":
            self._handle_reset_config()
        elif self.path == "/api/autostart":
            self._handle_autostart()
        elif self.path == "/api/quit":
            self._handle_quit()
        elif self.path == "/api/restart":
            self._handle_restart()
        elif self.path == "/api/open-log-dir":
            self._handle_open_log_dir()
        elif self.path == "/api/update/apply":
            self._handle_update_apply()
        else:
            self.send_error(404)

    def _handle_save_config(self) -> None:
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json(
                {"error": t("server.invalid_json")}, 400
            )
            return

        config_mgr: ConfigManager = self.server.config_manager
        on_config_changed = self.server.on_config_changed

        for key, value in data.items():
            config_mgr.set(key, value)

        config_mgr.save()

        # 通知运行中的应用更新即时生效的配置
        if on_config_changed:
            on_config_changed(data)

        self._send_json({"ok": True})

    def _handle_reset_config(self) -> None:
        from daobidao.config_manager import DEFAULT_CONFIG

        config_mgr: ConfigManager = self.server.config_manager
        on_config_changed = self.server.on_config_changed

        config_mgr.save(DEFAULT_CONFIG.copy())

        if on_config_changed:
            on_config_changed(DEFAULT_CONFIG)

        self._send_json({"ok": True})

    def _handle_autostart(self) -> None:
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json(
                {"error": t("server.invalid_json")}, 400
            )
            return

        _set_autostart(data.get("enabled", False))
        self._send_json({"ok": True})

    def _handle_quit(self) -> None:
        self._send_json({"ok": True})
        # 延迟发送退出信号，让响应先返回
        threading.Timer(
            0.5,
            lambda: os.kill(os.getpid(), signal.SIGTERM),
        ).start()

    def _handle_open_log_dir(self) -> None:
        log_dir = get_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        opener = "open" if IS_MACOS else "xdg-open"
        try:
            subprocess.Popen(
                [opener, str(log_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, FileNotFoundError) as e:
            self._send_json({"ok": False, "error": str(e)}, 500)
            return
        self._send_json({"ok": True, "path": str(log_dir)})

    def _handle_audio_devices(self) -> None:
        import sounddevice as sd

        try:
            devices = sd.query_devices()
            default_input = sd.default.device[0]
            result = [
                {
                    "index": i,
                    "name": d["name"],
                    "channels": d["max_input_channels"],
                    "is_default": (i == default_input),
                }
                for i, d in enumerate(devices)
                if d["max_input_channels"] > 0
            ]
        except Exception:
            result = []
        self._send_json({"devices": result})

    def _handle_update_check(self) -> None:
        checker: UpdateChecker = self.server.update_checker
        config_mgr: ConfigManager = self.server.config_manager
        # 关开关时直接返回 has_update=False,不访问网络
        update_cfg = config_mgr.config.get("update") or {}
        if not update_cfg.get("check_enabled", True):
            snap = checker.snapshot
            snap["has_update"] = False
            self._send_json(snap)
            return
        # 首次打开设置页 / 缓存为空时顺手异步触发一次
        snap = checker.snapshot
        if snap["checked_at"] is None and not snap["checking"]:
            checker.trigger_async()
            snap = checker.snapshot
        self._send_json(snap)

    def _handle_update_apply(self) -> None:
        ok, output = apply_upgrade()
        self._send_json({"ok": ok, "output": output})

    def _handle_stt_switch_status(self) -> None:
        """返回当前 STT 热切换状态。

        无 getter 时默认 `{switching: False, target_variant: None, error: None}`
        —— 这样 UI 层可以统一轮询,不用区分"支持切换的 / 不支持切换的"部署。
        """
        getter = getattr(self.server, "stt_switch_status_getter", None)
        if getter is None:
            self._send_json(
                {
                    "switching": False,
                    "target_variant": None,
                    "error": None,
                }
            )
            return
        try:
            status = getter()
        except Exception as exc:  # pragma: no cover - 防御
            self._send_json(
                {
                    "switching": False,
                    "target_variant": None,
                    "error": str(exc),
                }
            )
            return
        self._send_json(status)

    def _handle_restart(self) -> None:
        self._send_json({"ok": True})

        def do_restart():
            if sys.platform == "darwin":
                from daobidao.backends.app_bundle_macos import (
                    BUNDLE_ENV_KEY,
                    restart_via_bundle,
                )

                if os.environ.get(BUNDLE_ENV_KEY):
                    restart_via_bundle()
                    return
            os.execv(sys.executable, [sys.executable, *sys.argv])

        # 延迟重启，让响应先返回
        threading.Timer(0.5, do_restart).start()


class SettingsServer:
    """设置页面 Web 服务器，在后台线程中运行。"""

    def __init__(
        self,
        config_manager: ConfigManager,
        on_config_changed=None,
        port: int = 51230,
        stt_switch_status_getter=None,
    ):
        self._config_manager = config_manager
        self._on_config_changed = on_config_changed
        self._stt_switch_status_getter = stt_switch_status_getter
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int = port
        self._update_checker = UpdateChecker()

    def start(self) -> int:
        """启动服务器，返回端口号。"""
        handler = partial(_SettingsHandler)
        self._server = HTTPServer(("127.0.0.1", self._port), handler)
        # 把 config_manager / 回调 / update_checker 挂到 server 上供 handler 访问
        self._server.config_manager = self._config_manager
        self._server.on_config_changed = self._on_config_changed
        self._server.update_checker = self._update_checker
        self._server.stt_switch_status_getter = (
            self._stt_switch_status_getter
        )

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "server_started",
            port=self._port,
            message=t("server.started", port=self._port),
        )
        # 启动时按配置异步检查一次(dev 模式 / 开关关闭自动跳过)
        update_cfg = self._config_manager.config.get("update") or {}
        if update_cfg.get("check_enabled", True):
            self._update_checker.trigger_async()
        return self._port

    @property
    def port(self) -> int:
        return self._port

    def open_in_browser(self) -> None:
        """在默认浏览器中打开设置页面。"""
        if self._port:
            webbrowser.open(f"http://127.0.0.1:{self._port}")

    def stop(self) -> None:
        """停止服务器。"""
        if self._server:
            self._server.shutdown()
