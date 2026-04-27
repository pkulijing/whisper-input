"""配置管理器 - 统一管理配置文件的读取、写入和路径解析。"""

import copy
import os
import shutil
from importlib.resources import as_file, files
from pathlib import Path

import yaml

from daobidao.backends import IS_MACOS

# 用户配置目录（按平台,installed / bundled 模式使用）
if IS_MACOS:
    CONFIG_DIR = os.path.join(
        os.path.expanduser("~/Library/Application Support"),
        "Daobidao",
    )
else:
    CONFIG_DIR = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "daobidao",
    )

# 默认配置（按平台）
DEFAULT_CONFIG = {
    "engine": "qwen3",
    "hotkey_linux": "KEY_RIGHTCTRL",
    "hotkey_macos": "KEY_RIGHTMETA",  # 右 Command，MacBook 无右 Ctrl
    "audio": {
        "sample_rate": 16000,
        "channels": 1,
    },
    "qwen3": {
        "variant": "0.6B",  # "0.6B" (快) 或 "1.7B" (更准)
        "streaming_mode": True,  # 28 轮:按住说话时边说边出字
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
    "update": {
        "check_enabled": True,
    },
    "log_level": "INFO",
}

# 当前平台使用的 sound 配置键后缀
_SOUND_SUFFIX = "_macos" if IS_MACOS else "_linux"

# 当前平台使用的 hotkey 配置键名
HOTKEY_CONFIG_KEY = "hotkey_macos" if IS_MACOS else "hotkey_linux"


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典，override 覆盖 base。

    Deep-copies `base` so嵌套 dict 即使没被 override 覆盖,也是独立副本;
    后续 ConfigManager.set() 等就地修改不会污染 DEFAULT_CONFIG。
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _find_project_root() -> Path | None:
    """Dev 模式探测:从 daobidao 包目录向上找,返回同时包含
    .git/ 和 pyproject.toml 的仓库根目录。找不到则为已安装/打包模式。
    """
    pkg_file = files("daobidao") / "__init__.py"
    try:
        pkg_dir = Path(str(pkg_file)).parent
    except (TypeError, OSError):
        return None
    for candidate in [pkg_dir, *pkg_dir.parents]:
        if (candidate / ".git").is_dir() and (
            candidate / "pyproject.toml"
        ).is_file():
            return candidate
    return None


def _migrate_legacy(cfg: dict) -> tuple[dict, bool]:
    """把 ≤ 0.7.3 的 SenseVoice 配置升级到 Qwen3-ASR。

    老配置字段(engine: sensevoice, sensevoice: {...})整个被替换为
    engine: qwen3 + qwen3.variant: 0.6B。返回 (new_cfg, changed_bool),
    changed 为 True 时调用方会把结果写回磁盘。
    """
    if not isinstance(cfg, dict):
        return cfg, False

    out = dict(cfg)
    changed = False

    engine = out.get("engine")
    if engine == "sensevoice":
        out["engine"] = "qwen3"
        changed = True

    if "sensevoice" in out:
        out.pop("sensevoice", None)
        changed = True

    if changed and "qwen3" not in out:
        out["qwen3"] = {"variant": "0.6B"}

    return out, changed


def _copy_example_config(dest: str) -> None:
    """把 package 里的 config.example.yaml 拷贝到指定路径。"""
    example = files("daobidao.assets") / "config.example.yaml"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with as_file(example) as src:
        shutil.copy2(src, dest)


class ConfigManager:
    """配置管理器，支持开发模式和安装模式。

    路径解析优先级：
    1. 命令行指定的路径
    2. 仓库根目录下的 config.yaml（开发模式,通过 .git + pyproject.toml 探测）
    3. ~/.config/daobidao/config.yaml（安装/打包模式）
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

        migrated, changed = _migrate_legacy(file_config)
        self._config = _deep_merge(DEFAULT_CONFIG, migrated)

        # 自动持久化迁移结果,这样老用户无感知升级到 Qwen3-ASR
        if changed and os.path.exists(self._path):
            self.save()

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
        """获取配置值，支持点号分隔的路径如 'qwen3.variant'。"""
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
        lines = ["# 叨逼叨 (daobidao) - 语音输入配置", ""]

        lines.append("# STT 引擎")
        lines.append(f"engine: {config.get('engine', 'qwen3')}")
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
            f"hotkey_linux: {config.get('hotkey_linux', 'KEY_RIGHTCTRL')}"
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
            f"hotkey_macos: {config.get('hotkey_macos', 'KEY_RIGHTMETA')}"
        )
        lines.append("")

        lines.append("# 音频配置")
        lines.append("audio:")
        audio = config.get("audio", {})
        lines.append(f"  sample_rate: {audio.get('sample_rate', 16000)}")
        lines.append(f"  channels: {audio.get('channels', 1)}")
        lines.append("")

        lines.append("# Qwen3-ASR 本地模型配置")
        lines.append(
            "# 首次启动通过 modelscope.snapshot_download 从 ModelScope 下载,"
        )
        lines.append(
            "# 缓存在 ~/.cache/modelscope/hub/,国内 CDN 直连,之后永久离线。"
        )
        lines.append("# variant: 0.6B (~990MB 下载 / ~1.5GB 内存,默认) 或")
        lines.append("#          1.7B (~2.4GB 下载 / ~3GB 内存,识别更准)")
        lines.append("qwen3:")
        qw = config.get("qwen3", {})
        variant = qw.get("variant", "0.6B")
        lines.append(f'  variant: "{variant}"')
        streaming = "true" if qw.get("streaming_mode", True) else "false"
        lines.append("  # 流式识别:按住热键说话时边说边出字(28 轮)")
        lines.append(f"  streaming_mode: {streaming}")
        lines.append("")

        lines.append("# 提示音（按平台分别设置路径）")
        lines.append("sound:")
        sound = config.get("sound", {})
        enabled = "true" if sound.get("enabled", True) else "false"
        lines.append(f"  enabled: {enabled}")
        lines.append(f"  start_linux: {sound.get('start_linux', '')}")
        lines.append(f"  stop_linux: {sound.get('stop_linux', '')}")
        lines.append(f"  start_macos: {sound.get('start_macos', '')}")
        lines.append(f"  stop_macos: {sound.get('stop_macos', '')}")
        lines.append("")

        lines.append("# 设置页面端口")
        lines.append(f"settings_port: {config.get('settings_port', 51230)}")
        lines.append("")

        lines.append("# 录音浮窗")
        lines.append("overlay:")
        overlay = config.get("overlay", {})
        ov_enabled = "true" if overlay.get("enabled", True) else "false"
        lines.append(f"  enabled: {ov_enabled}")
        lines.append("")

        lines.append("# 托盘图标状态")
        lines.append("tray_status:")
        tray_st = config.get("tray_status", {})
        ts_enabled = "true" if tray_st.get("enabled", True) else "false"
        lines.append(f"  enabled: {ts_enabled}")
        lines.append("")

        lines.append("# 界面语言 (zh / en / fr)")
        lines.append("ui:")
        ui = config.get("ui", {})
        lines.append(f"  language: {ui.get('language', 'zh')}")
        lines.append("")

        return "\n".join(lines)
