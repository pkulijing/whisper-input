# macOS 分发优化 — 去 uv 依赖 + 单窗口三阶段进度

## 背景

当前 macOS 版本功能完整，但有几个体验问题：

1. **要求用户预装 uv**：虽然 .app 内已经 bundle 了 uv 二进制，但启动链路仍是 `shell → uv run --python 3.12 setup_window.py`。`uv run --python 3.12` 会在用户首次启动时去网上下载一份 cpython，对非技术用户来说"双击 .app 后卡住下载"是反人类体验。
2. **首次启动有三件慢事**，目前用户感知很差：
   - A. `uv sync` 安装重量级依赖（torch / funasr / sounddevice 等，~800MB）
   - B. 下载 SenseVoice 模型文件（~900MB）
   - C. 把模型加载到内存（每次启动都跑，几秒到几十秒）
3. 上一个 session 写过一版 `setup_window.py`，能展示 A/B 的进度，但 C 阶段被排除在窗口之外（窗口先关、再启动 main.py），用户始终对"为什么三件事不能在同一个窗口里展示进度"很不满。

## 需求

1. **用户拿到 .dmg → 拖到 /Applications → 双击即用**，不再要求预装任何东西（uv / brew / portaudio / Python 全部不需要），首次启动也不依赖联网下 Python。
2. **三阶段进度必须在同一个窗口里展示**：A 装依赖、B 下模型、C 加载模型，**同一个 tkinter 窗口、同一个进度条、同一片滚动 log**。窗口的标题和说明文案随阶段切换。
3. 后续每次启动只显示 stage C（模型加载），加载完即关窗，主程序进入托盘。
4. 麦克风 / 辅助功能 / 输入监控权限稳定地挂在 "Whisper Input.app" 上，不再因为多次 exec 跳壳而出现权限归属错误。

## 决定的技术路线（讨论过的关键决策）

- **bundle uv + bundle python-build-standalone 进 .app**，不引入 PyApp / cargo 等额外工具链。Python 来源在构建期就确定（build.sh 里 curl 下载 + 校验 + 解压到 `Contents/Resources/python/`），用户首启不联网。
- python-build-standalone 的 install_only 变体**自带 Tk**，删掉上一版 `TCL_LIBRARY` hack。
- 同一个 setup_window.py 进程贯穿三阶段，stage C 把 main.py 当 subprocess 跑、从 stdout 抓「模型加载完成」信号，**不**再让 main.py 由 shell 直接 exec。
- TCC 权限归属的兜底方案：先按最干净的方式（python 在 `Resources/`），实测后看弹窗归属，必要时回退到「python 树搬到 `Contents/MacOS/python/`」或「helper.app」方案。

## 不在本次范围

- Linux 构建链路（完全不动）
- 代码签名 / 公证（notarization）
- universal binary
- 自动更新机制
