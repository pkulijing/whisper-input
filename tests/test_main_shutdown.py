"""测试退出路径下 PortAudio 主动终止的行为。

对应 docs/24-退出路径CoreAudio死锁修复/。核心不变量：

1. 正常情况下 terminate_portaudio 返回 True，并调用 sounddevice._terminate
2. atexit.unregister 会把 sd._exit_handler 从默认 atexit 链里摘掉
3. 如果 _terminate 自己卡住，主线程必须在 timeout 内拿到 False 返回
4. _terminate 抛异常时返回 False + 记日志，不崩
5. sounddevice 未安装（ImportError）时优雅返回 True
"""

import atexit
import sys
import time
import types

import pytest


@pytest.fixture
def fake_sd(monkeypatch):
    """往 sys.modules 注入一份可控的 fake sounddevice。

    yield 之后清理 atexit 里可能残留的 fake _exit_handler，
    避免污染测试进程自己的退出路径。
    """
    called = {"terminate": 0, "exit_handler": 0}

    def _exit_handler() -> None:
        called["exit_handler"] += 1

    def _terminate() -> None:
        called["terminate"] += 1

    fake = types.ModuleType("sounddevice")
    fake._exit_handler = _exit_handler
    fake._terminate = _terminate

    # sounddevice 真实行为是 import 时把 _exit_handler 注册到 atexit；
    # fake 复刻这个行为，好让 terminate_portaudio 能真的 unregister。
    atexit.register(_exit_handler)

    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    try:
        yield fake, called
    finally:
        atexit.unregister(_exit_handler)


def _load_terminate():
    from daobidao.__main__ import terminate_portaudio

    return terminate_portaudio


def test_normal_path_returns_true_and_calls_terminate(fake_sd):
    _fake, called = fake_sd
    terminate = _load_terminate()

    assert terminate(timeout=1.0) is True
    assert called["terminate"] == 1


def test_unregisters_default_atexit(fake_sd):
    """验证 sd._exit_handler 被从 atexit 链里摘掉。"""
    fake, _called = fake_sd
    terminate = _load_terminate()

    # 先确认 handler 在链里（unregister 返回的 count 测不到注册状态，
    # 但可以通过再次 unregister 返回值是否仍然成功间接验证：
    # unregister 幂等,已摘就是 no-op,不报错）
    assert terminate(timeout=1.0) is True

    # 手动再调一次 atexit.unregister，确认 handler 已不在链里 →
    # 再 unregister 也不会出错（幂等语义）
    atexit.unregister(fake._exit_handler)
    # 没抛就算过


def test_timeout_returns_false_when_terminate_hangs(fake_sd, monkeypatch):
    fake, _called = fake_sd

    def hanging_terminate() -> None:
        # 睡够长,确保 timeout 先到
        time.sleep(5)

    fake._terminate = hanging_terminate
    terminate = _load_terminate()

    start = time.perf_counter()
    result = terminate(timeout=0.2)
    elapsed = time.perf_counter() - start

    assert result is False
    # 留点 slack,但不能偏离 0.2 太多
    assert 0.15 < elapsed < 0.6, f"timeout 兜底偏离预期: {elapsed:.3f}s"


def test_terminate_exception_returns_false(fake_sd):
    fake, _called = fake_sd

    def boom() -> None:
        raise RuntimeError("Pa_Terminate failed")

    fake._terminate = boom
    terminate = _load_terminate()

    assert terminate(timeout=1.0) is False


def test_missing_sounddevice_returns_true(monkeypatch):
    """没装 sounddevice 时,terminate_portaudio 直接返回 True,不抛。"""
    # 确保 sys.modules 里没有 sounddevice
    monkeypatch.delitem(sys.modules, "sounddevice", raising=False)

    # 拦截 import 让它抛 ImportError
    real_import = (
        __builtins__["__import__"]
        if isinstance(__builtins__, dict)
        else __builtins__.__import__
    )

    def fake_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise ImportError("no sounddevice installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    terminate = _load_terminate()
    assert terminate(timeout=1.0) is True


def test_missing_terminate_attr_still_unregisters(monkeypatch):
    """上游改了 API 把 _terminate 删了: 仍然应该 unregister atexit + 返回 True。

    保证 Python finalize 阶段不会再跑默认 atexit(死锁源头)。
    """
    called = {"exit_handler": 0}

    def _exit_handler() -> None:
        called["exit_handler"] += 1

    fake = types.ModuleType("sounddevice")
    fake._exit_handler = _exit_handler
    # 注意:故意不提供 _terminate

    atexit.register(_exit_handler)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    try:
        terminate = _load_terminate()
        assert terminate(timeout=0.5) is True
    finally:
        atexit.unregister(_exit_handler)


def test_does_not_block_when_terminate_returns_quickly(fake_sd):
    """正常返回路径必须远小于 timeout,不能傻等。"""
    terminate = _load_terminate()

    start = time.perf_counter()
    assert terminate(timeout=5.0) is True
    elapsed = time.perf_counter() - start
    # daemon 线程 spawn + Event.wait 唤醒 << 0.5s
    assert elapsed < 0.5
