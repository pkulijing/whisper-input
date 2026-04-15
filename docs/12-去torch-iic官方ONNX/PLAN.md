# PLAN: 去 torch 迁移 —— iic 官方 ONNX + funasr_onnx port

## 背景 & 最终方向

经过多轮曲折调研（见 `PROMPT.md` "被排除的路线"部分），敲定的最终方案：

- **模型**：达摩院官方 `iic/SenseVoiceSmall-onnx` on ModelScope
- **推理**：Microsoft `onnxruntime` PyPI 包
- **特征提取 + 解码 + 后处理**：从达摩院官方 `funasr_onnx` Python 包里 **port 三个类 ~250 行**（不 pip 安装 funasr_onnx 以避免带入 librosa/scipy/jieba 等无关重依赖）

## 目录结构

```
新增:
  stt/_wav_frontend.py       # port 自 funasr_onnx/utils/frontend.py (WavFrontend + load_cmvn)
  stt/_tokenizer.py          # port 自 funasr_onnx/utils/sentencepiece_tokenizer.py
  stt/_postprocess.py        # port 自 funasr_onnx/utils/postprocess_utils.py (rich_transcription_postprocess 及相关常量)

重写:
  stt/model_paths.py         # 5 文件 manifest, ModelScope URL, 分别 SHA256
  stt/downloader.py          # 逐文件下载(非 tar.bz2), 不需要 ghproxy failover
  stt/sense_voice.py         # 用 WavFrontend + Tokenizer + postprocess, 加载 model_quant.onnx

保持(前面已经写好了):
  stt/__init__.py            # create_stt 工厂, 懒加载
  stt/base.py                # BaseSTT 抽象基类

删除(本轮应该没有)

不变:
  main.py / config_manager.py / config.example.yaml / build.sh /
  setup_linux.sh / setup_macos.sh / macos/setup_window.py / debian/setup_window.py
  model_state.py (兼容壳, 通过 stt.model_paths 的 find_local_model/save_manifest 自动跟上)
```

## 模型分发

**全部从 ModelScope API 直接拉**，国内 CDN 直连，不需要 GitHub、不需要 ghproxy、不需要用户手动上传。

### 5 个文件

| 文件 | 来源仓库 | 大小 | 用途 |
|---|---|---|---|
| `model_quant.onnx` | `iic/SenseVoiceSmall-onnx` | 230 MB | 推理模型 |
| `tokens.json` | `iic/SenseVoiceSmall-onnx` | 344 KB | vocab 列表（可选,SentencepieceTokenizer 可能不需要）|
| `am.mvn` | `iic/SenseVoiceSmall-onnx` | 11 KB | CMVN 归一化参数 |
| `config.yaml` | `iic/SenseVoiceSmall-onnx` | 1.8 KB | frontend_conf 参数（fs, lfr_m, lfr_n 等）|
| `chn_jpn_yue_eng_ko_spectok.bpe.model` | `iic/SenseVoiceSmall` (PyTorch 版仓库) | 368 KB | SentencePiece BPE model |

### URL pattern

```
https://www.modelscope.cn/api/v1/models/iic/SenseVoiceSmall-onnx/repo?Revision=master&FilePath=<filename>
https://www.modelscope.cn/api/v1/models/iic/SenseVoiceSmall/repo?Revision=master&FilePath=chn_jpn_yue_eng_ko_spectok.bpe.model
```

- 匿名访问，不需要 token
- 实测 10.7 秒下 230 MB（国内）
- 所有文件的 SHA256 硬编码进 `stt/model_paths.py`

## 核心依赖

### 新增（`pyproject.toml`）

- `sentencepiece`（Google 官方 BPE tokenizer，PyPI wheel ~1.5 MB）

### 已有，保留

- `onnxruntime`（Microsoft 官方，~16 MB）
- `kaldi-native-fbank`（Fangjun Kuang 独立维护的特征提取库，~230 KB）
- `numpy`、`pyyaml`

### 要确认彻底消失

- `torch`、`torchaudio`、`funasr`、`sherpa-onnx`、`onnx`（我们不需要 onnx 包，只需要 onnxruntime）、`soundfile`（调试时加的，不需要）

