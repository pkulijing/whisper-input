"""设置页面 Web 服务 - 提供浏览器设置界面和 REST API。"""

import json
import os
import signal
import sys
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer

from backends import IS_MACOS
from config_manager import ConfigManager

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
    from backends.autostart_macos import (  # noqa: I001
        is_autostart_enabled as _is_autostart_enabled,
        set_autostart as _set_autostart,
    )
else:
    from backends.autostart_linux import (  # noqa: I001
        is_autostart_enabled as _is_autostart_enabled,
        set_autostart as _set_autostart,
    )


SETTINGS_HTML = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Whisper Input 设置</title>
<style>
  :root {
    --bg: #fafafa;
    --card: #ffffff;
    --border: #e0e0e0;
    --primary: #e95420;
    --primary-hover: #c7431a;
    --text: #333333;
    --text-secondary: #666666;
    --success: #4caf50;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Ubuntu', 'Noto Sans CJK SC', sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 0;
  }
  .header {
    background: var(--primary);
    color: white;
    padding: 24px 32px;
  }
  .header h1 { font-size: 22px; font-weight: 500; }
  .header p { font-size: 13px; opacity: 0.85; margin-top: 4px; }
  .container {
    max-width: 600px;
    margin: 24px auto;
    padding: 0 16px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0;
    margin-bottom: 16px;
  }
  .card-title {
    font-size: 13px;
    font-weight: 500;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 12px 20px 8px;
  }
  .setting-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 20px;
    border-top: 1px solid var(--border);
  }
  .setting-row:first-child { border-top: none; }
  .setting-label {
    font-size: 15px;
    flex-shrink: 0;
    margin-right: 16px;
  }
  .setting-desc {
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 2px;
  }
  select {
    padding: 6px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 14px;
    background: white;
    min-width: 180px;
    cursor: pointer;
  }
  select:focus {
    outline: none;
    border-color: var(--primary);
  }
  /* Toggle switch */
  .switch {
    position: relative;
    width: 44px;
    height: 24px;
    flex-shrink: 0;
  }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute;
    cursor: pointer;
    top: 0; left: 0; right: 0; bottom: 0;
    background: #ccc;
    border-radius: 24px;
    transition: 0.2s;
  }
  .slider:before {
    content: "";
    position: absolute;
    height: 18px; width: 18px;
    left: 3px; bottom: 3px;
    background: white;
    border-radius: 50%;
    transition: 0.2s;
  }
  .switch input:checked + .slider { background: var(--primary); }
  .switch input:checked + .slider:before { transform: translateX(20px); }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 12px;
    margin-top: 8px;
    margin-bottom: 24px;
  }
  .btn {
    padding: 10px 24px;
    border: none;
    border-radius: 6px;
    font-size: 14px;
    cursor: pointer;
    transition: 0.15s;
  }
  .btn-primary {
    background: var(--primary);
    color: white;
  }
  .btn-primary:hover { background: var(--primary-hover); }
  .btn-secondary {
    background: white;
    color: var(--text);
    border: 1px solid var(--border);
  }
  .btn-secondary:hover { background: #f5f5f5; }
  .btn-danger {
    background: white;
    color: #d32f2f;
    border: 1px solid var(--border);
  }
  .btn-danger:hover { background: #fef2f2; }
  .notice {
    font-size: 13px;
    color: var(--text-secondary);
    padding: 12px 20px;
    border-top: 1px solid var(--border);
    background: #f9f9f9;
    border-radius: 0 0 8px 8px;
  }
  .footer {
    text-align: center;
    padding: 8px 16px 24px;
    font-size: 13px;
    color: var(--text-secondary);
  }
  .footer a {
    color: var(--text-secondary);
    text-decoration: none;
  }
  .footer a:hover {
    color: var(--primary);
    text-decoration: underline;
  }
  .toast {
    position: fixed;
    bottom: 24px;
    left: 50%;
    transform: translateX(-50%) translateY(80px);
    background: #333;
    color: white;
    padding: 12px 24px;
    border-radius: 8px;
    font-size: 14px;
    opacity: 0;
    transition: all 0.3s;
    z-index: 1000;
  }
  .toast.show {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
  }
</style>
</head>
<body>
<div class="header">
  <h1>Whisper Input 设置</h1>
  <p>语音输入工具 - 按住快捷键说话，松开自动输入</p>
</div>
<div class="container">
  <div class="card">
    <div class="card-title">基本设置</div>
    <div class="setting-row">
      <div>
        <div class="setting-label">快捷键</div>
        <div class="setting-desc">按住此键开始录音</div>
      </div>
      <select id="hotkey"></select>
    </div>
    <div class="setting-row">
      <div>
        <div class="setting-label">识别语言</div>
        <div class="setting-desc">语音识别的目标语言</div>
      </div>
      <select id="language"></select>
    </div>
    <div class="setting-row">
      <div>
        <div class="setting-label">输入方式</div>
        <div class="setting-desc">INPUT_METHOD_DESC_PLACEHOLDER</div>
      </div>
      <select id="input_method">
        INPUT_METHOD_OPTIONS_PLACEHOLDER
      </select>
    </div>
  </div>

  <div class="card">
    <div class="card-title">高级设置</div>
    <div class="setting-row">
      <div>
        <div class="setting-label">提示音</div>
        <div class="setting-desc">录音开始和结束时播放提示音</div>
      </div>
      <label class="switch">
        <input type="checkbox" id="sound_enabled">
        <span class="slider"></span>
      </label>
    </div>
    <div class="setting-row">
      <div>
        <div class="setting-label">录音浮窗</div>
        <div class="setting-desc">录音时在屏幕上显示状态浮窗</div>
      </div>
      <label class="switch">
        <input type="checkbox" id="overlay_enabled">
        <span class="slider"></span>
      </label>
    </div>
    <div class="setting-row">
      <div>
        <div class="setting-label">托盘图标状态</div>
        <div class="setting-desc">托盘图标颜色随录音/识别状态变化</div>
      </div>
      <label class="switch">
        <input type="checkbox" id="tray_status_enabled">
        <span class="slider"></span>
      </label>
    </div>
    <div class="setting-row">
      <div>
        <div class="setting-label">设置页面端口</div>
        <div class="setting-desc">浏览器访问设置页面的端口号</div>
      </div>
      <input type="number" id="settings_port" min="1024" max="65535"
        style="width:100px; padding:6px 12px; border:1px solid var(--border);
        border-radius:6px; font-size:14px;">
    </div>
    <div class="notice">
      修改快捷键或端口后需要重启程序才能生效
    </div>
  </div>

  <div class="card">
    <div class="card-title">系统</div>
    <div class="setting-row">
      <div>
        <div class="setting-label">开机自动启动</div>
        <div class="setting-desc">登录后自动运行 Whisper Input</div>
      </div>
      <label class="switch">
        <input type="checkbox" id="autostart">
        <span class="slider"></span>
      </label>
    </div>
  </div>

  <div class="actions">
    <button class="btn btn-danger" onclick="quitApp()">退出程序</button>
    <button class="btn btn-secondary" onclick="restartApp()">重启程序</button>
    <div style="flex:1"></div>
    <button class="btn btn-secondary" onclick="resetConfig()">
      恢复默认设置
    </button>
  </div>
  <div class="footer">
    v<!--VERSION_PLACEHOLDER--> <!--COMMIT_PLACEHOLDER--> &middot;
    <a href="https://github.com/pkulijing/whisper-input" target="_blank">
      GitHub
    </a>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const HOTKEYS = HOTKEY_OPTIONS_PLACEHOLDER;
const LANGUAGES = LANGUAGE_OPTIONS_PLACEHOLDER;
const HOTKEY_KEY = 'HOTKEY_KEY_PLACEHOLDER';
const HOTKEY_DEFAULT = 'HOTKEY_DEFAULT_PLACEHOLDER';

function populateSelect(id, options, selectedValue) {
  const sel = document.getElementById(id);
  sel.innerHTML = '';
  options.forEach(([value, label]) => {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    if (value === selectedValue) opt.selected = true;
    sel.appendChild(opt);
  });
}

function showToast(msg, duration) {
  duration = duration || 2000;
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), duration);
}

