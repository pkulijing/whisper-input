# 实现计划：DEB 安装体验优化

## 问题分析

DEB 安装后首次从桌面菜单启动时，`uv sync`（下载 PyTorch ~2GB）和 FunASR 模型下载（~500MB）在 `Terminal=false` 环境下静默执行，用户长时间看不到任何反馈，会以为程序卡死。

## 方案设计

将耗时的依赖安装和模型下载挪到 `postinst` 阶段完成。`dpkg` 本身在终端中运行，用户能看到所有下载进度。

### 关键设计点

1. **`postinst` 中以实际用户身份执行**：通过 `su - $SUDO_USER` 切换到安装用户，设置 `UV_PROJECT_ENVIRONMENT` 指向 per-user venv（`~/.local/share/whisper-input/.venv`）

2. **uv 路径查找**：先检查 `~/.local/bin/uv`，找不到再通过 `su` 加载用户完整环境查找

3. **模型预下载用 CPU**：`device='cpu'` 避免 CUDA 初始化问题，只需下载模型文件即可

4. **失败不阻塞安装**：`uv sync` 或模型下载失败时打印警告，不 `exit 1`，launcher 脚本中有 fallback 逻辑兜底

5. **launcher 保留 fallback**：`whisper-input.sh` 中仍检测 venv 是否存在，不存在时补救安装

## 修改文件

- `debian/postinst`：uv 安装后增加 `uv sync` + 模型预下载
- `debian/whisper-input.sh`：首次运行逻辑简化为 fallback
