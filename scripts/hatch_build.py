"""Hatch 自定义构建钩子：将 git commit hash 写入 wheel。"""

import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """构建时将当前 git commit hash 写入 _commit.txt。"""

    PLUGIN_NAME = "custom"

    def initialize(self, version, build_data):
        self._written_file: Path | None = None

        # 源码树路径 / sdist 解包路径（sdist 中 force_include 去掉了 src/ 前缀）
        src_path = (
            Path(self.root) / "src" / "whisper_input" / "_commit.txt"
        )
        sdist_path = (
            Path(self.root) / "whisper_input" / "_commit.txt"
        )

        # 从 sdist 构建 wheel 时，文件已存在于 sdist 解包目录
        for existing in (src_path, sdist_path):
            if existing.exists() and existing.read_text(encoding="utf-8").strip():
                build_data["force_include"][str(existing)] = (
                    "whisper_input/_commit.txt"
                )
                return

        # 首次构建：从 git 获取 commit hash
        commit = self._get_commit()
        if not commit:
            return

        src_path.write_text(commit, encoding="utf-8")
        build_data["force_include"][str(src_path)] = (
            "whisper_input/_commit.txt"
        )
        self._written_file = src_path

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
