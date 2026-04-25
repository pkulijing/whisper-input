"""测试套全局 fixture / setup。

核心职责:在 import 任何 daobidao 模块**之前**,往 sys.modules 里强制
注入伪造的 pynput / evdev,这样:

1. CI 跑在 ubuntu-24.04 上,pynput 是 darwin-only 依赖根本没装,
   `daobidao.backends.hotkey_macos` / `input_macos` 直接 import 就会挂
2. 在真 macOS 上,如果不替换真 pynput,测试调到 Listener.start() 会真的开
   全局键盘监听,污染开发者的 system

用强制注入(不是 if "pynput" not in sys.modules)双向保证两边都用 fake。

此外,qwen3_* 系列测试依赖 ModelScope 下载的 ONNX / tokenizer 文件。
session-scoped 的 ``stt_0_6b`` / ``stt_1_7b`` fixture 各调一次
``Qwen3ASRSTT.load()`` —— 由 STT 自己触发 ``modelscope.snapshot_download``
(本地 cache 命中秒过,缺失则联网下),拿到的 ``cache_root`` 给其它 fixture
反推 ``model_*`` / ``tokenizer`` 子目录。CI 由 actions/cache 预热整个
modelscope hub。
"""

import sys
import types
from pathlib import Path

import pytest


def _install_fake_pynput() -> None:
    """注入 pynput / pynput.keyboard 占位,只满足 import 不报错的最低要求。"""

    class Key:
        # 修饰键(左 / 右各一份,匹配 hotkey_macos 的 SUPPORTED_KEYS)
        ctrl = "ctrl"
        ctrl_r = "ctrl_r"
        alt = "alt"
        alt_r = "alt_r"
        cmd = "cmd"
        cmd_r = "cmd_r"
        # 非修饰键
        caps_lock = "caps_lock"
        f1 = "f1"
        f2 = "f2"
        f5 = "f5"
        f12 = "f12"

    class Listener:
        def __init__(self, on_press=None, on_release=None) -> None:
            self.on_press = on_press
            self.on_release = on_release
            self.started = False
            self.stopped = False

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

    class Controller:
        """记录 press / release 调用顺序的 fake。

        测试 input_macos.type_text 时验证 Cmd+V 序列正确。
        """

        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def press(self, key) -> None:
            self.calls.append(("press", key))

        def release(self, key) -> None:
            self.calls.append(("release", key))

    pynput = types.ModuleType("pynput")
    keyboard = types.ModuleType("pynput.keyboard")
    keyboard.Key = Key
    keyboard.Listener = Listener
    keyboard.Controller = Controller
    pynput.keyboard = keyboard

    sys.modules["pynput"] = pynput
    sys.modules["pynput.keyboard"] = keyboard


def _install_fake_evdev() -> None:
    """注入 evdev / evdev.ecodes 占位。

    evdev 在 linux 上是真依赖,但 conftest 还是强制覆盖,避免 hotkey_linux
    的 _listen_loop 真去 /dev/input 找键盘。
    """
    ecodes = types.ModuleType("evdev.ecodes")
    # 任意整数即可,只要 hotkey_linux.SUPPORTED_KEYS 能从 ecodes 里查到
    ecodes.KEY_RIGHTCTRL = 97
    ecodes.KEY_LEFTCTRL = 29
    ecodes.KEY_RIGHTALT = 100
    ecodes.KEY_LEFTALT = 56
    ecodes.KEY_RIGHTMETA = 126
    ecodes.KEY_LEFTMETA = 125
    ecodes.KEY_CAPSLOCK = 58
    ecodes.KEY_F1 = 59
    ecodes.KEY_F2 = 60
    ecodes.KEY_F12 = 88
    ecodes.EV_KEY = 1
    ecodes.KEY_A = 30
    ecodes.KEY_Z = 44

    class InputDevice:
        def __init__(self, path: str) -> None:
            self.path = path
            self.name = "fake-keyboard"

        def capabilities(self, verbose: bool = False) -> dict:
            return {ecodes.EV_KEY: [ecodes.KEY_A, ecodes.KEY_Z]}

        def read(self):
            return iter(())

        def close(self) -> None:
            pass

    evdev = types.ModuleType("evdev")
    evdev.ecodes = ecodes
    evdev.InputDevice = InputDevice
    evdev.list_devices = lambda: []

    sys.modules["evdev"] = evdev
    sys.modules["evdev.ecodes"] = ecodes


# 必须在任何 daobidao.backends.hotkey_* / input_* import 之前执行
_install_fake_pynput()
_install_fake_evdev()


# --------------------------------------------------------------------------
# Qwen3-ASR fixtures
#
# 由 ``Qwen3ASRSTT.load()`` 触发唯一一次 modelscope snapshot_download,
# 然后所有路径 fixture 从 ``stt.cache_root`` 反推。session-scoped 实例
# 在不同测试文件之间共享,ONNX session 只加载一次。
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def stt_0_6b():
    """已加载的 0.6B Qwen3ASRSTT 实例(含 runner / tokenizer / cache_root)。

    cache 命中秒过;缺失则联网下载 ~990 MiB。
    """
    from daobidao.stt.qwen3 import Qwen3ASRSTT

    s = Qwen3ASRSTT(variant="0.6B")
    s.load()
    return s


@pytest.fixture(scope="session")
def stt_1_7b():
    """已加载的 1.7B Qwen3ASRSTT 实例。缺失则联网下载 ~2.4 GiB。"""
    from daobidao.stt.qwen3 import Qwen3ASRSTT

    s = Qwen3ASRSTT(variant="1.7B")
    s.load()
    return s


@pytest.fixture(scope="session")
def qwen3_tokenizer_dir(stt_0_6b) -> Path:
    return stt_0_6b.cache_root / "tokenizer"


@pytest.fixture(scope="session")
def qwen3_0_6b_model_dir(stt_0_6b) -> Path:
    return stt_0_6b.cache_root / "model_0.6B"


@pytest.fixture(scope="session")
def qwen3_1_7b_model_dir(stt_1_7b) -> Path:
    return stt_1_7b.cache_root / "model_1.7B"
