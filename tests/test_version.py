"""测试 version 模块的 fallback 逻辑。

针对 src/daobidao/version.py。

`__version__` 走 importlib.metadata,在 dev 安装下应该等于 pyproject.toml 里
的版本号; `_read_commit()` 有三条路径(_commit.txt / git / 空),每条都覆盖。
"""

import subprocess

import daobidao.version as version_mod


def test_version_is_non_empty_string():
    """dev 安装下 __version__ 等于 pyproject.toml 里的版本(>= 0.5.0)。"""
    assert isinstance(version_mod.__version__, str)
    assert version_mod.__version__
    assert version_mod.__version__ != "dev"


def test_read_commit_from_commit_file(tmp_path, monkeypatch):
    """_commit.txt 存在 → 直接读它的内容。"""
    fake_commit = "deadbeef" * 5
    (tmp_path / "_commit.txt").write_text(fake_commit, encoding="utf-8")

    # files("daobidao") / "_commit.txt" 应该返回 tmp_path / "_commit.txt"
    def fake_files(pkg: str):
        return tmp_path

    monkeypatch.setattr(version_mod, "files", fake_files)
    assert version_mod._read_commit() == fake_commit


def test_read_commit_falls_back_to_git(tmp_path, monkeypatch):
    """_commit.txt 不存在但 git rev-parse 成功 → 用 git 输出。"""
    # 注意:tmp_path 下没有 _commit.txt,所以 is_file() 是 False
    monkeypatch.setattr(version_mod, "files", lambda pkg: tmp_path)

    fake_sha = "abc1234567890"

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=fake_sha + "\n", stderr=""
        )

    monkeypatch.setattr(version_mod.subprocess, "run", fake_run)
    assert version_mod._read_commit() == fake_sha


def test_read_commit_returns_empty_when_all_fail(tmp_path, monkeypatch):
    """_commit.txt 不存在 + git 也失败 → 返回 ""。"""
    monkeypatch.setattr(version_mod, "files", lambda pkg: tmp_path)

    def fake_run(cmd, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(version_mod.subprocess, "run", fake_run)
    assert version_mod._read_commit() == ""


def test_read_commit_returns_empty_when_git_returns_nonzero(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(version_mod, "files", lambda pkg: tmp_path)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=128, stdout="", stderr="not a git repo"
        )

    monkeypatch.setattr(version_mod.subprocess, "run", fake_run)
    assert version_mod._read_commit() == ""
