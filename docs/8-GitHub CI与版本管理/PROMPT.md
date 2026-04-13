# GitHub CI 与版本管理

## 背景

项目目前没有任何 CI，所有构建都在本机手动执行：
- macOS：`bash build.sh` 产出 `build/macos/WhisperInput_<version>.dmg`
- Linux：`bash build.sh` 产出 `build/deb/whisper-input_<version>.deb`

版本号定义在 `pyproject.toml` 的 `version` 字段，是项目唯一来源（`build.sh`、`version.py` 都从这里读）。痛点是发版前经常忘记改版本号。

## 需求

利用 GitHub 免费的 CI 能力，搭建一套规范的发布流水线：

1. **触发**：push 到 `master` 分支时自动触发 CI 构建。
2. **产物**：自动构建出 macOS 版 (`.dmg`) 和 Linux 版 (`.deb`)，可以在 GitHub 页面下载。
3. **版本号管理**：希望规范化、自动化，至少在忘记改版本号时给出提示。需要先调研「一般项目是怎么做的」，再选定方案。
4. **免费**：必须在 GitHub Free 账号免费额度内。需要先调研免费额度的限制（构建分钟数、产物大小、保留时长、并发等），方案要在限制之内。

## 交付物

- `.github/workflows/` 下的 workflow 文件
- 版本号管理机制（具体形式由 PLAN 决定）
- README 中补充 CI 状态徽章和发版说明（如果方案需要）
- `SUMMARY.md` 总结实现与遗留问题
