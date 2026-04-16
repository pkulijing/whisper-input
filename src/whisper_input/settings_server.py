"""设置页面 Web 服务 - 提供浏览器设置界面和 REST API。"""

import json
import os
import signal
import sys
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from string import Template

from whisper_input.backends import IS_MACOS
from whisper_input.config_manager import ConfigManager

# 支持的热键列表（按平台不同）
if IS_MACOS:
    SUPPORTED_KEYS = [
        ("KEY_RIGHTCTRL", "右 Control"),
        ("KEY_LEFTCTRL", "左 Control"),
        ("KEY_RIGHTALT", "右 Option"),
        ("KEY_LEFTALT", "左 Option"),
        ("KEY_RIGHTMETA", "右 Command"),
        ("KEY_LEFTMETA", "左 Command"),
        ("KEY_CAPSLOCK", "Caps Lock"),
        ("KEY_F1", "F1"),
        ("KEY_F2", "F2"),
        ("KEY_F5", "F5"),
        ("KEY_F12", "F12"),
    ]
else:
    SUPPORTED_KEYS = [
        ("KEY_RIGHTCTRL", "右 Ctrl"),
        ("KEY_LEFTCTRL", "左 Ctrl"),
        ("KEY_CAPSLOCK", "Caps Lock"),
        ("KEY_F1", "F1"),
        ("KEY_F2", "F2"),
        ("KEY_F12", "F12"),
    ]

# 支持的语言列表
SUPPORTED_LANGUAGES = [
    ("auto", "自动检测"),
    ("zh", "中文"),
    ("en", "English"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("yue", "粤语"),
]

# 自启动：委托给平台后端
if IS_MACOS:
    from whisper_input.backends.autostart_macos import (  # noqa: I001
        is_autostart_enabled as _is_autostart_enabled,
        set_autostart as _set_autostart,
    )
else:
    from whisper_input.backends.autostart_linux import (  # noqa: I001
        is_autostart_enabled as _is_autostart_enabled,
        set_autostart as _set_autostart,
    )


_SETTINGS_TEMPLATE: Template | None = None


def _load_settings_template() -> Template:
    """从 assets/settings.html 加载模板（首次调用时缓存）。"""
    global _SETTINGS_TEMPLATE
    if _SETTINGS_TEMPLATE is None:
        src = (
            files("whisper_input.assets")
            .joinpath("settings.html")
            .read_text(encoding="utf-8")
        )
        _SETTINGS_TEMPLATE = Template(src)
    return _SETTINGS_TEMPLATE


def _get_settings_html() -> str:
    """生成设置页面 HTML，注入选项数据。"""
    from whisper_input.config_manager import HOTKEY_CONFIG_KEY
    from whisper_input.version import __commit__, __version__

    # 输入方式：macOS 只有剪贴板，Linux 额外支持 xdotool
    if IS_MACOS:
        input_opts = (
            '<option value="clipboard">剪贴板 (clipboard)</option>'
        )
        input_desc = "macOS 使用剪贴板 + Cmd+V 粘贴"
    else:
        input_opts = (
            '<option value="clipboard">剪贴板 (clipboard)</option>\n'
            '        <option value="xdotool">xdotool</option>'
        )
        input_desc = "clipboard 支持中文，xdotool 仅 ASCII"

    # commit 链接
    if __commit__:
        short = __commit__[:7]
        commit_html = (
            f'(<a href="https://github.com/pkulijing/'
            f'whisper-input/commit/{__commit__}"'
            f' target="_blank">{short}</a>)'
        )
    else:
        commit_html = ""

    return _load_settings_template().substitute(
        hotkey_options=json.dumps(
            SUPPORTED_KEYS, ensure_ascii=False
        ),
        language_options=json.dumps(
            SUPPORTED_LANGUAGES, ensure_ascii=False
        ),
        hotkey_key=HOTKEY_CONFIG_KEY,
        hotkey_default=(
            "KEY_RIGHTMETA" if IS_MACOS else "KEY_RIGHTCTRL"
        ),
        version=__version__,
        commit=commit_html,
        input_method_options=input_opts,
        input_method_desc=input_desc,
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
        else:
            self.send_error(404)

    def _handle_save_config(self) -> None:
        try:
            data = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "无效的 JSON"}, 400)
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
        from whisper_input.config_manager import DEFAULT_CONFIG

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
            self._send_json({"error": "无效的 JSON"}, 400)
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

    def _handle_restart(self) -> None:
        self._send_json({"ok": True})

        def do_restart():
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
    ):
        self._config_manager = config_manager
        self._on_config_changed = on_config_changed
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port: int = port

    def start(self) -> int:
        """启动服务器，返回端口号。"""
        handler = partial(_SettingsHandler)
        self._server = HTTPServer(("127.0.0.1", self._port), handler)
        # 把 config_manager 和回调挂到 server 上供 handler 访问
        self._server.config_manager = self._config_manager
        self._server.on_config_changed = self._on_config_changed

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        print(f"[settings] 设置服务已启动: http://127.0.0.1:{self._port}")
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
