"""配置管理器 - 统一管理配置文件的读取、写入和路径解析。"""

import os
import shutil
from importlib.resources import as_file, files
from pathlib import Path

import yaml

from whisper_input.backends import IS_MACOS

# 用户配置目录（按平台,installed / bundled 模式使用）
if IS_MACOS:
    CONFIG_DIR = os.path.join(
        os.path.expanduser("~/Library/Application Support"),
        "Whisper Input",
    )
else:
    CONFIG_DIR = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "whisper-input",
    )

# 默认配置（按平台）
DEFAULT_CONFIG = {
    "engine": "sensevoice",
    "hotkey_linux": "KEY_RIGHTCTRL",
    "hotkey_macos": "KEY_RIGHTMETA",  # 右 Command，MacBook 无右 Ctrl
    "audio": {
        "sample_rate": 16000,
        "channels": 1,
    },
    "sensevoice": {
        "use_itn": True,
    },
    "sound": {
        "enabled": True,
        "start_linux": "/usr/share/sounds/freedesktop/stereo/message.oga",
        "stop_linux": "/usr/share/sounds/freedesktop/stereo/complete.oga",
        "start_macos": "/System/Library/Sounds/Tink.aiff",
        "stop_macos": "/System/Library/Sounds/Pop.aiff",
    },
    "settings_port": 51230,
    "overlay": {
        "enabled": True,
    },
    "tray_status": {
        "enabled": True,
    },
    "ui": {
        "language": "zh",
    },
}

# 当前平台使用的 sound 配置键后缀
_SOUND_SUFFIX = "_macos" if IS_MACOS else "_linux"

