# 需求：DEB 安装体验优化

## 背景

当前 DEB 包安装后，首次从桌面菜单启动时需要：
1. `uv sync` 下载 Python 依赖（PyTorch ~2GB，总计耗时可能十几分钟）
2. FunASR 首次加载 SenseVoice 模型时从 ModelScope 下载（~500MB）

由于 desktop 文件配置了 `Terminal=false`，用户在整个过程中看不到任何进度提示，会以为程序卡死或安装失败。

## 需求

将 `uv sync`（安装 Python 依赖）和模型预下载都挪到 DEB 的 `postinst` 阶段完成：

1. **`postinst` 中执行 `uv sync`**：`dpkg` 本身在终端中运行，用户能看到下载进度
2. **`postinst` 中预下载 SenseVoice 模型**：`uv sync` 完成后，通过 `uv run python -c "..."` 触发模型下载，同样在终端中展示进度
3. **launcher 脚本适配**：首次启动的 `uv sync` 逻辑改为 fallback（仅在 venv 不存在时补救），不再是主要安装路径
4. 安装完成后用户从桌面菜单点击应该能直接使用，无需长时间等待
