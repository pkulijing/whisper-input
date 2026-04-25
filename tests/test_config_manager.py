"""测试 ConfigManager 的纯逻辑路径。

针对 src/whisper_input/config_manager.py。

所有测试都用 pytest 的 tmp_path fixture,绝不允许触碰真实的
~/.config/whisper-input/ 或 ~/Library/Application Support/Whisper Input/。
"""

import os
from pathlib import Path

import pytest

from whisper_input import config_manager
from whisper_input.config_manager import (
    DEFAULT_CONFIG,
    ConfigManager,
    _deep_merge,
    _migrate_legacy,
)

# --- _deep_merge ---


def test_deep_merge_overrides_top_level():
    base = {"a": 1, "b": 2}
    override = {"b": 99}
    assert _deep_merge(base, override) == {"a": 1, "b": 99}


def test_deep_merge_recurses_into_nested_dicts():
    base = {"audio": {"sample_rate": 16000, "channels": 1}}
    override = {"audio": {"channels": 2}}
    merged = _deep_merge(base, override)
    # 嵌套 dict 应该深合并,sample_rate 保留,channels 被覆盖
    assert merged == {"audio": {"sample_rate": 16000, "channels": 2}}


def test_deep_merge_replaces_dict_with_non_dict():
    base = {"x": {"nested": True}}
    override = {"x": "scalar"}
    assert _deep_merge(base, override) == {"x": "scalar"}


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"b": 1}}
    override = {"a": {"c": 2}}
    _deep_merge(base, override)
    assert base == {"a": {"b": 1}}
    assert override == {"a": {"c": 2}}


# --- ConfigManager: load + merge ---


def test_load_missing_file_returns_defaults(tmp_path):
    """配置文件不存在时,load 后内存里是 DEFAULT_CONFIG 的拷贝。"""
    cfg_path = tmp_path / "config.yaml"
    mgr = ConfigManager(config_path=str(cfg_path))
    assert mgr.config["engine"] == DEFAULT_CONFIG["engine"]
    assert mgr.config["audio"] == DEFAULT_CONFIG["audio"]


def test_load_existing_file_merges_with_defaults(tmp_path):
    """文件里只写了部分 key,其他 key 用默认值。"""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        'engine: qwen3\n'
        'qwen3:\n'
        '  variant: "1.7B"\n',
        encoding="utf-8",
    )
    mgr = ConfigManager(config_path=str(cfg_path))
    # 文件里覆盖的 key
    assert mgr.config["qwen3"]["variant"] == "1.7B"
    # 文件里没写的 key 走默认
    assert mgr.config["audio"] == DEFAULT_CONFIG["audio"]


# --- get / set 点号路径 ---


def test_get_with_dot_path(tmp_path):
    mgr = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    assert mgr.get("qwen3.variant") == "0.6B"
    assert mgr.get("audio.sample_rate") == 16000


def test_default_streaming_mode_is_true(tmp_path):
    """28 轮默认开启流式识别。老配置加载时 _deep_merge 自动补默认值。"""
    mgr = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    assert mgr.get("qwen3.streaming_mode") is True


def test_get_missing_key_returns_default(tmp_path):
    mgr = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    assert mgr.get("nonexistent.key", "fallback") == "fallback"
    assert mgr.get("qwen3.nonexistent") is None


def test_set_with_dot_path(tmp_path):
    mgr = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    mgr.set("qwen3.variant", "1.7B")
    assert mgr.get("qwen3.variant") == "1.7B"


def test_set_creates_intermediate_dicts(tmp_path):
    mgr = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    mgr.set("brand.new.key", "value")
    assert mgr.get("brand.new.key") == "value"


# --- save / load 往返 ---


