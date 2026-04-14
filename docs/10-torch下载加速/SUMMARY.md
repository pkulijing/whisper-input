# 总结：Linux torch 安装加速 + 按 GPU 分流 CUDA / CPU wheel

## 开发项背景

两个叠加的问题:

1. **SJTU 镜像名存实亡**。`pyproject.toml` 之前用 `[[tool.uv.index]]` 接入 SJTU 的 PyTorch CUDA 镜像 `https://mirror.sjtu.edu.cn/pytorch-wheels/cu121`,实测 uv 对这种半 PEP 503 静态目录支持有缺陷,最终仍然回落到 `download.pytorch.org` 拉 wheel,国内首装一次 `uv sync` 动辄几十分钟。
2. **CUDA wheel 对无显卡机器是浪费**。CUDA 版 torch wheel 约 2 GB(内含 cuDNN / cuBLAS),CPU 版只有 200 MB 一个量级。装在没有 NVIDIA 显卡的 Linux 上虽然能跑(运行时由 `_select_device` 自动回落),但下载和占盘是纯浪费,对国内慢网用户尤其难受。

希望:Linux 首次安装时自动按 `nvidia-smi` 存在与否选择 CUDA / CPU 变体,wheel 全部走能稳定连通的国内镜像;macOS 行为完全不变。

## 实现方案

### 关键设计

1. **阿里云镜像替代 SJTU,但用 PEP 508 直链而不是 PyPI index**。已实测确认 `https://mirrors.aliyun.com/pytorch-wheels/{cu121,cpu}/` 提供 `torch-2.5.1+{cu121,cpu}-cp312-cp312-linux_x86_64.whl` 完整集合,但它只是静态目录列表、不符合 PEP 503,无法当作 `[[tool.uv.index]]`。所以走 PEP 508 direct reference,把 wheel URL 直接写进 `project.optional-dependencies` 的依赖项里(如 `torch @ https://...whl`),`+` 编码成 `%2B`。

2. **uv 官方推荐的 pytorch 变体模式:互斥 extras + `[tool.uv] conflicts`**。`cuda` / `cpu` 两个 extra 各自带一组直链依赖,带 `; sys_platform == 'linux'` marker 兜底防止 macOS 误装;`[tool.uv] conflicts = [[{extra="cuda"}, {extra="cpu"}]]` 强制二选一,避免 lockfile 同时锁两个 local version。

3. **macOS 的 torch 仍走主 `dependencies` 但加 darwin marker**。`torch==2.5.1; sys_platform == 'darwin'` / `torchaudio==2.5.1; sys_platform == 'darwin'` 留在主 deps 里,走默认 PyPI(清华镜像)拉 macOS cp312 wheel,与本次改动彻底解耦。

4. **`requires-python` 必须显式收紧到 `<3.13`**。新版 uv universal resolver 会为 `requires-python` 允许的所有 Python minor 版本生成解析 split,wheel URL 硬编码 `cp312` 在 `>=3.13` 的 split 里没有匹配 wheel 会让整体解析失败。配合 `.python-version` 钉死的 `3.12.13`,改成 `>=3.12.13,<3.13` 才能让 universal resolution 通过。

5. **GPU 检测放在安装期、不动运行期**(原则)。运行时的设备选择早就由 `stt_sensevoice.py:_select_device` 按 `cuda → mps → cpu` 自动挑,装哪种 wheel 它都能用对。所以本次改动主线集中在"依赖声明 + 安装脚本",运行期代码原则上不动 —— 唯一例外见关键设计 #6。

6. **`_select_device` 在 `import torch` 失败时立即抛错**(实施中发现的必要例外)。原实现是静默 fallback 到 `"cpu"`,导致 torch 没装上时日志先打印 `device=cpu`,接着下游 funasr 再炸 `ImportError: No module named 'torch'`,排查者会被 "device=cpu" 误导以为是 cpu 版 torch 装坏了。改成立即 `raise RuntimeError`,把根因(torch 没装上)直接暴露在 traceback 第一行。

