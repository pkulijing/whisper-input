# PLAN：GitHub CI 与版本管理

## 一、最终方案概览

- **触发**：push 到 `master` 和 `ci-bootstrap`（调试用）+ `workflow_dispatch` 手动兜底
- **构建矩阵**：macOS arm64 (`macos-14`) + Linux x86_64 (`ubuntu-24.04`)，**显式版本不用 latest**
- **额外 lint**：ruff check，~10s，拦截明显风格/语法问题
- **版本号管理**：方案 B + 半自动 tag 发版
  - `pyproject.toml` 仍是版本号唯一来源
  - CI 检测「源码改了但版本号没动」→ warning 提醒（不 fail）
  - CI 检测「版本号变了且无对应 tag」→ 自动打 tag、创建 GitHub Release、上传产物到 Release
- **产物分发**：
  - 日常构建：上传到 Actions Artifact，保留 30 天，登录 GitHub 在 Actions 页面可下载
  - 发版构建：额外上传到 GitHub Release，永久保留，公开下载链接
- **缓存**：`actions/cache` 复用 `build/macos/cache/` 下的 python-build-standalone tarball
- **README**：加 CI 状态 badge

## 二、Runner 版本固定的理由

**不使用 `ubuntu-latest` / `macos-latest`**，原因：
- `latest` 会随时间漂移，某天悄悄升到新版本，构建行为静默变化
- macOS 固定 `macos-14`（Sonoma, arm64），避免 hdiutil/sips 行为漂移

**Linux 为什么选 `ubuntu-24.04` 而不是更老的版本**：
- 项目 `debian/control` 的 `Depends` 字段声明了 `libgirepository-2.0-dev`（pygobject 3.54+ 运行时依赖 girepository-2.0，来自 glib 2.80+）
- 这个包在 Ubuntu 仓库里只有 24.04 及以后才有；22.04/20.04 根本装不了
- 因此项目"真实最低支持版本"本来就是 Ubuntu 24.04，CI 用 24.04 = 和真实 min 对齐
- 用 22.04 会制造"CI 绿但用户装不了"的盲区
- deb 本身不含 native 编译产物（纯 python + 配置），runner 的 glibc 版本对最终产物无影响，所以 glibc 向前兼容担忧在本项目不成立

## 三、deb 依赖冒烟测试

build-linux job 在打包完成后增加一步：

```bash
sudo apt-get update -qq
sudo apt-get install --simulate -y "$PWD/build/deb/*.deb"
```

用 apt 的依赖解析器验证 deb 的 `Depends` 字段能在 runner 上被满足。**不实际安装**（`--simulate`），避免触发 postinst 里的 `uv sync` 拉几 GB 的 torch。

作用：以后谁改了 `debian/control` 加了个系统上没有的包，CI 立刻会红，不会等到用户装机时才发现。

## 四、Workflow 文件设计

新建唯一 workflow 文件：`.github/workflows/build.yml`

### Job 0: `lint`（ubuntu-24.04）

1. checkout
2. 安装 uv
3. `uv run ruff check .`

失败则 fail，但不阻断 build job（用 `needs` 时不让 build 依赖 lint，保持并行）。

### Job 1: `version-check`（ubuntu-24.04）

职责：解析当前版本号、判断是否为「发版 push」、产出供后续 job 消费的输出。

步骤：
1. `actions/checkout@v4`，`fetch-depth: 0`，`fetch-tags: true`（需要历史和 tag 才能比较）
2. 用一行 grep 提取 `pyproject.toml` 的 version：
   ```bash
   VERSION=$(grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')
   ```
3. 检查 `v$VERSION` tag 是否已存在于本地（`git rev-parse -q --verify "refs/tags/v$VERSION"`）
4. 检查本次 push 是否动了源码（`git diff --name-only ${{ github.event.before }} ${{ github.sha }}` 看是否含 `*.py` / `build.sh` / `debian/` / `macos/` / `pyproject.toml`）
5. 决策：
   - tag 已存在 + 源码有改动 + 版本号没变 → `::warning::` 提示「忘记改版本号了」
   - tag 不存在 + 版本号是新值 → `is_release=true`
   - 其它情况 → `is_release=false`
6. 通过 `$GITHUB_OUTPUT` 输出 `version`、`is_release`

### Job 2: `build-macos`（macos-14，needs: version-check）

1. checkout
2. 安装 uv（官方一行脚本）
3. **缓存**：用 `actions/cache@v4` 缓存 `build/macos/cache/`，key 用 `macos/python_dist.txt` 的 hash
   ```yaml
   key: macos-pydist-${{ hashFiles('macos/python_dist.txt') }}
   ```
4. `bash build.sh`
5. `actions/upload-artifact@v4` 上传 `build/macos/*.dmg`，名为 `WhisperInput-macos-${version}`，保留 30 天

### Job 3: `build-linux`（ubuntu-24.04，needs: version-check）

