"""版本号管理 - 统一提供 __version__ 和 __commit__ 变量。"""

import subprocess
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files

try:
    __version__ = version("whisper-input")
except PackageNotFoundError:
    __version__ = "dev"


def _read_commit() -> str:
    # 优先读 package data 里的 _commit.txt(预留给发版流程写入,
    # 当前 PyPI wheel 里并不会有这个文件)
    try:
        commit_file = files("whisper_input") / "_commit.txt"
        if commit_file.is_file():
            c = commit_file.read_text().strip()
            if c:
                return c
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        pass
    # 开发模式:从包文件所在目录向上找 .git
    try:
        pkg_dir = str(files("whisper_input"))
        r = subprocess.run(
            ["git", "-C", pkg_dir, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


__commit__ = _read_commit()
