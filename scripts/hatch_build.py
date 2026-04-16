"""Hatch 自定义构建钩子。

功能：
1. 将 git commit hash 写入 wheel (_commit.txt)
2. 将 macOS launcher binary + icns 强制包含进 wheel
   （这些文件在 .gitignore 里，hatch 默认会跳过）
"""

import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """构建时注入额外文件到 wheel。"""

    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        self._written_file: Path | None = None

        self._include_commit_hash(build_data)
        self._include_macos_assets(build_data)

    def _include_commit_hash(self, build_data):
        """将 git commit hash 写入 _commit.txt。"""
        # 源码树路径 / sdist 解包路径
        src_path = (
            Path(self.root) / "src" / "whisper_input" / "_commit.txt"
        )
        sdist_path = (
            Path(self.root) / "whisper_input" / "_commit.txt"
        )

        for existing in (src_path, sdist_path):
            if existing.exists() and existing.read_text(encoding="utf-8").strip():
                build_data["force_include"][str(existing)] = (
                    "whisper_input/_commit.txt"
                )
                return

        commit = self._get_commit()
        if not commit:
            return

        src_path.write_text(commit, encoding="utf-8")
        build_data["force_include"][str(src_path)] = (
            "whisper_input/_commit.txt"
        )
        self._written_file = src_path

    def _include_macos_assets(self, build_data):
        """将 macOS launcher binary 和 icns 图标强制包含进 wheel。

        这些文件在 .gitignore 里（CI 构建产物），
        hatch 默认会跳过，需要 force_include。

        源码树路径: src/whisper_input/assets/macos/
        sdist 解包路径: whisper_input/assets/macos/
        """
        src_dir = (
            Path(self.root) / "src" / "whisper_input"
            / "assets" / "macos"
        )
        sdist_dir = (
            Path(self.root) / "whisper_input"
            / "assets" / "macos"
        )
        for name in ("whisper-input-launcher", "AppIcon.icns"):
            for d in (src_dir, sdist_dir):
                path = d / name
                if path.exists():
                    build_data["force_include"][str(path)] = (
                        f"whisper_input/assets/macos/{name}"
                    )
                    break

    def finalize(self, version, build_data, artifact_path):
        if self._written_file and self._written_file.exists():
            self._written_file.unlink()

    @staticmethod
    def _get_commit() -> str:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return ""