## Port 的三份代码 —— 文件级对齐

### 1. `stt/_wav_frontend.py`（~120 行）

来源：`modelscope/FunASR/runtime/python/onnxruntime/funasr_onnx/utils/frontend.py`

Port 的内容：
- `WavFrontend` class：三个方法 `fbank(waveform)`、`apply_lfr(inputs, m, n)`、`apply_cmvn(inputs)` + `__init__` 接 `**frontend_conf`
- `load_cmvn(cmvn_file)` 函数：从 `am.mvn` 解析 `<AddShift>` / `<Rescale>` 两行得到 means/vars

**不 port** 的内容：
- `WavFrontendOnline` 类（流式推理用，我们不需要）
- `load_bytes`、`SinusoidalPositionEncoderOnline`、`test()` 等辅助

关键：
- **`dither=0` 强制**：官方 default 是 `1.0`（推理时会引入随机性），我们在实例化时显式传 `dither=0` 保证确定性
- 版权头 + 来源 URL 注释
- MIT 协议（和我们项目兼容）

### 2. `stt/_tokenizer.py`（~40 行）

来源：`modelscope/FunASR/runtime/python/onnxruntime/funasr_onnx/utils/sentencepiece_tokenizer.py`

Port 的内容：`SentencepiecesTokenizer` class，接口是 `encode(text)` / `decode(token_ids: list[int]) -> str`。

实现：包装 `sentencepiece.SentencePieceProcessor.Load(bpemodel_path)`。

### 3. `stt/_postprocess.py`（~100 行）

来源：`modelscope/FunASR/runtime/python/onnxruntime/funasr_onnx/utils/postprocess_utils.py`

Port 的内容：
- `rich_transcription_postprocess(s)` 函数
- 相关常量：`emo_set`、`event_set`、`lang_dict`、`emoji_dict`、`emo_dict`、`event_dict`
- 依赖的辅助函数：`format_str_v2`、可能的 `format_str` 等

不 port：`sentence_postprocess`、`sentence_postprocess_sentencepiece`（那是给 Paraformer 用的）。

## 详细改动清单

### 1. `pyproject.toml`

- 新增 `sentencepiece`
- 其他保持不变（已经是纯 onnxruntime 依赖了）

### 2. `stt/model_paths.py`（重写）

```python
MODEL_VERSION = "SenseVoiceSmall-onnx-iic-2024-09-25"  # ModelScope 仓库上传日期
MODEL_DIR_NAME = "iic-SenseVoiceSmall-onnx"            # 本地缓存目录名

# 5 个文件 + SHA256（从 ModelScope API 的 Files 响应里提取）
MODEL_FILES = [
    {
        "name": "model_quant.onnx",
        "repo": "iic/SenseVoiceSmall-onnx",
        "sha256": "21dc965f689a78d1604717bf561e40d5a236087c85a95584567835750549e822",
        "size": 241216270,
    },
    {
        "name": "tokens.json",
        "repo": "iic/SenseVoiceSmall-onnx",
        "sha256": "a2594fc1474e78973149cba8cd1f603ebed8c39c7decb470631f66e70ce58e97",
        "size": 352064,
    },
    {
        "name": "am.mvn",
        "repo": "iic/SenseVoiceSmall-onnx",
        "sha256": "29b3c740a2c0cfc6b308126d31d7f265fa2be74f3bb095cd2f143ea970896ae5",
        "size": 11203,
    },
    {
        "name": "config.yaml",
        "repo": "iic/SenseVoiceSmall-onnx",
        "sha256": "f71e239ba36705564b5bf2d2ffd07eece07b8e3f2bbf6d2c99d8df856339ac19",
        "size": 1855,
    },
    {
        "name": "chn_jpn_yue_eng_ko_spectok.bpe.model",
        "repo": "iic/SenseVoiceSmall",  # 注意:这个来自 PyTorch 版仓库
        "sha256": None,  # 开发时取值,TODO 填
        "size": 377341,
    },
]

REQUIRED_FILES = tuple(f["name"] for f in MODEL_FILES)

def user_data_dir() -> Path: ...            # 和原来一样
def models_root() -> Path: ...               # user_data_dir / models
def sense_voice_model_dir() -> Path:         # models_root / MODEL_DIR_NAME
def manifest_path() -> Path: ...             # user_data_dir / .model_state.json
def is_model_complete(dir) -> bool: ...     # 检查 REQUIRED_FILES 全部非空存在
def load_manifest() -> dict | None: ...
def save_manifest(version, dir) -> None: ...
def find_local_model() -> Path | None: ...  # manifest 优先, 否则默认路径

def modelscope_file_url(repo: str, filename: str) -> str:
    return (f"https://www.modelscope.cn/api/v1/models/{repo}"
            f"/repo?Revision=master&FilePath={filename}")
```

