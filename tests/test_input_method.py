"""测试两个平台的 input_method shell-out 顺序。

针对 src/daobidao/backends/input_macos.py 和 input_linux.py。

测试策略:
- monkeypatch subprocess.run 成记录调用的 fake,验证调用顺序和参数
- macOS 的 pynput Controller 由 conftest 注入的 fake 提供,fake 会把
  press / release 记到 .calls 列表里
"""

from daobidao.backends import input_linux as il
from daobidao.backends import input_macos as im


class _RunRecorder:
    """记录 subprocess.run 调用的 fake。

    每条记录是 (cmd, input_bytes) 元组。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bytes | None]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs.get("input")))

        class Result:
            stdout = b""
            returncode = 0

        return Result()


# --- macOS ---


def test_macos_empty_text_no_subprocess(monkeypatch):
    rec = _RunRecorder()
    monkeypatch.setattr(im.subprocess, "run", rec)
    im.type_text("")
    assert rec.calls == []


def test_macos_clipboard_paste_sequence(monkeypatch):
    rec = _RunRecorder()
    monkeypatch.setattr(im.subprocess, "run", rec)
    # 不真等 50ms / 100ms 的 sleep
    monkeypatch.setattr(im.time, "sleep", lambda _: None)
    # 清掉 fake Controller 的历史调用
    im._keyboard.calls.clear()

    im.type_text("hello world")

    # 至少 3 次 subprocess(pbpaste 读 + pbcopy 写 + pbcopy 还原)
    cmds = [c[0][0] for c in rec.calls]
    assert "pbpaste" in cmds
    assert cmds.count("pbcopy") >= 1

    # 找到写入新文本的那次 pbcopy 调用
    paste_payloads = [
        payload
        for cmd, payload in rec.calls
        if cmd[0] == "pbcopy" and payload is not None
    ]
    assert b"hello world" in paste_payloads

    # Cmd+V 的 4 次按键调用顺序
    from pynput.keyboard import Key

    assert im._keyboard.calls == [
        ("press", Key.cmd),
        ("press", "v"),
        ("release", "v"),
        ("release", Key.cmd),
    ]


# --- Linux ---


def test_linux_empty_text_no_subprocess(monkeypatch):
    rec = _RunRecorder()
    monkeypatch.setattr(il.subprocess, "run", rec)
    il.type_text("")
    assert rec.calls == []


def test_linux_clipboard_writes_both_selections_and_pastes(
    monkeypatch,
):
    rec = _RunRecorder()
    monkeypatch.setattr(il.subprocess, "run", rec)
    monkeypatch.setattr(il.time, "sleep", lambda _: None)

    il.type_text("中文测试")

    cmds = [c[0] for c in rec.calls]
    # 必须同时写 clipboard 和 primary 两个 selection
    assert any(c[:3] == ["xclip", "-selection", "clipboard"] for c in cmds)
    assert any(c[:3] == ["xclip", "-selection", "primary"] for c in cmds)
    # 必须用 xdotool key shift+Insert 触发粘贴
    assert any(
        c
        == [
            "xdotool",
            "key",
            "--clearmodifiers",
            "shift+Insert",
        ]
        for c in cmds
    )

    # 写入两个 selection 时 input payload 是 utf-8 编码
    payloads = [
        payload
        for cmd, payload in rec.calls
        if cmd[0] == "xclip" and payload is not None
    ]
    assert any(p == "中文测试".encode() for p in payloads)
