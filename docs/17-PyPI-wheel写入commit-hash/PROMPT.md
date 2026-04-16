# PyPI wheel 写入 commit hash

## 问题

从 PyPI 安装的 whisper-input（`uv pip install whisper-input`）在设置页面只显示版本号（如 v0.5.2），不显示 git commit hash。

## 原因

`version.py` 的 `_read_commit()` 有两条 fallback 路径：

1. 读 package data 里的 `_commit.txt` → PyPI wheel 里没有这个文件
2. 跑 `git rev-parse HEAD` → 从 PyPI 安装的包不在 git 仓库里，失败

最终返回空字符串，设置页面的 commit 链接不渲染。

之前本地开发模式可以用 git 命令拿到 commit hash，所以显示正常。第 14 轮 PyPI 分发开发时认为此行为可接受，未修复。

## 需求

在构建 wheel 时自动将当前 git commit hash 写入 `_commit.txt`，使 PyPI 安装的版本也能在设置页面显示代码版本。