async function loadConfig() {
  try {
    const [cfgRes, autoRes] = await Promise.all([
      fetch('/api/config'),
      fetch('/api/autostart')
    ]);
    const config = await cfgRes.json();
    const autostart = await autoRes.json();

    populateSelect('hotkey', HOTKEYS, config[HOTKEY_KEY] || HOTKEY_DEFAULT);
    populateSelect('language', LANGUAGES,
      (config.sensevoice && config.sensevoice.language) || 'auto');
    document.getElementById('input_method').value =
      config.input_method || 'clipboard';
    document.getElementById('sound_enabled').checked =
      config.sound ? config.sound.enabled !== false : true;
    document.getElementById('overlay_enabled').checked =
      config.overlay ? config.overlay.enabled !== false : true;
    document.getElementById('tray_status_enabled').checked =
      config.tray_status ? config.tray_status.enabled !== false : true;
    document.getElementById('settings_port').value =
      config.settings_port || 51230;
    document.getElementById('autostart').checked =
      autostart.enabled || false;
  } catch (e) {
    showToast('加载配置失败: ' + e.message, 3000);
  }
}

const RESTART_KEYS = [HOTKEY_KEY, 'settings_port'];

async function saveSetting(key, value) {
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({[key]: value})
    });
    if (res.ok) {
      showToast('已保存', 1200);
      if (RESTART_KEYS.includes(key)) {
        setTimeout(() => {
          if (confirm('此设置需要重启程序才能生效，现在重启吗？')) {
            restartApp();
          }
        }, 300);
      }
    } else {
      showToast('保存失败', 3000);
    }
  } catch (e) {
    showToast('保存失败: ' + e.message, 3000);
  }
}

