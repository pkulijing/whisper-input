# 实现计划:Linux torch 切阿里云直链 + 按 GPU 分流 CUDA/CPU

## 核心原则

**只动安装期,不动运行期**。改动集中在依赖声明 + 安装脚本 + 文档,Python 运行时代码一行不改 —— 运行时的设备选择早已由 `stt_sensevoice.py:_select_device` 按 `cuda → mps → cpu` 自动挑,装哪种 wheel 它都能用对。

## 技术选型

用 uv 官方推荐的 pytorch 变体模式:

- **互斥 extras**:`project.optional-dependencies` 里声明 `cuda` / `cpu` 两个 extra,每个 extra 用 PEP 508 直链依赖(`torch @ https://...whl`)写明阿里云 wheel URL。
- **`[tool.uv] conflicts`** 强制二选一,防止同时启用两个 extra 把两种 local version 都锁进 lockfile。
- **`; sys_platform == 'linux'`** marker 保证即使在 macOS 下误传 `--extra cuda` 也会被 marker 过滤,不会触发 Linux wheel 下载。
- macOS 的 torch / torchaudio 仍放在主 `dependencies` 下(带 `; sys_platform == 'darwin'` marker),走默认 PyPI(清华镜像)解析 macOS cp312 wheel。

### 为什么不用 `[tool.uv.sources]` + url

sources 也支持 `extra = ...` 条件,但那样需要主 `dependencies` 留 `torch==2.5.1`,Linux 靠 sources 的 extra + marker 叠加覆盖,两头呼应阅读成本高。PEP 508 直链写在 optional-dependencies 里,每条 dep 自带 URL、marker、extra,更直白。

## 已确认事实

阿里云镜像存在所有需要的 wheel:

- `cu121/torch-2.5.1+cu121-cp312-cp312-linux_x86_64.whl`
- `cu121/torchaudio-2.5.1+cu121-cp312-cp312-linux_x86_64.whl`
- `cpu/torch-2.5.1+cpu-cp312-cp312-linux_x86_64.whl`
- `cpu/torchaudio-2.5.1+cpu-cp312-cp312-linux_x86_64.whl`

项目 Python 版本被 `requires-python = ">=3.12.13"` + `.python-version` 双重锁定在 3.12.x,cp312 ABI 标签匹配。

## 改动清单

### 1. `pyproject.toml`

- 删 `[[tool.uv.index]] pytorch-cu121`
- 删 `[tool.uv.sources] torch/torchaudio`
- 主 `dependencies` 的 torch/torchaudio 加上 `; sys_platform == 'darwin'` marker
- 新增 `[project.optional-dependencies]` 的 `cuda` / `cpu` 两个 extras,每个 extra 两条 `torch @ URL` / `torchaudio @ URL` 直链依赖(带 `; sys_platform == 'linux'` marker)
- 新增 `[tool.uv] conflicts = [[{extra="cuda"}, {extra="cpu"}]]`
- URL 里的 `+` 必须编码为 `%2B`

### 2. `setup_linux.sh`

第 4 步 `uv sync` 之前插入 GPU 检测:

- 优先读环境变量 `TORCH_VARIANT`(允许手动覆盖)
- 否则 `command -v nvidia-smi && nvidia-smi -L` 判断,有 → `cuda`,无 → `cpu`
- 最终执行 `uv sync --extra "$VARIANT"`

更新旁边的注释说明新的下载渠道。

### 3. `CLAUDE.md`

Install dependencies 区块现在写的 `uv sync --group linux-cuda` / `uv sync --group macos` 跟实际 `pyproject.toml` 对不上(早已漂移),顺手更正:

```
# Install dependencies (Linux)
bash setup_linux.sh               # 推荐,自动按是否有 NVIDIA GPU 选 cuda/cpu
# or manually:
uv sync --extra cuda
uv sync --extra cpu

# Install dependencies (macOS)
bash setup_macos.sh
# or manually:
uv sync
```

## 验证

1. **CUDA 路径**:`rm -rf .venv uv.lock && bash setup_linux.sh`,日志出现"检测到 NVIDIA GPU",下载 URL 出现 `mirrors.aliyun.com/pytorch-wheels/cu121/`,**不出现** `download.pytorch.org` / `mirror.sjtu.edu.cn`。验证 `torch.__version__ == '2.5.1+cu121'` 且 `torch.cuda.is_available() is True`。
2. **CPU 路径**:`rm -rf .venv uv.lock && TORCH_VARIANT=cpu bash setup_linux.sh`,下载 `pytorch-wheels/cpu/...`,`torch.__version__ == '2.5.1+cpu'`。
3. **冒烟**:`uv run python main.py --no-tray --no-preload`,两种变体都能启动。
4. **ruff**:`uv run ruff check .` 无新增告警。
5. **macOS 回归**(人工在另一台机器):`bash setup_macos.sh` 仍能装上 torch 且 `torch.backends.mps.is_available()` 为 True。

## 实施过程发现的两个补丁(plan 阶段没考虑到)

1. **DEB 安装路径 `debian/setup_window.py` 也是裸 `uv sync` 的受害者**。原 plan 标注的"局限"只点到了开发 venv,完全没意识到 DEB 用户首启时跑的 `_stage_a_run` 也是裸 `uv sync --python $PYTHON_VERSION`,装出来的 user venv 没有 torch,首次启动 main.py 时 funasr `import torch` 直接 ImportError。修复:`debian/setup_window.py` 新增 `detect_torch_variant()`(`nvidia-smi -L` + `TORCH_VARIANT` 环境变量覆盖),`_stage_a_run` 把 `--extra <variant>` 加进 `uv sync` 命令,并把 variant 纳入 `compute_deps_hash` 让旧的 `.deps_sha256` sentinel 自动失效以触发重装。

2. **`stt_sensevoice.py:_select_device` 在 `import torch` 失败时静默返回 `"cpu"`**,导致日志先打印 `device=cpu`,下游 funasr 再炸同样的 ImportError —— 误导排查方向("以为 cpu 版 torch 装坏了")。改为立即抛 `RuntimeError` 并在 message 里直接指出"用 `--extra cuda/cpu` 重装"。这次 DEB 报错就是被这个误导日志卡了一阵子。

3. **`requires-python` 必须收紧到 `<3.13`**(已落地)。新版 uv 的 universal resolver 会为 `requires-python` 允许的所有 Python minor 版本生成解析 split,我们的 wheel URL 硬编码 `cp312`,在 `>=3.13` 的 split 里没有匹配 wheel → 整个解析失败。这条原 plan 标记成"短期安全的局限",实际开发中真踩到了,必须显式 `>=3.12.13,<3.13`。

## 局限 / 后续 TODO

- wheel URL 硬编码 cp312 ABI 标签;升级 Python 到 3.13 会 404,短期靠 `.python-version` 锁住。
- 硬编码 torch 2.5.1;升级 torch 要同步改 `pyproject.toml` 中 5 处(macOS 行 + 4 条 URL)。
- 仅覆盖 linux_x86_64;ARM Linux 失配,当前项目也不支持。
- Linux 下裸跑 `uv sync`(不带 extra)得不到 torch;setup_linux.sh 是 canonical 入口,文档已说明。
- GPU 检测只看 `nvidia-smi -L` 是否成功,极端情况(驱动坏)可能误判,`TORCH_VARIANT` 兜底。
- 阿里云镜像可用性不受我们控制,若下架需要再换源。
