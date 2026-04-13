# SUMMARY：GitHub CI 与版本管理

## 开发项背景

项目此前没有任何 CI，所有构建都在本机手动执行（`bash build.sh`），痛点：

- 发版时经常忘记 bump `pyproject.toml` 的版本号，本地构建、push、tag、上传这一长串手工流程容易出错
- macOS 和 Linux 的产物依赖开发者切换到对应机器才能构建
- 没有统一的分发渠道，用户只能从源码自行构建

## 实现方案

### 关键设计

1. **单 workflow 多 job 并行**：`.github/workflows/build.yml` 承载 lint / version-check / build-macos / build-linux / release 五个 job，通过 `needs` 声明依赖关系，macOS 和 Linux 构建并行执行。
2. **半自动版本号管理**：`pyproject.toml` 仍是版本号唯一来源，CI 在 version-check job 里读版本号、比对 tag 存在性来判断是否发版：
   - 源码有改动但版本号已对应已存在的 tag → 输出 GitHub Actions warning 提醒"忘记 bump"
   - 版本号不在任何 tag 上 → 设 `is_release=true`，release job 自动打 tag + 建 GitHub Release + 上传产物
3. **lint 与项目依赖解耦**：lint job 用 `uvx ruff check .` 在隔离环境跑，不拖 `uv sync` 整个项目依赖。原因是 linter 职责上不需要运行时依赖，且能绕开 pygobject 3.56 在 Ubuntu 上的源码编译开销。
4. **Runner 版本对齐项目真实 min**：Linux 所有 job 用 `ubuntu-24.04` 而非 `ubuntu-22.04` 或 `ubuntu-latest`：
   - 项目 `debian/control` 声明 `libgirepository-2.0-dev` 依赖，该包仅存在于 Ubuntu 24.04+ 仓库，因此项目真实最低支持版本本就是 24.04
   - 用 22.04 会制造"CI 绿但用户装不了"的盲区
   - deb 不含 native 二进制，runner 的 glibc 版本对产物无影响
5. **macOS runner 固定 `macos-14`**（Sonoma, arm64），不用 `macos-latest` 避免 `hdiutil`/`sips` 行为随时间漂移。
6. **deb 依赖冒烟测试**：build-linux 在打包后跑 `sudo apt-get install --simulate ./*.deb`，用 apt 依赖解析器验证 `Depends` 字段能在目标系统上被满足。不实际安装，避免触发 postinst 里的 `uv sync` 拉 torch。
7. **python-build-standalone 缓存**：用 `actions/cache@v4` 按 `macos/python_dist.txt` 的 hash 作为 key 缓存 `build/macos/cache/`，首次 run 后续每次省掉 30MB python tarball 的下载。
8. **release job 守卫**：`if: is_release == 'true' && github.ref == 'refs/heads/master'`，让 `ci-bootstrap` 调试分支可以正常构建但不会误发 release。

### 开发内容概括

- 新增 `.github/workflows/build.yml`（约 150 行 yaml）
- `README.md`：
  - 顶部加 CI 状态 badge
  - 新增"下载安装包"章节指向 GitHub Releases
  - 新增"发版流程（维护者）"章节说明 bump 版本号 → push → 自动 release 的工作流和 ci-bootstrap 调试约定
  - "系统要求"章节明确写 "Ubuntu 24.04+ / Debian 13+"，避免老发行版用户踩坑
- `docs/8-GitHub CI与版本管理/` 下 PROMPT / PLAN / SUMMARY 三个文档
- 首个正式 GitHub Release：v0.3.0，包含 `WhisperInput_0.3.0.dmg` 和 `whisper-input_0.3.0.deb` 两个 asset

### 额外产物

- 确立了 **ci-bootstrap 调试分支约定**：以后调 CI 都在这个分支上迭代，不污染 master，release 守卫确保调试 push 不会误发版本
- 发现并记录了项目实际最低支持 Ubuntu 版本（24.04）——这个约束此前只隐藏在 `debian/control` 里，没有在 README 或任何文档里明说
- deb 依赖冒烟测试作为持续保护：以后谁往 `debian/control` 加了 runner 上没有的包，CI 立刻红
- **macOS venv 健康自愈**（顺手修）：第一次发版本真机验证时，在自己的开发机上装 dmg，复现了一个早已潜伏的 bug —— 如果用户数据目录 `~/Library/Application Support/Whisper Input/` 里有旧的 venv（由之前本地 `bash build.sh` 产生），`deps_up_to_date` 旧实现只看 venv python 二进制是否存在和 hash 哨兵是否匹配，不会发现 pyvenv.cfg 的 `home` 字段指向的基 python 已经失效。这会让第三方包 import 时抛诡异的 `No module named 'importlib'`。修复：在 `macos/setup_window.py:deps_up_to_date` 里加一步 `subprocess.run` 健康检查，跑一次最小 import 验活，失败则返回 False 触发 `uv sync` 重建。commit: `224805c fix(macos): venv 健康检查捕获 pyvenv.cfg home 失效`