### 3. `stt/downloader.py`（重写/简化）

```python
def download_model(progress_cb=None, log_cb=None) -> Path:
    local = find_local_model()
    if local is not None:
        return local  # 命中跳过

    target = sense_voice_model_dir()
    target.mkdir(parents=True, exist_ok=True)

    total_bytes = sum(f["size"] for f in MODEL_FILES)
    downloaded_total = 0

    for spec in MODEL_FILES:
        url = modelscope_file_url(spec["repo"], spec["name"])
        dest = target / spec["name"]

        if dest.exists() and dest.stat().st_size == spec["size"]:
            # 验证 SHA256
            if _sha256(dest) == spec["sha256"]:
                downloaded_total += spec["size"]
                continue

        _download_one(url, dest,
                      lambda done, _: progress_cb(downloaded_total + done, total_bytes) if progress_cb else None)

        actual_sha = _sha256(dest)
        if spec["sha256"] is not None and actual_sha != spec["sha256"]:
            raise ModelDownloadError(...)
        downloaded_total += spec["size"]

    save_manifest(MODEL_VERSION, target)
    return target
```

- 纯 stdlib（`urllib.request` + `hashlib`），引导向导 `setup_window.py` 可直接复用
- 不再有 ghproxy 候选列表，不再 failover（ModelScope 单源稳定）
- 不再 tar.bz2 解压，5 个文件直接落到目标目录

### 4. `stt/sense_voice.py`（重写）

```python
import io, wave
import numpy as np
import yaml

from stt.base import BaseSTT
from stt.downloader import download_model
from stt.model_paths import find_local_model
from stt._wav_frontend import WavFrontend
from stt._tokenizer import SentencepiecesTokenizer
from stt._postprocess import rich_transcription_postprocess


class SenseVoiceSTT(BaseSTT):
    LANG_ID = {"auto": 0, "zh": 3, "en": 4, "yue": 7, "ja": 11, "ko": 12, "nospeech": 13}
    WITH_ITN, WITHOUT_ITN = 14, 15

    def __init__(self, language="auto", use_itn=True, num_threads=4):
        self.language = language
        self.use_itn = use_itn
        self.num_threads = num_threads
        self._session = None
        self._frontend = None
        self._tokenizer = None

    def load(self):
        if self._session is not None:
            return

        model_dir = find_local_model()
        if model_dir is None:
            model_dir = download_model()

        config = yaml.safe_load((model_dir / "config.yaml").read_text())
        frontend_conf = dict(config["frontend_conf"])
        frontend_conf["cmvn_file"] = str(model_dir / "am.mvn")
        frontend_conf["dither"] = 0  # 强制确定性
        self._frontend = WavFrontend(**frontend_conf)

        self._tokenizer = SentencepiecesTokenizer(
            bpemodel=str(model_dir / "chn_jpn_yue_eng_ko_spectok.bpe.model")
        )

        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = self.num_threads
        self._session = ort.InferenceSession(
            str(model_dir / "model_quant.onnx"),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

    def transcribe(self, wav_data: bytes) -> str:
        if not wav_data:
            return ""
        self.load()

        # WAV bytes → float32 [-1, 1] (WavFrontend 内部会 *32768 转 int16 scale)
        buf = io.BytesIO(wav_data)
        with wave.open(buf, "rb") as wf:
            audio = (np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
                     .astype(np.float32) / 32768.0)

        if len(audio) < 1600:
            return ""

        feat, _ = self._frontend.fbank(audio)
        feat, _ = self._frontend.lfr_cmvn(feat)

        x = feat[None].astype(np.float32)
        xl = np.array([x.shape[1]], dtype=np.int32)
        lang = np.array([self.LANG_ID.get(self.language, 0)], dtype=np.int32)
        tn = np.array([self.WITH_ITN if self.use_itn else self.WITHOUT_ITN], dtype=np.int32)

        ctc_logits, encoder_out_lens = self._session.run(
            ["ctc_logits", "encoder_out_lens"],
            {"speech": x, "speech_lengths": xl, "language": lang, "textnorm": tn},
        )
        logits = ctc_logits[0, :encoder_out_lens[0], :]

        yseq = logits.argmax(axis=-1)
        mask = np.concatenate(([True], np.diff(yseq) != 0))  # CTC 去连续重复
        yseq = yseq[mask]
        token_int = yseq[yseq != 0].tolist()                   # blank=0

        raw = self._tokenizer.decode(token_int)
        return rich_transcription_postprocess(raw)
```

