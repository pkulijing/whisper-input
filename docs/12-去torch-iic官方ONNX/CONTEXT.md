# 开发上下文快照（重启锚点）

> 本文档是为了**让任何时候接手这个开发项的人（包括未来的我自己）能在 5 分钟内恢复上下文**。
> 详细背景见 `PROMPT.md`，详细执行计划见 `PLAN.md`。本文档只记录**已完成**和**待做**。

## 一句话

**把 STT 从 `torch + FunASR` 迁到 `onnxruntime + 达摩院官方 ONNX`，解决国内 `uv sync` 卡 40 分钟装不上的问题。**

## 最终架构（已敲定）

```
模型       = iic/SenseVoiceSmall-onnx on ModelScope  (230 MB 官方量化,正确性和 FunASR bit-aligned)
推理       = Microsoft onnxruntime  (纯 Python 包,clean wheel)
特征提取   = funasr_onnx 的 WavFrontend  (port 进来,~120 行)
BPE 解码   = funasr_onnx 的 SentencepiecesTokenizer  (port 进来,~40 行,需要 sentencepiece pip 包)
后处理     = funasr_onnx 的 rich_transcription_postprocess  (port 进来,~100 行)
分发       = ModelScope API 直连,5 个文件,国内 CDN,不需要 GitHub/ghproxy/HF mirror
```

**去掉的东西**：`torch`、`torchaudio`、`funasr`、`sherpa-onnx` Python 包、所有 cuda/cpu/mps 分支、手工 GitHub release 上传。

## 技术决策的曲折历史（节选）

1. 原本想用 sherpa-onnx 官方 int8 (166 MB) → **实测质量崩**（无标点、无 ITN、英文 ALL CAPS）
2. 曾想过加 `ctypes.CDLL` 预加载 dylib 绕 sherpa-onnx PyPI wheel 的 macOS 打包 bug → 可行但丑
3. 跟着 sherpa-onnx 的 `scripts/sense-voice/test.py` 自己抄了一版纯 numpy → **发现 test.py 里 LFR 实现和 FunASR 不一致**，features 有 0.648 max_abs 偏差
4. 自己用 `torch.onnx.export` 从 FunASR `.pt` 导了一份 fp32 ONNX (894 MB) → 正确但太大
5. 试 fp16 转换 (448 MB) → `onnxconverter_common` 留下 Cast 节点 type 错误，要手工修 graph
6. **用户问**："达摩院官方有没有 ONNX 版本？" → 发现 `iic/SenseVoiceSmall-onnx` 和 `funasr_onnx` pip 包
7. **用户问**："为什么官方依赖 jieba/scipy？" → 确认那些是给其他模型（Paraformer + CT-Transformer 标点）用的，SenseVoice 用不上
8. **敲定**：port funasr_onnx 的三个类，不 pip 安装（避免 librosa/scipy/jieba 脏依赖）

详见 `PROMPT.md`"被排除的路线" + `PLAN.md`"对比原始 PLAN 的变化"。

## 目前的代码状态

### 已完成

- `stt/__init__.py`（懒加载工厂 `create_stt`）
- `stt/base.py`（`BaseSTT` 抽象基类）
- `stt/model_paths.py`（基于 sherpa-onnx tar.bz2 的旧版本 —— **需要重写**为 iic 5 文件 manifest）
- `stt/downloader.py`（基于 ghproxy failover 的旧版本 —— **需要重写**为 ModelScope 多文件直链）
- `stt/sense_voice.py`（基于手抄 sherpa-onnx test.py 的旧版本 —— **需要重写**为 WavFrontend + Tokenizer）
- `model_state.py`（兼容壳，透明透传到 `stt/model_paths`，不需要改）
- `pyproject.toml`（已去掉 torch/torchaudio/funasr，需要**加 sentencepiece**）
- `main.py`（`create_stt_engine` 已接到 `stt.create_stt`，不需要改）
- `config_manager.py` / `config.example.yaml`（`device_priority` 已删，`use_itn` 已加）
- `setup_linux.sh` / `setup_macos.sh`（已简化，文案需要把"160 MB"改成"231 MB"）
- `macos/setup_window.py` / `debian/setup_window.py`（stage B 已改走 `stt.downloader.download_model()`，接口不变所以 downloader 内部重写后自动跟上）
- `build.sh`（`SOURCE_STT` 列表需要加 3 个新文件）

### 待做

见 TaskList #20–#27。核心是 port 三个工具 + 重写 model_paths/downloader/sense_voice 三个核心文件 + 冒烟测试 + 文档同步。

## 关键外部资源

| 名称 | URL | 用途 |
|---|---|---|
| iic/SenseVoiceSmall-onnx | modelscope.cn/models/iic/SenseVoiceSmall-onnx | 主模型 + tokens.json + am.mvn + config.yaml |
| iic/SenseVoiceSmall | modelscope.cn/models/iic/SenseVoiceSmall | BPE 模型 `chn_jpn_yue_eng_ko_spectok.bpe.model` 来源（姐妹 PyTorch 仓库）|
| funasr_onnx 源码 | github.com/modelscope/FunASR/tree/main/runtime/python/onnxruntime/funasr_onnx | port 来源，MIT 协议 |
| 测试音频 | /tmp/funasr_zh.wav | 真人中文单句，FunASR 期望输出 `欢迎大家来体验达摩院推出的语音识别模型。`（**有句号**）|

## 本地环境状态

- **Worktree**：`/Users/jing/Developer/whisper-input/.claude/worktrees/sherpa-onnx-migration/`，分支 `worktree-sherpa-onnx-migration`
- **`.venv` 已建**，有 `onnxruntime + kaldi-native-fbank + numpy + sounddevice + pynput + ruff + ...`，还没有 `sentencepiece`
- **模型缓存**：`~/Library/Application Support/Whisper Input/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/` 目前还有**旧 int8 模型**，开发过程中需要替换为新目录 `iic-SenseVoiceSmall-onnx/`
- **临时文件**：
  - `/tmp/iic_onnx/` — iic 官方模型 5 个文件（测试中下载的）
  - `/tmp/my_export_fp32.onnx` — 我自己导出的 fp32 版本（894 MB，开发结束可删）
  - `/tmp/funasr_feats.npy`、`/tmp/my_features.npy` — 调试用特征 dump
  - `/tmp/funasr_zh.wav` — 真人测试音频
  - `/tmp/funasr_onnx_*.py` — port 来源文件副本

## 废弃物清理

开发完成后要删除的东西：

1. **GitHub release `models-v1` 草稿**（`pkulijing/whisper-input`）—— 本来是要上传 sherpa-onnx int8 tar.bz2 的，现在整个方案不用 GitHub release 分发了。需要和用户确认后删除。
2. **/tmp 下的调试产物**（上面列的临时文件）
3. **本轮开发过程中在主仓库 `.venv` 里临时装的 `onnx` 包**（通过 `uv run --with onnx`，是覆盖层不污染 pyproject.toml，应该已经自动清理）

## 触发开发继续的命令

```bash
cd /Users/jing/Developer/whisper-input/.claude/worktrees/sherpa-onnx-migration
# 阅读:
cat docs/12-去torch-iic官方ONNX/PROMPT.md  # 需求
cat docs/12-去torch-iic官方ONNX/PLAN.md    # 计划
cat docs/12-去torch-iic官方ONNX/CONTEXT.md # 本文档(快照)

# 核心任务清单:Task #20–#27(ruff/冒烟/文档/删草稿/写 SUMMARY)
```