## 局限性

1. **CI 只在 push 后才能验证**：GitHub Actions 的 yaml 没有完整的本地模拟方案（`act` 工具不支持 macOS runner），调试 workflow 只能反复 push 到 ci-bootstrap 分支观察结果。这是 GitHub Actions 生态通病，不是本次方案的特定问题。
2. **macOS 产物未签名、未公证 —— Gatekeeper 必然拦截**：实装验证时在本机装从 GitHub 下载的 `.dmg`，被 macOS Gatekeeper 拦了，提示"无法打开，因为 Apple 无法检查其是否包含恶意软件"。走"系统设置 → 隐私与安全性 → 仍要打开"可以绕过。这是**无 Apple Developer 证书 + 未公证的必然结果**，不是 CI 或打包配置 bug：
   - 浏览器下载 dmg 时 macOS 打 `com.apple.quarantine` xattr
   - 首次启动时 Gatekeeper 发现 quarantine + 未签名 + 未公证 → 拦截
   - 点"仍要打开"后 Gatekeeper 清标记允许启动，但整个过程用户体验很差
   - 彻底消除的唯一办法：Apple Developer Program（$99/年）申请 Developer ID Application 证书 + 用 `codesign` 签 + 用 `notarytool` 公证。证书还需要通过 GitHub secrets 注入 workflow
   - 不花钱的缓解手段：README 里醒目说明"首次打开需走系统设置绕过"，或提供一行 `xattr -cr "/Applications/Whisper Input.app"` 的命令，或未来走 Homebrew Cask 分发（cask 安装时会自动清 quarantine）
   - 本次不做，作为遗留项记在后续 TODO 里
3. **仅出 macOS arm64 和 Linux x86_64**：Intel Mac 不支持（项目已明确不再支持），Linux arm64 也不支持（当前没有用户需求）。
4. **deb 的 postinst 当前仍会 curl 装 uv + 跑 uv sync + 预下载 500MB 模型**：这导致 apt install 可能卡 5-10 分钟，且 "deb postinst 里 curl | sh" 本身也是反模式。**这个问题超出本次 CI 任务范围，已拆到下一个开发项 `9-Linux安装体验优化`**。
5. **release notes 靠 `gh release create --generate-notes` 自动生成**：基于 commit 历史，没有手工精修。如果未来发版需要更正式的 CHANGELOG 再考虑升级到 release-please 等工具。
6. **version-check 的"改了源码但忘 bump"只给 warning 不 fail**：刻意选择——避免在日常开发推进时 CI 被这种提示阻断。trade-off 是 warning 容易被忽略。

## 后续 TODO

- **`9-Linux安装体验优化`**（已新开分支 `feat/linux-install-ux`，PROMPT.md 已写好）：改造 `debian/postinst`，移除网络 curl 装 uv 的反模式，把依赖安装和模型下载延迟到首次启动时由 launcher 处理，让 `apt install` 秒级完成
- **考虑引入 `release-please`**：如果未来版本号 bump + CHANGELOG 维护变成负担，可以升级到基于 conventional commits 的全自动发版工具
- **macOS Gatekeeper 体验改进**：有三条路可以选：
  - 低成本：README 加醒目提示，或打包时在 dmg 里附带一个 `install.command` 自动跑 `xattr -cr` 清 quarantine
  - 中成本：走 Homebrew Cask 分发，cask 会自动处理 quarantine，用户 `brew install --cask whisper-input` 即可
  - 高成本：Apple Developer Program $99/年 + 证书 secrets 注入 workflow + `codesign` + `notarytool` 公证流程
- **Linux arm64 支持**：如果有用户需求，可以给 build-linux 加 `matrix.arch` 并用 `ubuntu-24.04-arm`（GitHub 已提供 arm runner）
- **加一个"装 deb 并运行"的完整端到端 smoke test**：现在只验证依赖能解析，没有真的跑起来；可以考虑在 docker 容器里装 deb + 启动应用 + 发送假的音频数据验证全链路
