# 开发总结：DEB 安装体验优化

## 开发项背景

DEB 包安装后，首次从桌面菜单启动时需要下载 PyTorch（~2GB）和 SenseVoice 模型（~500MB），由于 desktop 文件配置了 `Terminal=false`，整个过程用户看不到任何进度提示，会以为程序无响应。此外，修改快捷键等需要重启才能生效的配置后，用户没有便捷的重启方式。

## 实现方案

### 关键设计

1. 将耗时的依赖安装和模型下载挪到 `postinst` 阶段，利用 `dpkg` 在终端中运行的特性展示进度
2. 设置页面增加重启功能，修改需重启生效的配置后自动弹出确认框
3. 设置服务改用固定端口，保证重启后页面能自动重连

### 开发内容概括

1. **`debian/postinst`**：在 uv 检测/安装之后，以 `$SUDO_USER` 身份执行：
   - `uv sync`：安装 Python 依赖到 per-user venv
   - `uv run python -c "..."`：触发 FunASR AutoModel 下载模型到 `~/.cache/modelscope/`
   - 失败时打印警告但不阻塞安装流程

2. **`debian/whisper-input.sh`**：将首次运行的 `uv sync` 逻辑简化为 fallback，仅在 venv 不存在时触发

3. **`settings_server.py`**：
   - 新增 `/api/restart` 接口，通过 `os.execv` 以相同参数重新执行当前进程
   - 设置页面增加「重启程序」按钮
   - 修改快捷键、计算设备、端口后自动弹出确认框提示重启
   - `SettingsServer` 改为接受固定端口参数（默认 51230），重启后端口不变

4. **`config_manager.py`**：`DEFAULT_CONFIG` 和 YAML 生成器增加 `settings_port` 字段

5. **`main.py`**：从 config 读取 `settings_port` 传给 `SettingsServer`

## 局限性

- `postinst` 中模型预下载使用 `device='cpu'`，首次启动时仍需将模型加载到 GPU（几秒，不涉及网络下载）
- 如果 `$SUDO_USER` 为空（纯 root 安装），依赖和模型会装到 root 的目录下，实际用户首次启动仍需安装
- 固定端口 51230 可能与其他服务冲突，但用户可在设置中修改

## 后续 TODO

- 考虑支持多用户场景：DEB 安装时只有一个 `$SUDO_USER`，其他用户首次启动仍走 fallback
- launcher fallback 时可考虑弹出终端窗口显示进度，而非静默执行
