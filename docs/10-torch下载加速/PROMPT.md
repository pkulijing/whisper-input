# 需求:Linux 下 torch 安装加速 + 按 GPU 分流

## 背景

现在 `pyproject.toml` 用 `[[tool.uv.index]]` 接入了 SJTU 的 PyTorch CUDA 镜像,但实测并没有效果 —— uv 最终仍会从 PyTorch 官方源下载 wheel,国内首装 `uv sync` 几十分钟起。

我刚发现阿里云有一个 `https://mirrors.aliyun.com/pytorch-wheels/cu121/` 页面,上面有我们需要的 wheel,问题是这不是一个标准的 PyPI 源,不能直接当 `[[tool.uv.index]]`。所以需要改变 Linux 环境下 torch 的安装方式,**直接从这个页面上下载 wheel 文件**,而不是通过 uv 的 index 机制去同步。对 macOS 需要保持不变。

## 叠加需求:按 GPU 存在与否分流

还要考虑一个问题:假如这台机器压根没有 NVIDIA 显卡,是不是应该装 CPU 版的 torch?

注意 —— 这个区别**只影响第一次安装**,运行期完全不需要感知是哪种变体。装 GPU 版本其实是可以兼容只有 CPU 的情况的(运行时会自动回落),但问题是:

- CUDA wheel 约 2 GB(内含 cuDNN / cuBLAS)
- CPU wheel 约 200 MB

在网速有限的情况下,没显卡的机器装 CPU 版本能省一个量级的下载量和磁盘占用。所以希望 Linux 首装时自动检测有没有 NVIDIA GPU,自动选择合适的变体。
