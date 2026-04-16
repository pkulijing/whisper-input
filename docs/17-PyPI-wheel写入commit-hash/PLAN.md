# 实现计划

## 方案

使用 hatchling 的自定义构建钩子（custom build hook），在 `uv build` 时自动将 git commit hash 写入 `_commit.txt` 并打包进 wheel。

## 关键设计

- hatchling 默认遵循 `.gitignore` 排除文件，而 `_commit.txt` 在 `.gitignore` 中，所以需要通过 `build_data["force_include"]` 强制包含
- `initialize` 阶段写入文件，`finalize` 阶段清理，避免残留
- 如果不在 git 仓库中（如在 CI 的 shallow clone 或非 git 环境），静默跳过，不影响构建

## 实施步骤

### 1. 新建 `hatch_build.py`（项目根目录）

hatchling 约定：项目根目录下的 `hatch_build.py` 自动被识别为自定义构建钩子。

```python
class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # git rev-parse HEAD → 写入 src/whisper_input/_commit.txt
        # build_data["force_include"] 强制包含（绕过 .gitignore）

    def finalize(self, version, build_data, artifact_path):
        # 清理 _commit.txt
```

### 2. 修改 `pyproject.toml`

添加一行启用自定义钩子：

```toml
[tool.hatch.build.hooks.custom]
```

### 3. 验证

```bash
cd /Users/jing/Developer/whisper-input
uv build
# 检查 wheel 内是否包含 _commit.txt
unzip -l dist/*.whl | grep _commit
```
