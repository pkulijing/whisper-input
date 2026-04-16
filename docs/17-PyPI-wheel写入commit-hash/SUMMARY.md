# 开发总结

## 开发项背景

从 PyPI 安装的 whisper-input 在设置页面只显示版本号（如 v0.5.2），不显示 git commit hash 链接。原因是 PyPI wheel 中不包含 `_commit.txt`，且安装目录不在 git 仓库中，导致 `version.py` 的两条 fallback 路径均失败。

## 实现方案

### 关键设计

利用 hatchling 的自定义构建钩子（custom build hook）机制，在 `uv build` 时自动将 `git rev-parse HEAD` 的结果写入 `_commit.txt` 并打包进 wheel。

核心难点：`uv build` 默认先建 sdist 再从 sdist 建 wheel，后者在临时目录中执行，没有 git 信息。解决方式是让 hook 兼容两种场景：
- **直接构建 / sdist 构建**：从 git 获取 commit hash，写入文件
- **从 sdist 构建 wheel**：识别 sdist 中已存在的 `_commit.txt`，直接 force_include

### 开发内容概括

| 文件 | 变更 |
|------|------|
| `hatch_build.py`（新建） | 自定义构建钩子，`initialize` 写入 commit hash 并 force_include，`finalize` 清理临时文件 |
| `pyproject.toml` | 添加 `[tool.hatch.build.hooks.custom]` 启用钩子 |

### 额外产物

无。

## 局限性

- 在非 git 环境（如 CI 的 shallow clone 未配置 fetch-depth）中，`git rev-parse HEAD` 可能失败，此时 wheel 不包含 commit hash，行为退化到修复前（只显示版本号）

## 后续 TODO

- 发版时需要重新 `uv build` 并发布，新 wheel 才会包含 commit hash
- 可考虑在 GitHub Actions release workflow 中确保 git 信息可用