7. **`setup.sh` 重命名为 `setup_linux.sh`**(用户指出的命名一致性 bug)。原来 `setup.sh` 对应 `setup_macos.sh`,反直觉。`git mv` 保留历史,顺手给脚本开头加 `uname == Linux` 守卫。

8. **DEB 路径上的 `setup_window.py:_stage_a_run` 同样需要分流**(实施中发现的设计盲点)。我在原 plan 里把"裸 `uv sync` Linux 下不会装 torch"标注为"局限,setup_linux.sh 是 canonical 入口",但完全漏掉了 DEB 用户首启时跑的 `_stage_a_run` 走的也是裸 `uv sync --python $PYTHON_VERSION`。结果 DEB 装出来的 user venv 没有 torch,首次启动 main.py funasr import torch 直接 ImportError。补丁:`detect_torch_variant()`(`nvidia-smi -L` + `TORCH_VARIANT` 环境变量覆盖,与 `setup_linux.sh` 行为对齐),`_stage_a_run` 把 `--extra <variant>` 加进 `uv sync`,并把 variant 纳入 `compute_deps_hash` 让旧的 `.deps_sha256` sentinel 自动失效以触发 stage A 重跑。

### 开发内容概括

| 文件 | 变动 |
|---|---|
| `pyproject.toml` | 删 `[[tool.uv.index]] pytorch-cu121` 和 `[tool.uv.sources] torch/torchaudio`;主 `dependencies` 的 torch / torchaudio 加 `; sys_platform == 'darwin'` marker;新增 `[project.optional-dependencies]` 的 `cuda` / `cpu` 两个互斥 extras,每个 extra 两条阿里云直链依赖(带 `sys_platform == 'linux'` marker);新增 `[tool.uv] conflicts`;`requires-python` 收紧到 `>=3.12.13,<3.13` |
| `setup.sh` → `setup_linux.sh` | `git mv` 改名(保留历史);开头加 `uname == Linux` 守卫;插入 GPU 检测逻辑(`nvidia-smi -L` + `TORCH_VARIANT` 环境变量覆盖),`uv sync` 改成 `uv sync --extra "$VARIANT"` |
| `setup_macos.sh` | 顺手修文档漂移 bug:`uv sync --group macos` → `uv sync`(pyproject 里根本没有 macos group,torch/torchaudio macOS 版本挂在主 deps 带 marker) |
| `debian/setup_window.py` | 新增 `detect_torch_variant()`;`_stage_a_run` 加 `--extra <variant>`;`compute_deps_hash` 纳入 variant |
| `stt_sensevoice.py:_select_device` | `import torch` 失败时改抛 `RuntimeError`,message 直接指向 `--extra cuda/cpu` 修法 |
| `CLAUDE.md` + `README.md` | 同步更新 Install dependencies 区块的命令,顺便修了 `--group linux-cuda` / `--group macos` 这两处早已对不上 pyproject 的文档漂移 |

### 额外产物

- 这一轮改动**不需要新增**任何测试脚本或调试工具 —— 验证完全靠跑 `uv sync --extra cuda/cpu` 后用 `import torch; print(torch.__version__)` 直接观察。
- 完整的端到端验证矩阵留在 `PLAN.md` 的「验证方法」一节里,可重复执行。
- 顺手暴露并修复了三处与本需求无关但同样存在的文档/命令漂移:
  - `CLAUDE.md` 的 `uv sync --group linux-cuda` / `uv sync --group macos`(pyproject 里没有这两个 group)
  - `setup_macos.sh:46` 的 `uv sync --group macos`(同上)
  - `setup.sh` 没有 `uname` 守卫

## 局限性