1. checkout
2. 安装 uv
3. `bash build.sh`（Linux 分支不需要 python-build-standalone，无缓存需求）
4. upload-artifact 上传 `build/deb/*.deb`，名为 `WhisperInput-linux-${version}`，保留 30 天

注：CI 上的 deb 构建是否要跑 `uv sync`？看了 build.sh 的 Linux 分支只是文件复制 + dpkg-deb，不需要 python 环境，纯 bash + dpkg-deb 即可。`dpkg-deb` ubuntu-latest 自带。**不需要 uv，可以省掉那一步**。

### Job 4: `release`（needs: [build-macos, build-linux]，if: is_release == true）

1. 用 `actions/download-artifact@v4` 下载两个 artifact 到 `dist/`
2. `gh release create v$VERSION dist/**/*.dmg dist/**/*.deb --generate-notes --title "v$VERSION"`
3. 需要 `permissions: contents: write`

## 五、关键代码片段（写到 yaml 里的命令，不另起脚本）

```yaml
# version-check job 核心逻辑
- name: Resolve version & detect release
  id: ver
  run: |
    VERSION=$(grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')
    echo "version=$VERSION" >> "$GITHUB_OUTPUT"

    if git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null; then
      TAG_EXISTS=1
    else
      TAG_EXISTS=0
    fi

    BEFORE='${{ github.event.before }}'
    if [ -z "$BEFORE" ] || [ "$BEFORE" = "0000000000000000000000000000000000000000" ]; then
      CHANGED_SRC=1
    else
      if git diff --name-only "$BEFORE" "${{ github.sha }}" \
         | grep -E '\.py$|^build\.sh$|^debian/|^macos/|^pyproject\.toml$' >/dev/null; then
        CHANGED_SRC=1
      else
        CHANGED_SRC=0
      fi
    fi

    if [ "$TAG_EXISTS" = "1" ] && [ "$CHANGED_SRC" = "1" ]; then
      echo "::warning::源码有改动但 pyproject.toml 的版本号 ($VERSION) 已经发过 tag，记得 bump 版本号"
      echo "is_release=false" >> "$GITHUB_OUTPUT"
    elif [ "$TAG_EXISTS" = "0" ]; then
      echo "is_release=true" >> "$GITHUB_OUTPUT"
    else
      echo "is_release=false" >> "$GITHUB_OUTPUT"
    fi
```

## 六、不在本次范围内的事

- 不引入 commitizen / release-please 等工具
- 不做 PR 触发的 CI（只 push master，符合需求）
- 不做代码签名 / 公证（macOS DMG 仍是未签名状态，跟现状一致）
- 不做 ruff lint job（可以加但不是本次需求；如果你要可以顺手加）
- 不出 Intel macOS、不出 arm64 Linux

## 七、改动文件清单

新增：
- `.github/workflows/build.yml`
- `docs/8-GitHub CI与版本管理/PROMPT.md`（已建）
- `docs/8-GitHub CI与版本管理/PLAN.md`（本文件）
- `docs/8-GitHub CI与版本管理/SUMMARY.md`（开发后写）

修改：
- `README.md`：顶部加 CI badge，发版章节简单写明「改 pyproject.toml 版本号 → push → 自动 release」

不动：
- `build.sh`（CI 直接复用）
- `pyproject.toml`（版本号此次不动）
- 任何源码文件

## 八、验证计划：ci-bootstrap 调试分支约定

GitHub Actions 没办法本地完整模拟（`act` 工具不支持 macOS runner），调试只能 push 后看 run 结果。为了不污染 master：

1. workflow 的 `on.push.branches` 同时包含 `master` 和 `ci-bootstrap`
2. 调试时新建 `ci-bootstrap` 分支，反复 push 修 yaml，**但 release job 加 `if: github.ref == 'refs/heads/master'` 守卫**，确保调试分支不会误发 release
3. 调通后再 merge 到 master，触发首次正式 run（创建 `v0.3.0` Release）
4. 后续日常调 CI 仍走 ci-bootstrap 分支，约定写进 SUMMARY.md

验证步骤：
1. 本地用 `act` 或纯人工 review yaml 语法（act 跑 macos job 不现实，主要校验 yaml 合法性 + bash 逻辑）
2. push 到 master 后实际观察一次 run：
   - version-check job 应输出当前版本 `0.3.0`，因为 `v0.3.0` tag 不存在 → `is_release=true`
   - 两个 build job 跑通，各产出一份 artifact
   - release job 创建 `v0.3.0` Release
3. 第二次 push（不改版本号）应只跑 build，不跑 release，且 version-check 给出 warning

## 九、风险与回退

- **决策已定**：0.3.0 作为首个正式 GitHub Release，CI 调通后第一次推 master 时自动创建
- **风险 2**：macOS runner 首次运行无缓存，下载 python-build-standalone ~30MB，耗时多 10–30s，可接受
- **风险 3**：`gh release create` 需要 `GITHUB_TOKEN` 有 `contents: write` 权限，已在 workflow 里显式声明
- **回退**：删除 `.github/workflows/build.yml` 即可，无任何源码污染
