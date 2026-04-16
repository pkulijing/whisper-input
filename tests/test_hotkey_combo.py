"""测试热键 300ms combo 检测状态机。

针对 src/whisper_input/backends/hotkey_macos.py 和 hotkey_linux.py。

两个文件的 HotkeyListener 类几乎对称:macOS 用 pynput Key 对象,Linux 用
evdev int 键码,但状态机逻辑(_on_hotkey_press / _on_hotkey_release /
_on_combo_detected / _on_delayed_press)完全一致。

参数化跑同一组用例覆盖两个 backend,从根本上保证状态机行为一致。

测试只调内部方法,**永远不调 .start()**,从而:
1. 不会真的起后台线程 / 真去 /dev/input 找键盘 / 真注册全局监听
2. conftest 注入的 fake pynput / evdev 已经让 import 可行
"""

import importlib
import time

import pytest

BACKENDS = ["hotkey_macos", "hotkey_linux"]


def _load(name: str):
    return importlib.import_module(
        f"whisper_input.backends.{name}"
    )


def _make_listener(mod, hotkey: str, monkeypatch, delay: float):
    """构造一个 HotkeyListener,把模块的 COMBO_DELAY 改成给定值。"""
    monkeypatch.setattr(mod, "COMBO_DELAY", delay)
    presses: list[None] = []
    releases: list[None] = []
    listener = mod.HotkeyListener(
        hotkey=hotkey,
        on_press=lambda: presses.append(None),
        on_release=lambda: releases.append(None),
    )
    return listener, presses, releases


@pytest.mark.parametrize("backend", BACKENDS)
def test_modifier_early_release_does_not_trigger(backend, monkeypatch):
    """修饰键按下后在 delay 过期前就释放 → 不触发 on_press / on_release。"""
    mod = _load(backend)
    listener, presses, releases = _make_listener(
        mod, "KEY_RIGHTCTRL", monkeypatch, delay=0.2
    )

    listener._on_hotkey_press()
    listener._on_hotkey_release()
    # 等远超 delay 的时间确保 Timer 真的没机会再 fire
    time.sleep(0.3)

    assert presses == []
    assert releases == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_modifier_full_press_release_triggers_both(
    backend, monkeypatch
):
    """修饰键按下 → 等过 delay → 释放 → on_press 和 on_release 各 1 次。"""
    mod = _load(backend)
    listener, presses, releases = _make_listener(
        mod, "KEY_RIGHTCTRL", monkeypatch, delay=0.05
    )

    listener._on_hotkey_press()
    # 等 Timer 触发 _on_delayed_press
    time.sleep(0.15)
    assert presses == [None]
    assert releases == []

    listener._on_hotkey_release()
    assert releases == [None]


@pytest.mark.parametrize("backend", BACKENDS)
def test_modifier_combo_detected_cancels_trigger(
    backend, monkeypatch
):
    """修饰键按下后检测到组合键 → 取消触发 → 释放也不触发。"""
    mod = _load(backend)
    listener, presses, releases = _make_listener(
        mod, "KEY_RIGHTCTRL", monkeypatch, delay=0.2
    )

    listener._on_hotkey_press()
    listener._on_combo_detected()
    time.sleep(0.3)
    listener._on_hotkey_release()

    assert presses == []
    assert releases == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_non_modifier_key_triggers_immediately(backend, monkeypatch):
    """非修饰键(F1)按下 → 立即 on_press,不走 delay 路径。"""
    mod = _load(backend)
    listener, presses, releases = _make_listener(
        mod, "KEY_F1", monkeypatch, delay=10.0  # 故意设很大,证明不走 delay
    )

    listener._on_hotkey_press()
    # 不 sleep,立即 assert
    assert presses == [None]

    listener._on_hotkey_release()
    assert releases == [None]


@pytest.mark.parametrize("backend", BACKENDS)
def test_unknown_hotkey_raises(backend):
    """构造时传不支持的 hotkey 名 → ValueError。"""
    mod = _load(backend)
    with pytest.raises(ValueError, match="Unsupported hotkey"):
        mod.HotkeyListener(
            hotkey="KEY_NONEXISTENT",
            on_press=lambda: None,
            on_release=lambda: None,
        )


@pytest.mark.parametrize("backend", BACKENDS)
def test_supported_keys_dict_non_empty(backend):
    """SUPPORTED_KEYS 至少包含 RIGHTCTRL / F1 / CAPSLOCK 三个常用项。"""
    mod = _load(backend)
    assert "KEY_RIGHTCTRL" in mod.SUPPORTED_KEYS
    assert "KEY_F1" in mod.SUPPORTED_KEYS
    assert "KEY_CAPSLOCK" in mod.SUPPORTED_KEYS