1. **wheel URL 硬编码 cp312 ABI 标签**。项目升 Python 到 3.11/3.13 会 404。短期靠 `requires-python = ">=3.12.13,<3.13"` + `.python-version` 双重锁住,长期需要脚本化按当前 Python 版本拼 URL。
2. **硬编码 torch 2.5.1**。以后升级 torch 需要同步改 5 处:`pyproject.toml` 中 macOS 主 deps 行 + cuda extra 两条 URL + cpu extra 两条 URL。集中可控但非自动化。
3. **仅覆盖 linux_x86_64**。ARM Linux 失配,当前项目本就不支持 ARM Linux,但风险点已经在注释里说明,未来若有人在 aarch64 上跑会立刻定位到。
4. **裸 `uv sync` 在 Linux 上得不到 torch**。已修复 `setup_linux.sh` + `debian/setup_window.py` 两个 canonical 入口,但开发者直接 `uv sync` 跑(不带 extra)仍然会装出无 torch 的 venv —— 文档已说明,未做强制守卫。
5. **GPU 检测只看 `nvidia-smi -L` 是否成功**。极端情况(驱动坏、卡是 AMD/Intel)可能误判,`TORCH_VARIANT` 环境变量留作兜底覆盖。
6. **阿里云镜像可用性不受我们控制**。若未来阿里云下架该目录,需要再换源,改动面集中在 `pyproject.toml` 的 4 条 URL。
7. **macOS 端到端回归本地未验**。本次开发机器只有 Linux + 4090,macOS 路径理论上不受影响(主 deps + darwin marker 没动过 + setup_macos.sh 走的是默认 `uv sync`),但没在真 Mac 上跑过 `bash setup_macos.sh` + `import torch; torch.backends.mps.is_available()`。
8. **DEB 重装路径用户侧未确认**。本机 `bash build.sh` 重建 DEB 成功 + dpkg-deb 内容核对 OK,但 `sudo dpkg -i` 重装 + 启动 whisper-input 走完整 stage A/B/C 这一步还需要用户手动确认通过。

## 后续 TODO

- **macOS 真机回归**:在 Mac 上 `rm -rf .venv && bash setup_macos.sh && uv run python -c "import torch; print(torch.backends.mps.is_available())"`,确认本次改动没误伤 macOS 路径。
- **DEB 升级路径冒烟**:用户在装好 0.3.2 的 DEB 上,启动 whisper-input,看 `~/.local/state/whisper-input/whisper-input.log` 里 stage A 是否正确选 `--extra cuda` 并成功装上 torch、stage C 主程序是否打印 `device=cuda` 而不是 `device=cpu`。
- **`requires-python` 升级方案**:未来若需要支持 Python 3.13,必须先确认阿里云镜像有 cp313 wheel,或改用程序化拼 URL 的方案。当前是"短期锁死",不是"长期解决"。
- **全局开发宪章 `~/.claude/CLAUDE.md` 的 pypi index 指南示例**:目前还推荐旧的 SJTU 镜像 + `[[tool.uv.index]]` 写法,既然这次实证不可靠,值得把示例更新成"阿里云 wheel 直链 + 互斥 extras"模式,避免后续新项目沿用旧范式再踩坑。**这一项是开发宪章级的修改,需要单独和用户确认后再执行,不并入本轮**。
- **DEB 命令行版的 GPU 检测和开发期保持同步**:目前 `setup_linux.sh` 和 `debian/setup_window.py` 各自实现了一份 `detect_torch_variant`(一份 bash、一份 python),逻辑刻意保持一致。将来若要扩展(比如根据 `lspci` 检测 AMD GPU 推荐 ROCm 变体),需要在两处同步改。可考虑抽成 Python 单文件 + 在 setup_linux.sh 里 `uv run python -m ...` 调用,但那样会引入"setup_linux.sh 在装 uv 之前跑不了"的鸡生蛋问题,目前两份独立实现的 trade-off 是合理的。
- **`_select_device` 抛 RuntimeError 后的失败处理路径**:目前 `main.py:preload_model` 里没有针对这个 RuntimeError 的 catch,会让 main.py 完全崩出来 —— 这正是设计意图(让根因暴露)。但如果未来想做"torch 缺失 → 弹窗引导用户重装"这类更友好的交互,需要在 `main.py` / `setup_window.py` 上层包一层。当前不做。
