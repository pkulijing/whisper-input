# macOS 平台支持 - 开发总结

## 开发项背景

macOS 上微信等输入法的语音转文字效果不佳，而 Whisper Input 使用本地 SenseVoice 模型，识别效果更好。用户希望将原本 Linux 专用的 Whisper Input 移植到 macOS，实现跨平台支持，并提供 DMG 安装包。

## 实现方案

### 关键设计

1. **平台后端抽象层（`backends/`）**：按平台拆分实现，顶层模块（`hotkey.py`、`input_method.py`）运行时按 `sys.platform` 自动选择后端，无抽象基类
2. **Helper .app 方案**：在用户数据目录创建最小 `.app` bundle 包裹 Python 二进制副本，解决 macOS 权限系统需要按可执行文件路径追踪权限的问题，使辅助功能/输入监控/麦克风权限列表中显示正确的应用名称和图标
3. **权限引导流程**：使用 `AXIsProcessTrusted()`（辅助功能）和 `CGPreflightListenEventAccess()`/`CGRequestListenEventAccess()`（输入监控）检测权限状态，分步弹窗引导用户设置，完成后自动重启
4. **设备优先级**：`sensevoice.device` 单选改为 `device_priority` 列表（`[cuda, mps, cpu]`），运行时自动选第一个可用设备，彻底解决跨平台配置兼容问题
5. **PyTorch 跨平台依赖**：通过 `[tool.uv.sources]` 条件 marker 实现 Linux 从 SJTU CUDA 镜像装 `torch==2.5.1+cu121`，macOS 从默认 PyPI 装 `torch==2.5.1`

### 开发内容概括

**新建文件：**
- `backends/__init__.py` — 平台检测常量
- `backends/hotkey_linux.py` / `hotkey_macos.py` — 热键监听（evdev / pynput）
- `backends/input_linux.py` / `input_macos.py` — 文字输入（xclip+xdotool / pbcopy+pynput）
- `backends/autostart_linux.py` / `autostart_macos.py` — 自启动（.desktop / LaunchAgents）
- `macos/Info.plist` — .app 元数据模板
- `macos/whisper-input.sh` — macOS .app launcher 脚本（检查依赖、创建 helper .app、设置环境变量）
- `build_macos.sh` — 构建 .app + DMG 安装包
- `setup_macos.sh` — macOS 开发环境配置脚本
- `run_macos.sh` — macOS 开发模式启动脚本

**修改文件：**
- `hotkey.py` / `input_method.py` — 改为平台调度器
- `main.py` — GI_TYPELIB_PATH 守卫、play_sound(afplay/paplay)、macOS 权限检查、pystray 主线程运行
- `config_manager.py` — 平台路径、`device_priority` 替代 `device`
- `settings_server.py` — 热键列表、设备显示、输入方式按平台适配
- `stt_sensevoice.py` — `_select_device()` 按优先级列表选设备
- `pyproject.toml` — 条件依赖、条件 source、版本锁定
- `build_deb.sh` — 补充 backends/ 目录拷贝
- `recorder.py` — 添加录音诊断日志

### 额外产物

- `docs/5-macOS平台支持/PROMPT.md` — 需求文档
- `docs/5-macOS平台支持/PLAN.md` — 实现计划

## 关键技术挑战与解决

| 问题 | 解决方案 |
|------|---------|
| pynput 无 `Key.fn` 属性 | 去掉 Fn 键支持，macOS 默认改为右 Ctrl |
| PyTorch CUDA 源在 macOS 无 wheel | `[tool.uv.sources]` 加 `marker = "sys_platform == 'linux'"` |
| `uv run` 导致权限归到 uv 名下 | 复制 Python 二进制为 `whisper-input`，配合 PYTHONHOME + PYTHONPATH + dylib 软链接 |
| 复制的二进制找不到标准库 | 设置 `PYTHONHOME` 指向 `sys.base_prefix` |
| 复制的二进制找不到 venv 包 | 设置 `PYTHONPATH` 指向 site-packages |
| macOS 权限列表无图标 | 将二进制放入最小 .app bundle + lsregister 注册 |
| 输入监控权限无法检测 | 使用 `CGPreflightListenEventAccess()` / `CGRequestListenEventAccess()` API |
| 麦克风静默拒绝无弹窗 | helper .app Info.plist 添加 `NSMicrophoneUsageDescription` |
| pystray 图标不显示 | macOS 上 `icon.run()` 必须在主线程运行 |
| config 里 `device: cuda` 在 macOS 无效 | 改为 `device_priority` 列表，自动选最优可用设备 |

## 局限性

- macOS .app 版本的麦克风权限弹窗行为尚待验证（依赖 `NSMicrophoneUsageDescription`）
- FunASR 在 macOS 上加载时报 `Loading remote code failed: model`（不影响识别但日志有警告）
- 无自动化测试覆盖
- 未测试 Intel Mac（仅在 Apple Silicon 上验证）

## 后续 TODO

- [ ] 验证 .app 版本麦克风权限弹窗是否正常
- [ ] 验证完整端到端流程（.app 双击 → 录音 → 识别 → 输入）
- [ ] 考虑 Homebrew formula 分发
- [ ] 添加 Wayland 支持（Linux 端遗留问题）
- [ ] 解决 FunASR `Loading remote code failed` 警告