### 5. `pyproject.toml`

增加一行 `sentencepiece` 到 `dependencies`。

### 6. 其他文件不需要改

- `stt/__init__.py`、`stt/base.py`：已经是最终版
- `main.py`、`config_manager.py`：已经接到 `stt.create_stt` 工厂
- `macos/setup_window.py`、`debian/setup_window.py`：stage B 已经改成调 `stt.downloader.download_model()`
- `setup_linux.sh`、`setup_macos.sh`：已经简化
- `build.sh`：`SOURCE_STT` 列表需要加上 3 个新文件（`_wav_frontend.py`、`_tokenizer.py`、`_postprocess.py`）

### 7. `build.sh` 补充

```bash
SOURCE_STT=(
    stt/__init__.py stt/base.py stt/model_paths.py
    stt/downloader.py stt/sense_voice.py
    stt/_wav_frontend.py stt/_tokenizer.py stt/_postprocess.py  # 新增三个
)
```

### 8. 文档同步

- `CLAUDE.md`：架构图更新，`stt/` 模块清单加新文件，依赖说明改为 `onnxruntime + kaldi-native-fbank + sentencepiece + numpy`
- `README.md`：首包体积和下载说明更新
- 本文档目录名从 `12-sherpa-onnx迁移` 改为 `12-去torch-iic官方ONNX`

## 执行顺序

1. Port 三个 funasr_onnx 工具文件 → `stt/_wav_frontend.py`、`stt/_tokenizer.py`、`stt/_postprocess.py`
2. 重写 `stt/model_paths.py`（新常量 + 5 文件 manifest）
3. 重写 `stt/downloader.py`（逐文件下载）
4. 重写 `stt/sense_voice.py`（用新 WavFrontend + Tokenizer + postprocess）
5. `pyproject.toml` 加 `sentencepiece`
6. `uv sync` 验证依赖树干净
7. **端到端冒烟**：清缓存 → 下载 → 五语种推理验证标点/ITN/大小写
8. `build.sh` 的 `SOURCE_STT` 加新文件
9. `CLAUDE.md` / `README.md` 同步
10. `uv run ruff check .`
11. 撰写最终 `SUMMARY.md`
12. 删除 `models-v1` 草稿 GitHub release（和用户确认）

## 验证方案

**端到端冒烟**（macOS worktree）：

```bash
# 1. 清掉本地模型缓存
rm -rf "$HOME/Library/Application Support/Whisper Input/models"
rm -f "$HOME/Library/Application Support/Whisper Input/.model_state.json"

# 2. 跑主程序,触发下载 + 加载
cd /Users/jing/Developer/whisper-input/.claude/worktrees/sherpa-onnx-migration
uv run python -c "
from stt.sense_voice import SenseVoiceSTT
stt = SenseVoiceSTT(language='auto', use_itn=True)
stt.load()
# 用真人录音(前面测试时下载的 /tmp/funasr_zh.wav)
wav = open('/tmp/funasr_zh.wav', 'rb').read()
print(repr(stt.transcribe(wav)))
# 期望: '欢迎大家来体验达摩院推出的语音识别模型。' (含句号)
"

# 3. 再跑一次看本地缓存命中
uv run python -c "...同上..."  # 应该秒级加载,不联网
```

