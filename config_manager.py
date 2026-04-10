"""配置管理器 - 统一管理配置文件的读取、写入和路径解析。"""

import os
import shutil

import yaml

from backends import IS_MACOS

# 配置目录（按平台）
if IS_MACOS:
    CONFIG_DIR = os.path.join(
        os.path.expanduser("~/Library/Application Support"),
        "Whisper Input",
    )
    INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
else:
    CONFIG_DIR = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "whisper-input",
    )
    INSTALL_DIR = "/opt/whisper-input"

# 默认配置（按平台）
if IS_MACOS:
    DEFAULT_CONFIG = {
        "engine": "sensevoice",
        "hotkey": "KEY_RIGHTCTRL",
        "audio": {
            "sample_rate": 16000,
            "channels": 1,
        },
        "sensevoice": {
            "model": "iic/SenseVoiceSmall",
            "device_priority": ["cuda", "mps", "cpu"],
            "language": "auto",
        },
        "input_method": "clipboard",
        "sound": {
            "enabled": True,
            "start": "/System/Library/Sounds/Tink.aiff",
            "stop": "/System/Library/Sounds/Pop.aiff",
        },
        "settings_port": 51230,
    }
else:
    DEFAULT_CONFIG = {
        "engine": "sensevoice",
        "hotkey": "KEY_RIGHTCTRL",
        "audio": {
            "sample_rate": 16000,
            "channels": 1,
        },
        "sensevoice": {
            "model": "iic/SenseVoiceSmall",
            "device_priority": ["cuda", "mps", "cpu"],
            "language": "auto",
        },
        "input_method": "clipboard",
        "sound": {
            "enabled": True,
            "start": "/usr/share/sounds/freedesktop/stereo/message.oga",
            "stop": "/usr/share/sounds/freedesktop/stereo/complete.oga",
        },
        "settings_port": 51230,
    }


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


class ConfigManager:
    """配置管理器，支持开发模式和安装模式。

    路径解析优先级：
    1. 命令行指定的路径
    2. 项目目录下的 config.yaml（开发模式）
    3. ~/.config/whisper-input/config.yaml（安装模式）
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

        # 开发模式：项目目录下的 config.yaml
        project_config = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.yaml"
        )
        if os.path.exists(project_config):
            return project_config

        # 安装模式：XDG 配置目录
        xdg_config = os.path.join(CONFIG_DIR, "config.yaml")
        if os.path.exists(xdg_config):
            return xdg_config

        # XDG 配置不存在，从安装目录拷贝默认配置
        install_config = os.path.join(INSTALL_DIR, "config.yaml")
        if os.path.exists(install_config):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            shutil.copy2(install_config, xdg_config)
            return xdg_config

        # 都没有，使用 XDG 路径（将写入默认配置）
        return xdg_config

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

        lines.append("# 快捷键配置")
        if IS_MACOS:
            lines.append(
                "# 可选值: KEY_RIGHTCTRL, KEY_LEFTCTRL,"
                " KEY_RIGHTALT, KEY_LEFTALT,"
            )
            lines.append(
                "#         KEY_RIGHTMETA, KEY_LEFTMETA"
                " (Command), KEY_CAPSLOCK, KEY_F1/F2/F5/F12"
            )
            default_hotkey = "KEY_RIGHTCTRL"
        else:
            lines.append(
                "# 可选值: KEY_RIGHTCTRL, KEY_LEFTCTRL,"
                " KEY_RIGHTALT, KEY_LEFTALT,"
            )
            lines.append(
                "#         KEY_RIGHTMETA, KEY_LEFTMETA"
                " (Meta = Win/Super键)"
            )
            default_hotkey = "KEY_RIGHTCTRL"
        lines.append(f"hotkey: {config.get('hotkey', default_hotkey)}")
        lines.append("")

        lines.append("# 音频配置")
        lines.append("audio:")
        audio = config.get("audio", {})
        lines.append(f"  sample_rate: {audio.get('sample_rate', 16000)}")
        lines.append(f"  channels: {audio.get('channels', 1)}")
        lines.append("")

        lines.append("# SenseVoice 本地模型配置")
        lines.append("sensevoice:")
        sv = config.get("sensevoice", {})
        lines.append(f"  model: {sv.get('model', 'iic/SenseVoiceSmall')}")
        priority = sv.get("device_priority", ["cuda", "mps", "cpu"])
        priority_str = ", ".join(priority)
        lines.append(
            f"  device_priority: [{priority_str}]"
            "   # 按顺序尝试，选第一个可用的"
        )
        lines.append(
            f"  language: {sv.get('language', 'auto')}"
            "  # auto, zh, en, ja, ko, yue"
        )
        lines.append("")

        if IS_MACOS:
            lines.append(
                '# 输入方式: "clipboard" (剪贴板 + Cmd+V)'
            )
        else:
            lines.append(
                '# 输入方式: "clipboard" (推荐,支持中文)'
                ' 或 "xdotool" (仅ASCII)'
            )
        lines.append(f"input_method: {config.get('input_method', 'clipboard')}")
        lines.append("")

        lines.append("# 提示音")
        lines.append("sound:")
        sound = config.get("sound", {})
        enabled = "true" if sound.get("enabled", True) else "false"
        lines.append(f"  enabled: {enabled}")
        lines.append(f"  start: {sound.get('start', '')}")
        lines.append(f"  stop: {sound.get('stop', '')}")
        lines.append("")

        lines.append("# 设置页面端口")
        lines.append(
            f"settings_port: {config.get('settings_port', 51230)}"
        )
        lines.append("")

        return "\n".join(lines)
