"""版本号管理 - 统一提供 __version__ 变量。"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("whisper-input")
except PackageNotFoundError:
    # 开发模式或未安装时，从 pyproject.toml 解析
    import re
    from pathlib import Path

    _toml = Path(__file__).parent / "pyproject.toml"
    _m = re.search(r'^version\s*=\s*"(.+?)"', _toml.read_text(), re.M)
    __version__ = _m.group(1) if _m else "dev"
