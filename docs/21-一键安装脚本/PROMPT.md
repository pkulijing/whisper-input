# 一键安装脚本

## 背景

14 轮把分发路线定在 PyPI（`uv tool install whisper-input`）。对已经装了 `uv` 的技术用户足够自然，但对"会用 terminal 但没碰过 Python 生态"的用户门槛仍然偏高 —— 他们得先知道 `uv` 是什么、怎么装、再去查系统依赖（portaudio / xdotool / input 组…）。

BACKLOG 里一直挂着「一键安装脚本（`curl | bash` 风格）」这条，这一轮把它做掉。

## 目标

提供一条 `curl | sh` 命令，在一台干净的 macOS / Linux 上把 Whisper Input 从零装好、并且询问用户是否立即启动。理想状态：

```bash
curl -LsSf https://raw.githubusercontent.com/pkulijing/whisper-input/master/install.sh | sh
```

脚本完成后，终端里直接 `whisper-input` 就能跑起来。

## 具体要求

1. **脚本托管**：暂时走 GitHub Raw（方案 A），不考虑自定义域名。命令形如上面示例。脚本本身放在仓库根目录 `install.sh`。

2. **脚本负责的事**（按 BACKLOG 列出的步骤做全套 init）：
   - 检测平台（macOS / Linux）和架构（arm64 / x86_64），不支持的直接退出
   - 检测并装好 `uv`（官方一行脚本）
   - 检测并装好 Python 3.12（通过 `uv python install`）
   - 装系统依赖：
     - macOS：`brew install portaudio`
     - Linux（Debian/Ubuntu 系）：`sudo apt install xdotool xclip pulseaudio-utils libportaudio2 gir1.2-ayatanaappindicator3-0.1` 等
     - 其他发行版（Fedora / Arch）如果短期能顺手支持就加上；不行就明确提示用户手动装
   - Linux：引导 `sudo usermod -aG input $USER`（交互确认）
   - 跑 **`uv tool install --upgrade whisper-input`** —— **注意一定带 `--upgrade`，处理"用户之前装过想升级"的情况**
   - 跑 **`whisper-input --init`** —— 这是 19 轮加的一次性初始化命令（见 `docs/19-macOS-TCC权限/SUMMARY.md` 第 5 节）：macOS 下会安装 `~/Applications/Whisper Input.app` bundle（TCC 权限归属需要），全平台都会下载 SenseVoice ONNX 模型（约 231 MB）。把这步做在安装脚本里，用户首次启动就不会卡在"正在下载 231MB 模型"
   - 打印安装成功信息

3. **结尾交互**：装完之后**询问用户是否立即启动 `whisper-input`**；回答 y / yes 就直接启动（前台 `exec`，shell 退出时也一起退出），其他一律当作「之后自己跑」。

4. **可重入**：整个脚本支持**重复执行**。已经装过 `uv` / Python / 系统依赖的跳过或提示"已存在"，`uv tool install --upgrade` 天然幂等。

5. **错误处理**：脚本开头 `set -eu`，任何一步失败要打印清晰的错误信息并退出，不要一路挣扎到最后产生更难 debug 的状态。

## 非目标

- **不做 .deb / .dmg / AppImage**。14 轮已经放弃 native bundle 路线，这个脚本是"懒人的 DMG"而不是替代品。
- **不做 Windows 支持**。项目本来就只支持 Linux + macOS。
- **不做自定义域名 / 短链**（BACKLOG 方案 B）—— 这一轮先用 GitHub Raw，以后真觉得 URL 太长再说。
- **不做 Release asset 版本绑定**（BACKLOG 方案 C）—— 同上，以后再说。
- **不追求完整的发行版测试矩阵**。主测 macOS（Apple Silicon）+ Ubuntu 24.04，其他发行版能支持多少支持多少，不做就打清楚错误让用户手动装依赖。

## 交付物

- `install.sh`（仓库根目录）
- `README.md` / `README.zh-CN.md` 的"安装"段落里把一键命令放在最显眼的位置，老的"手动 uv tool install"作为次选保留
- `docs/21-一键安装脚本/PLAN.md`、`SUMMARY.md`