# 当前平台使用的 hotkey 配置键名
HOTKEY_CONFIG_KEY = "hotkey_macos" if IS_MACOS else "hotkey_linux"


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典，override 覆盖 base。"""
    result = base.copy()
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _find_project_root() -> Path | None:
    """Dev 模式探测:从 whisper_input 包目录向上找,返回同时包含
    .git/ 和 pyproject.toml 的仓库根目录。找不到则为已安装/打包模式。
    """
    pkg_file = files("whisper_input") / "__init__.py"
    try:
        pkg_dir = Path(str(pkg_file)).parent
    except (TypeError, OSError):
        return None
    for candidate in [pkg_dir, *pkg_dir.parents]:
        if (
            (candidate / ".git").is_dir()
            and (candidate / "pyproject.toml").is_file()
        ):
            return candidate
    return None


def _copy_example_config(dest: str) -> None:
    """把 package 里的 config.example.yaml 拷贝到指定路径。"""
    example = files("whisper_input.assets") / "config.example.yaml"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with as_file(example) as src:
        shutil.copy2(src, dest)


class ConfigManager:
    """配置管理器，支持开发模式和安装模式。

    路径解析优先级：
    1. 命令行指定的路径
    2. 仓库根目录下的 config.yaml（开发模式,通过 .git + pyproject.toml 探测）
    3. ~/.config/whisper-input/config.yaml（安装/打包模式）
    """

    def __init__(self, config_path: str | None = None):
        self._path = self._resolve_path(config_path)
        self._config: dict = {}
        self.load()

    @staticmethod
    def _resolve_path(config_path: str | None) -> str:
        """解析配置文件路径。"""
        # 命令行显式指定
        if config_path:
            return os.path.abspath(config_path)

        # 开发模式:仓库根目录的 config.yaml
        project_root = _find_project_root()
        if project_root is not None:
            project_config = str(project_root / "config.yaml")
            if not os.path.exists(project_config):
                _copy_example_config(project_config)
            return project_config

        # 安装/打包模式:平台用户配置目录
        user_config = os.path.join(CONFIG_DIR, "config.yaml")
        if not os.path.exists(user_config):
            _copy_example_config(user_config)
        return user_config

    @property
    def path(self) -> str:
        return self._path

    @property
    def config(self) -> dict:
        return self._config

    def load(self) -> dict:
        """加载配置文件，合并默认值。"""
        if os.path.exists(self._path):
            with open(self._path, encoding="utf-8") as f:
                file_config = yaml.safe_load(f) or {}
        else:
            file_config = {}

        self._config = _deep_merge(DEFAULT_CONFIG, file_config)
        return self._config

    def save(self, config: dict | None = None) -> None:
        """保存配置到文件。"""
        if config is not None:
            self._config = config

        os.makedirs(os.path.dirname(self._path), exist_ok=True)

        # 生成带注释的 YAML
        content = self._generate_yaml(self._config)
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(content)

    def get(self, key: str, default=None):
        """获取配置值，支持点号分隔的路径如 'sensevoice.language'。"""
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def set(self, key: str, value) -> None:
        """设置配置值，支持点号分隔的路径。"""
        keys = key.split(".")
        target = self._config
        for k in keys[:-1]:
            if k not in target or not isinstance(target[k], dict):
                target[k] = {}
            target = target[k]
        target[keys[-1]] = value

    @staticmethod
    def _generate_yaml(config: dict) -> str:
        """生成带注释的 YAML 配置。"""
        lines = ["# Whisper Input - 语音输入配置", ""]

        lines.append("# STT 引擎")
        lines.append(f"engine: {config.get('engine', 'sensevoice')}")
        lines.append("")

        lines.append("# 快捷键配置（按平台分别设置）")
        lines.append(
            "# Linux 可选: KEY_RIGHTCTRL, KEY_LEFTCTRL,"
            " KEY_RIGHTALT, KEY_LEFTALT,"
        )
        lines.append(
            "#             KEY_RIGHTMETA, KEY_LEFTMETA,"
            " KEY_CAPSLOCK, KEY_F1-F12"
        )
        lines.append(
            f"hotkey_linux: "
            f"{config.get('hotkey_linux', 'KEY_RIGHTCTRL')}"
        )
        lines.append(
            "# macOS 可选: KEY_RIGHTMETA, KEY_LEFTMETA (Command),"
            " KEY_RIGHTCTRL, KEY_LEFTCTRL,"
        )
        lines.append(
            "#             KEY_RIGHTALT, KEY_LEFTALT (Option),"
            " KEY_CAPSLOCK, KEY_F1/F2/F5/F12"
        )
        lines.append(
            f"hotkey_macos: "
            f"{config.get('hotkey_macos', 'KEY_RIGHTMETA')}"
        )
        lines.append("")

        lines.append("# 音频配置")
        lines.append("audio:")
        audio = config.get("audio", {})
        lines.append(f"  sample_rate: {audio.get('sample_rate', 16000)}")
        lines.append(f"  channels: {audio.get('channels', 1)}")
        lines.append("")

        lines.append("# SenseVoice 本地模型配置")
        lines.append(
            "# 首次启动通过 modelscope.snapshot_download 从 ModelScope 下载 ~231MB,"
        )
        lines.append(
            "# 缓存在 ~/.cache/modelscope/hub/,国内 CDN 直连,之后永久离线。"
        )
        lines.append("sensevoice:")
        sv = config.get("sensevoice", {})
        use_itn = "true" if sv.get("use_itn", True) else "false"
        lines.append(
            f"  use_itn: {use_itn}  # 反向文本规范化(数字、日期等)"
        )
        lines.append("")

        lines.append("# 提示音（按平台分别设置路径）")
        lines.append("sound:")
        sound = config.get("sound", {})
        enabled = "true" if sound.get("enabled", True) else "false"
        lines.append(f"  enabled: {enabled}")
        lines.append(
            f"  start_linux: {sound.get('start_linux', '')}"
        )
        lines.append(
            f"  stop_linux: {sound.get('stop_linux', '')}"
        )
        lines.append(
            f"  start_macos: {sound.get('start_macos', '')}"
        )
        lines.append(
            f"  stop_macos: {sound.get('stop_macos', '')}"
        )
        lines.append("")

        lines.append("# 设置页面端口")
        lines.append(
            f"settings_port: {config.get('settings_port', 51230)}"
        )
        lines.append("")

        lines.append("# 录音浮窗")
        lines.append("overlay:")
        overlay = config.get("overlay", {})
        ov_enabled = (
            "true" if overlay.get("enabled", True) else "false"
        )
        lines.append(f"  enabled: {ov_enabled}")
        lines.append("")

        lines.append("# 托盘图标状态")
        lines.append("tray_status:")
        tray_st = config.get("tray_status", {})
        ts_enabled = (
            "true" if tray_st.get("enabled", True) else "false"
        )
        lines.append(f"  enabled: {ts_enabled}")
        lines.append("")

        lines.append("# 界面语言 (zh / en / fr)")
        lines.append("ui:")
        ui = config.get("ui", {})
        lines.append(f"  language: {ui.get('language', 'zh')}")
        lines.append("")

        return "\n".join(lines)