async function saveAutostart(enabled) {
  try {
    await fetch('/api/autostart', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: enabled})
    });
    showToast('已保存', 1200);
  } catch (e) {
    showToast('保存失败: ' + e.message, 3000);
  }
}

async function resetConfig() {
  if (!confirm('确定要恢复所有设置为默认值吗？')) return;
  try {
    const res = await fetch('/api/config/reset', {method: 'POST'});
    if (res.ok) {
      showToast('已恢复默认设置');
      loadConfig();
    } else {
      showToast('重置失败', 3000);
    }
  } catch (e) {
    showToast('重置失败: ' + e.message, 3000);
  }
}

async function quitApp() {
  if (!confirm('确定要退出 Whisper Input 吗？')) return;
  try {
    await fetch('/api/quit', {method: 'POST'});
  } catch (e) {
    // 连接断开是正常的（程序已退出）
  }
  showToast('程序已退出');
}

async function restartApp() {
  showToast('正在重启...');
  try {
    await fetch('/api/restart', {method: 'POST'});
  } catch (e) {
    // 连接断开是正常的（程序正在重启）
  }
  // 等待新进程启动后刷新页面
  setTimeout(() => location.reload(), 3000);
}

// 绑定控件变化事件，自动保存
function bindAutoSave() {
  document.getElementById('hotkey').addEventListener('change', function() {
    saveSetting(HOTKEY_KEY, this.value);
  });
  document.getElementById('language').addEventListener('change', function() {
    saveSetting('sensevoice.language', this.value);
  });
  document.getElementById('input_method').addEventListener('change', function() {
    saveSetting('input_method', this.value);
  });
  document.getElementById('sound_enabled').addEventListener('change', function() {
    saveSetting('sound.enabled', this.checked);
  });
  document.getElementById('overlay_enabled').addEventListener('change', function() {
    saveSetting('overlay.enabled', this.checked);
  });
  document.getElementById('tray_status_enabled').addEventListener('change', function() {
    saveSetting('tray_status.enabled', this.checked);
  });
  document.getElementById('settings_port').addEventListener('change', function() {
    const port = parseInt(this.value);
    if (port >= 1024 && port <= 65535) {
      saveSetting('settings_port', port);
    } else {
      showToast('端口号需在 1024-65535 之间', 3000);
    }
  });
  document.getElementById('autostart').addEventListener('change', function() {
    saveAutostart(this.checked);
  });
}

// 页面加载时获取配置并绑定事件
loadConfig().then(bindAutoSave);
</script>
</body>
</html>
"""


def _get_settings_html() -> str:
    """生成设置页面 HTML，注入选项数据。"""
    from config_manager import HOTKEY_CONFIG_KEY

    hotkey_json = json.dumps(SUPPORTED_KEYS, ensure_ascii=False)
    language_json = json.dumps(SUPPORTED_LANGUAGES, ensure_ascii=False)
    html = SETTINGS_HTML.replace("HOTKEY_OPTIONS_PLACEHOLDER", hotkey_json)
    html = html.replace("LANGUAGE_OPTIONS_PLACEHOLDER", language_json)

    # 热键配置键名和默认值
    hotkey_default = (
        "KEY_RIGHTMETA" if IS_MACOS else "KEY_RIGHTCTRL"
    )
    html = html.replace("HOTKEY_KEY_PLACEHOLDER", HOTKEY_CONFIG_KEY)
    html = html.replace("HOTKEY_DEFAULT_PLACEHOLDER", hotkey_default)

    # 版本号 + commit
    from version import __commit__, __version__

    html = html.replace("<!--VERSION_PLACEHOLDER-->", __version__)
    if __commit__:
        short = __commit__[:7]
        commit_html = (
            f'(<a href="https://github.com/pkulijing/whisper-input/commit/'
            f'{__commit__}" target="_blank">{short}</a>)'
        )
    else:
        commit_html = ""
    html = html.replace("<!--COMMIT_PLACEHOLDER-->", commit_html)

    # 输入方式：macOS 只有剪贴板，Linux 额外支持 xdotool
    if IS_MACOS:
        input_opts = '<option value="clipboard">剪贴板 (clipboard)</option>'
        input_desc = "macOS 使用剪贴板 + Cmd+V 粘贴"
    else:
        input_opts = (
            '<option value="clipboard">剪贴板 (clipboard)</option>\n'
            '        <option value="xdotool">xdotool</option>'
        )
        input_desc = "clipboard 支持中文，xdotool 仅 ASCII"
    html = html.replace("INPUT_METHOD_OPTIONS_PLACEHOLDER", input_opts)
    html = html.replace("INPUT_METHOD_DESC_PLACEHOLDER", input_desc)

    return html


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
        from config_manager import DEFAULT_CONFIG

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
