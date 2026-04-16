"""测试 whisper_input.{hotkey,input_method,tray} 调度器模块。

这些 dispatcher 文件只做一件事:在 import 时按 sys.platform 选择合适的
backend 实现。覆盖率工具把它们当独立代码看,所以加一个 smoke import 测试
让这部分 stmt 也算进 covered。

`whisper_input.overlay` 故意不在这里测 —— 它会触发 import overlay_macos
(pyobjc / AppKit) 或 overlay_linux (GTK / pygobject),都是 conftest 没
办法用 sys.modules 注入兜住的重型原生依赖。overlay 路径的覆盖率永远
是 0,这是有意识的取舍(见 PROMPT.md 的"非目标"段)。

`whisper_input.tray` 同理 —— 两个后端都在 import 时拉 pystray + PIL,
macOS 端还拉 AppKit / Foundation。CI 只跑 ubuntu,import tray_linux 需要
pystray/Pillow（已在 deps 里），所以 Linux CI 上可以测 tray dispatcher。
"""


def test_hotkey_dispatcher_imports():
    from whisper_input import hotkey

    assert hasattr(hotkey, "HotkeyListener")
    assert hasattr(hotkey, "SUPPORTED_KEYS")
    assert "KEY_RIGHTCTRL" in hotkey.SUPPORTED_KEYS


def test_input_method_dispatcher_imports():
    from whisper_input import input_method

    assert hasattr(input_method, "type_text")
    assert callable(input_method.type_text)


def test_tray_dispatcher_imports():
    from whisper_input import tray

    assert hasattr(tray, "run_tray")
    assert callable(tray.run_tray)
