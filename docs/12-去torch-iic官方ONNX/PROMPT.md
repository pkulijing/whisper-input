# 需求：去 torch 迁移 —— 用达摩院官方 ONNX 重写 STT 推理

## 核心痛点（本轮最首要解决的问题）

国内用户首次 `uv sync` 卡 40 分钟——大概率装不上。

根因是 `torch + torchaudio + funasr` 依赖巨大：
- macOS ~800 MB
- Linux CPU ~1.2 GB
- Linux CUDA ~2.8 GB

第 10 轮切到阿里云镜像只能缓解不能根治——`torch` 的间接依赖（`nvidia-*` CUDA runtime 系列、`triton`、`sympy` 预编译包等）在国内镜像没有完整副本，`uv` 解析器会**回退到 `pypi.org`**，一旦走跨境链路速度就崩。普通中国用户等于装不上。

## 开发目标

**把 STT 推理后端从 `FunASR + PyTorch` 迁移到"官方 ONNX + Microsoft onnxruntime"**，彻底移除 `torch/torchaudio/funasr` 依赖。

### 具体指标

| 维度 | 迁移前 | 迁移后 |
|---|---|---|
| 首包依赖下载 | 800 MB – 2.8 GB | ~20 MB |
| 首次模型下载 | ModelScope ~500 MB | ModelScope ~231 MB |
| `uv sync` 时长（国内） | 40 分钟或失败 | 几十秒 |
| 运行时依赖 | `torch + torchaudio + funasr` | `onnxruntime + kaldi-native-fbank + sentencepiece + numpy` |
| Linux GPU 支持 | cuda/cpu 双 extras | 无差异，统一 CPU ONNX |
| macOS/Linux 代码路径 | 有 cuda/mps/cpu 设备降级逻辑 | 一条 CPU 路径 |
| 识别质量 | SenseVoice-Small（FunASR） | **SenseVoice-Small（同一份权重）**，bit-aligned |
| 标点/ITN/大小写/语种 | 正确 | **必须同样正确** |

### 硬约束

- **零质量退化**：标点、反向文本规范化（ITN）、英文大小写、语种自动检测——**必须和原 FunASR 路径 bit-aligned**
- **不引入 HuggingFace 依赖**：HF 在国内不可靠，也不想教用户配 `hf-mirror.com`
- **不要求用户手动操作**：安装、首次启动、模型下载都要全自动
- **全国内直连**：模型下载走 ModelScope 官方源（CN-native CDN），不走 GitHub、不依赖 ghproxy 代理
- **STT 模块抽象**：保留可扩展性，为将来加 Fun-ASR-Nano / Qwen3-ASR 等后端留接口
- **Minimal 版为目标**：本轮交付 `uv sync` + 自动首次下载的开发者体验。完整版打包（`.app` / `.deb` 内置 Python + 模型）是第 13 轮的事

## 技术选型（经过多轮曲折后敲定）

### 最终方案

**模型：** 达摩院官方 `iic/SenseVoiceSmall-onnx` on ModelScope
- 文件：`model_quant.onnx` (230 MB) + `tokens.json` + `am.mvn` + `config.yaml`
- BPE tokenizer：`chn_jpn_yue_eng_ko_spectok.bpe.model` (368 KB)，来自姐妹仓库 `iic/SenseVoiceSmall`
- 由训练 SenseVoice 的达摩院 Speech Lab 自己量化和维护，被 FunASR 生产级 runtime SDK 使用（77 万次下载）

**推理框架：** Microsoft 官方 `onnxruntime` PyPI 包
- `kaldi-native-fbank`（Fangjun Kuang 独立维护的纯 C++ 特征提取库，和 sherpa-onnx 同作者但**独立包**、**macOS wheel 干净**）
- `sentencepiece`（Google 官方 BPE tokenizer）
- `numpy` + `PyYAML`

**特征提取与后处理代码：** 从达摩院官方 `funasr_onnx` 包 port
- `WavFrontend`（fbank + LFR + CMVN，位对齐训练时的 FunASR WavFrontend）
- `SentencepiecesTokenizer`（BPE 解码）
- `rich_transcription_postprocess`（meta 标签清理，emoji 渲染）