def test_save_then_reload_preserves_changes(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    mgr = ConfigManager(config_path=str(cfg_path))
    mgr.set("qwen3.variant", "1.7B")
    mgr.set("settings_port", 51999)
    mgr.save()

    # 用新实例从磁盘读回,验证持久化
    mgr2 = ConfigManager(config_path=str(cfg_path))
    assert mgr2.get("qwen3.variant") == "1.7B"
    assert mgr2.get("settings_port") == 51999


def test_generated_yaml_contains_key_sections(tmp_path):
    import copy

    mgr = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    # deepcopy guards against other tests mutating DEFAULT_CONFIG via set()
    # (pre-existing shallow-copy behavior in _deep_merge — unrelated to
    # round 26, but makes test ordering fragile otherwise)
    yaml_text = mgr._generate_yaml(copy.deepcopy(DEFAULT_CONFIG))
    # 关键 section 都在
    assert "engine: qwen3" in yaml_text
    assert "hotkey_linux:" in yaml_text
    assert "hotkey_macos:" in yaml_text
    assert "audio:" in yaml_text
    assert "qwen3:" in yaml_text
    assert 'variant: "0.6B"' in yaml_text
    assert "streaming_mode: true" in yaml_text
    assert "sound:" in yaml_text
    assert "overlay:" in yaml_text
    assert "tray_status:" in yaml_text
    assert "settings_port:" in yaml_text


# --- _migrate_legacy: sensevoice → qwen3 ---


def test_migrate_legacy_sensevoice_engine_becomes_qwen3():
    legacy = {"engine": "sensevoice", "sensevoice": {"use_itn": False}}
    migrated, changed = _migrate_legacy(legacy)
    assert changed is True
    assert migrated["engine"] == "qwen3"
    assert "sensevoice" not in migrated
    assert migrated["qwen3"] == {"variant": "0.6B"}


def test_migrate_legacy_modern_config_unchanged():
    modern = {"engine": "qwen3", "qwen3": {"variant": "1.7B"}}
    migrated, changed = _migrate_legacy(modern)
    assert changed is False
    assert migrated == modern


def test_migrate_legacy_empty_input():
    migrated, changed = _migrate_legacy({})
    assert changed is False
    assert migrated == {}


def test_migrate_legacy_preserves_user_qwen3_if_already_present():
    """罕见边界:配置同时含 sensevoice 和 qwen3。迁移应保留用户的 qwen3 块。"""
    legacy = {
        "engine": "sensevoice",
        "sensevoice": {"use_itn": True},
        "qwen3": {"variant": "1.7B"},
    }
    migrated, changed = _migrate_legacy(legacy)
    assert changed is True
    assert migrated["engine"] == "qwen3"
    assert migrated["qwen3"] == {"variant": "1.7B"}
    assert "sensevoice" not in migrated


def test_migrate_legacy_non_dict_input_safe():
    migrated, changed = _migrate_legacy(None)  # type: ignore[arg-type]
    assert changed is False
    assert migrated is None


def test_load_auto_migrates_and_persists(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "engine: sensevoice\nsensevoice:\n  use_itn: true\n",
        encoding="utf-8",
    )
    mgr = ConfigManager(config_path=str(cfg_path))
    # In-memory config is migrated
    assert mgr.get("engine") == "qwen3"
    assert mgr.get("qwen3.variant") == "0.6B"
    assert mgr.get("sensevoice") is None

    # And the migration has been persisted to disk
    on_disk = cfg_path.read_text(encoding="utf-8")
    assert "engine: qwen3" in on_disk
    assert "sensevoice" not in on_disk


# --- _resolve_path 的 dev / installed 模式 ---


def test_resolve_path_explicit_takes_priority(tmp_path):
    """显式传 config_path 时,绕过 dev / installed 探测。"""
    explicit = tmp_path / "custom.yaml"
    mgr = ConfigManager(config_path=str(explicit))
    assert mgr.path == os.path.abspath(str(explicit))


def test_resolve_path_dev_mode_uses_project_root(
    tmp_path, monkeypatch
):
    """dev 模式下应该返回 <project_root>/config.yaml,
    并且首次调用会从 package data 拷贝 example。
    """
    fake_root = tmp_path / "fake-repo"
    fake_root.mkdir()

    monkeypatch.setattr(
        config_manager,
        "_find_project_root",
        lambda: fake_root,
    )
    mgr = ConfigManager()
    assert mgr.path == str(fake_root / "config.yaml")
    # 例子被拷贝过来了
    assert (fake_root / "config.yaml").is_file()


def test_resolve_path_installed_mode_uses_config_dir(
    tmp_path, monkeypatch
):
    """非 dev 模式下,落在 CONFIG_DIR(被 monkeypatch 到 tmp_path)。"""
    fake_config_dir = tmp_path / "user-config"
    monkeypatch.setattr(
        config_manager,
        "_find_project_root",
        lambda: None,
    )
    monkeypatch.setattr(
        config_manager, "CONFIG_DIR", str(fake_config_dir)
    )
    mgr = ConfigManager()
    assert mgr.path == str(fake_config_dir / "config.yaml")
    assert (fake_config_dir / "config.yaml").is_file()


def test_save_creates_parent_directory(tmp_path):
    """save 时父目录不存在应该自动创建。"""
    nested = tmp_path / "deeply" / "nested" / "config.yaml"
    mgr = ConfigManager(config_path=str(nested))
    mgr.set("engine", "qwen3")
    mgr.save()
    assert nested.is_file()


@pytest.fixture
def isolated_config(tmp_path: Path) -> ConfigManager:
    """用 tmp_path 隔离的 ConfigManager,供其他测试模块复用。"""
    return ConfigManager(config_path=str(tmp_path / "config.yaml"))