**五语种 + 标点/ITN 对比**：

用前面验证 fp32 + 我的 ad-hoc features 时的那组 test wavs，预期：

```
zh: 开放时间早上9点至下午5点。           ✓ 句号 + ITN 数字
en: The tribal chieftain called for...  ✓ 大小写 + ITN
ja: うちの中学は弁当制で持って...        ✓ 日文
ko: 조금만 생각을 하면서 살면...          ✓ 韩文
yue: 呢几个字都表达唔到,我想讲嘅意思。  ✓ 粤语 + 标点
```

**`uv sync` 速度**：期望 30 秒内完成（加 sentencepiece 可能比当前多几秒，还是远快于 40 分钟）。

**`ruff check`**：全量通过。

## 风险 & 开放问题

1. **BPE 模型文件的 SHA256**：`iic/SenseVoiceSmall` 仓库里 `chn_jpn_yue_eng_ko_spectok.bpe.model` 的 SHA256 开发时实测填入。我在主仓库 modelscope 缓存里看到文件本身存在（377341 字节），但需要算一次 hash。
2. **tokens.json 实际用不用**：`SentencepiecesTokenizer` 用 BPE 模型解码，`tokens.json` 理论上可能冗余。但 `funasr_onnx` 源码里可能还是读 tokens.json 用来初始化一些 map。如果实测不需要，可以从 MODEL_FILES 里去掉（省 344 KB 下载）。**执行阶段第一步先确认一下**。
3. **ModelScope 匿名 API 稳定性**：实测可以直连下载不要 token，但长期是否对个人用户限流未知。万一以后限了，回退方案是"用 ghproxy 镜像 GitHub 上 funasr 的 release" → 但那又回到不稳定的路径。目前不处理。
4. **`rich_transcription_postprocess` 的完整依赖链**：需要一并 port `format_str_v2` 和相关常量。执行时先把整个 `postprocess_utils.py` 扫一遍，把与 SenseVoice 路径有关的函数和常量打包进 `stt/_postprocess.py`，不相关的（如 `sentence_postprocess_sentencepiece` 这种给 Paraformer 用的）不 port。
5. **setup_window 已经改好**：两个引导向导当前已经指向 `stt.downloader.download_model()`，downloader 内部从 tar.bz2 改成多文件不影响接口。需要更新"约 160 MB"文案为"约 231 MB"。

## 对比原始 PLAN 的变化

如果对照本项目历史上的 PLAN（最早写的 sherpa-onnx 迁移版本），主要变化：

| | 原 PLAN（已废弃） | 现 PLAN（本文档）|
|---|---|---|
| 模型来源 | k2-fsa/sherpa-onnx GitHub release int8 tar.bz2 | iic/SenseVoiceSmall-onnx ModelScope 5 文件 |
| 下载协议 | ghproxy 多源 failover + tarfile 解压 | ModelScope 单源直链 + 多文件 |
| 特征提取 | 照抄 sherpa-onnx test.py（含 LFR bug）| funasr_onnx WavFrontend 原样 port |
| 解码 | 手写 tokens.txt 查表 + ▁→space | 官方 SentencepiecesTokenizer |
| 后处理 | `re.sub(r"<\|[^\|]*\|>", "", text)` | 官方 `rich_transcription_postprocess` |
| 体积 | "170 MB" 的谎言（int8 坏的） | 231 MB 真实可用 |
| 首包正确性 | 破坏严重（无标点、无 ITN、ALL CAPS） | bit-aligned FunASR |
| 用户需要手动 | 上传模型到 GitHub release | 无 |

**关键教训**：第一次查官方资源不应该停在 sherpa-onnx test.py，应该一开始就问"达摩院自己有没有官方 ONNX 推理代码"。本轮开发的大部分时间浪费在了走一条低质量的第三方路径上。