Port 而不是 pip 安装 `funasr_onnx` 的原因：`funasr_onnx` 同时还打包了 Paraformer / VAD / 标点恢复等其他模型，所以 setup.py 顶层依赖 `librosa + scipy + jieba`（librosa 触发 numba/llvmlite 一大坨编译器基础设施，jieba 是给 CT-Transformer 标点模型用的，scipy 源码里 0 处 import 是个死依赖）。**这些对 SenseVoice 推理完全没用**。我们只 port 真正用到的 ~250 行纯 Python（三个类 + 常量表），依赖树保持干净（只新增 `sentencepiece`，一个 1.5 MB 官方 wheel）。

### 被排除的路线

- **whisper.cpp + 中文 Whisper**：中文精度明显差（AISHELL-1 CER 3% → 7–8%），且有短音频幻觉问题
- **Qwen3-ASR / Qwen-Omni**：参数量 800M–30B，CPU 延迟做不到"按住说话松开即出字"
- **Rust / C++ 重写整个项目**：STT 推理只占项目 10% 代码量，90% 价值在跨平台 OS 集成、权限对接、打包分发这些"脏活"，换语言后这些都要重写且无收益
- **sherpa-onnx Python 封装（`sherpa-onnx` PyPI 包）**：macOS arm64 wheel 有打包 bug（没把 `libonnxruntime.*.dylib` 打进去，`import sherpa_onnx` 直接崩）
- **sherpa-onnx 的 int8 ONNX 模型（第三方封装）**：实测质量严重退化——无标点、无 ITN、英文全大写拼写错、语种识别失败
- **sherpa-onnx 的 fp32 ONNX 模型（894 MB）**：正确但体积是 iic 官方 quant 的 4 倍
- **自己做 int8 量化**：iic 官方已经做了，自己做大概率不如官方做的好
- **fp16 转换**：`onnxconverter_common` 转完有 Cast 节点 type 错误，要手工修 ONNX graph，收益不稳

## 不在本轮范围

- **完整版打包分发**（把 Python runtime + 依赖 + 模型打进 `.app` / `.deb`）：第 13 轮做
- **新增 Fun-ASR-Nano / Qwen3-ASR 后端**：只做抽象预留，不实际实现
- **设置页"模型选择"下拉框**：后端抽象支持，但 UI 不加
- **Intel Mac 实机测试**：保留现状不破坏

## 验收标准

1. **`uv sync` 在 macOS 和 Linux 都能 30 秒内完成**，无任何包回退到 `pypi.org`
2. **`uv pip list` 里没有 torch / torchaudio / funasr / sherpa-onnx**
3. **首次启动从 ModelScope 自动下载 ~231 MB 模型**，国内 CDN 直连无需代理
4. **一次下载后永久离线可用**，第二次启动零联网
5. **中/英/日/韩/粤五语种识别质量和原 FunASR 完全等价**（含标点、ITN、大小写、语种检测）
6. **`stt/` 模块抽象清晰**：接口 load + transcribe，后续加新后端是"新增一个文件 + dispatcher 加分支"
7. **两端代码路径统一**：没有 CUDA / MPS / 设备降级逻辑
8. **`setup_linux.sh` 简化**：不再 `nvidia-smi` 检测，不再分 cuda/cpu 变体
9. **文档同步**：`CLAUDE.md`、`README.md`、两个 `setup_window.py`、`build.sh` 与新架构一致

## 开发模式

全局 CLAUDE.md 的四步法：
1. ✅ 需求（本文档）
2. ✅ 计划（`PLAN.md`）
3. ⏭️ 执行
4. ⏭️ 总结（`SUMMARY.md`，含完整决策链路和曲折过程的诚实记录）

开发在独立 git worktree 中进行，分支 `worktree-sherpa-onnx-migration`（分支名有历史原因，实际最终方案不用 sherpa-onnx，但不重命名避免额外工作）。
