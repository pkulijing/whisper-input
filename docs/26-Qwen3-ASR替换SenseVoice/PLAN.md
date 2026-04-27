# 实现计划:Qwen3-ASR 替换 SenseVoice(离线模式)

## 总体策略

两个阶段,每段都可以独立验证:

1. **阶段 A:离线推理打通**。把 Wasser1462 的代码改造成我们的去 torch 版,
   `Qwen3ASRSTT.transcribe(wav_bytes) → str` 端到端跑通。这一步结束后已
   经具备"替换 SenseVoice"的能力
2. **阶段 B:多模型选项 + 热切换 + 清理**。在阶段 A 基础上加 0.6B/1.7B
   选项、设置页下拉、热切换逻辑,删除所有 SenseVoice 代码和依赖,更新
   CLAUDE.md / BACKLOG.md,全面补测试

**本轮完全不做流式**。流式识别是第 27 轮的事,本轮只保证代码结构对未来
流式友好(三段 ONNX session 分离、特征提取无状态、tokenizer 无状态)。

## 阶段 A:离线推理打通

### A.1 目录结构新增

```
src/whisper_input/stt/qwen3/
    __init__.py
    qwen3_asr.py         # Qwen3ASRSTT 主类(继承 BaseSTT)
    _downloader.py       # ModelScope 下载逻辑
    _onnx_runner.py      # 三段 ONNX session 管理,底层推理
    _feature.py          # Whisper 风格 log-mel 特征提取
    _tokenizer.py        # HF tokenizers 库包装
    _prompt.py           # 提示词模板构建
    _postprocess.py      # asr_text 解析 + 特殊 token 清理
```

**`stt/qwen3/` 单独建子目录的理由**:职责分散在 6-7 个小模块,分目录比
单文件可读性好,且单元测试各自独立。

### A.2 模型下载(ModelScope 唯一源)

`_downloader.py`:

```python
from modelscope import snapshot_download
from pathlib import Path

REPO_ID = "zengshuishui/Qwen3-ASR-onnx"

def download_qwen3_asr(variant: str) -> Path:
    """下载指定 variant 的 ONNX 文件 + tokenizer。

    variant: '0.6B' 或 '1.7B'
    返回 ModelScope cache 根路径 (~/.cache/modelscope/hub/...).
    """
    assert variant in ("0.6B", "1.7B"), f"unknown variant: {variant}"
    root = snapshot_download(
        REPO_ID,
        allow_patterns=[
            f"model_{variant}/conv_frontend.onnx",
            f"model_{variant}/encoder.int8.onnx",
            f"model_{variant}/decoder.int8.onnx",
            "tokenizer/*",
        ],
    )
    return Path(root)
```

**强依赖 ModelScope**,不设计 HuggingFace fallback、不写兜底逻辑。下载
失败直接让 modelscope 报错,上层捕获后用现有的 overlay 错误提示机制告
知用户。

**注意**:0.6B 和 1.7B 的 conv_frontend 是独立文件(44MB vs 48MB),不
共享,必须按 variant 拉对应目录。

**单测策略**:mock `snapshot_download` 验证 `allow_patterns` 参数正确
构造;variant 参数校验的异常路径也要测。

### A.3 Whisper log-mel 特征提取(最关键自研模块)

见 BACKGROUND.md "Kaldi fbank vs Whisper mel"。不能用 `kaldi-native-fbank`
(数值分布对不齐 Qwen3-ASR 的训练特征),不能用 `librosa`(拖 scipy 等大
依赖),必须手写 + golden 文件回归。

`_feature.py` 核心:

```python
import numpy as np

SAMPLE_RATE = 16000
N_FFT = 400            # 25ms @ 16kHz
HOP_LENGTH = 160       # 10ms @ 16kHz
N_MELS = 128

_MEL_FILTERS = _compute_whisper_mel_filters(
    n_mels=N_MELS, sr=SAMPLE_RATE, n_fft=N_FFT
)  # shape (201, 128),模块加载时算一次
_HANN_WINDOW = np.hanning(N_FFT).astype(np.float32)

def log_mel_spectrogram(audio: np.ndarray) -> np.ndarray:
    """
    audio: float32 1D, 16kHz mono, 范围 [-1, 1]
    return: log-mel spectrogram (n_frames, 128) float32
    """
    # 1. 分帧 (reflect padding 兼容 whisper)
    # 2. 乘 Hann window
    # 3. 实数 FFT (n_fft=400 → 201 bins)
    # 4. Power spectrogram
    # 5. Mel filter bank 矩阵乘
    # 6. Whisper 特殊 log:log10 + clip(max-8) + (+4)/4 归一化
    ...
```

**Mel filter bank 算法**:严格按 Whisper 官方 `transformers/models/
whisper/feature_extraction_whisper.py` 的定义实现 —— Slaney 风格 mel
scale(HTK 不对,Whisper 训练时用的是 Slaney),中心频率线性在 mel 域
均匀分布。这部分会直接参考 Whisper 源码用 numpy 翻译,不会自己发明。

**验证策略**(**阻塞项,先做**):

1. 写一个独立的一次性脚本 `scripts/generate_whisper_mel_golden.py`,用
   `transformers.WhisperFeatureExtractor` 跑 `tests/fixtures/zh.wav`,输
   出 log-mel 存成 `tests/fixtures/whisper_mel_golden_zh.npy`。这个脚本
   只在开发机上跑一次,生成 golden 文件,**transformers 不进项目
   runtime 依赖**
2. `tests/test_qwen3_feature.py` 里加 `test_log_mel_matches_golden()`,
   用我们自己实现的 `log_mel_spectrogram()` 跑同一段音频,跟 golden
   `np.allclose(rtol=1e-4, atol=1e-5)` 比对。**必须通过**,否则识别
   必然崩

**其他单测**:

- `test_log_mel_shape()`:各种长度音频的输出形状
- `test_log_mel_silence()`:全零音频的输出特性
- `test_log_mel_clip_high()`:饱和信号的 log 上界
- `test_mel_filter_bank_shape_and_values()`:filter bank 矩阵的维度 + 每
  行非负 + 能量归一化
- `test_hann_window_matches_numpy()`:窗函数值

### A.4 Tokenizer(HF tokenizers,去 transformers)

`_tokenizer.py` 用 `tokenizers` 库直接构造一个 BPE tokenizer,不经过
`transformers.AutoTokenizer`。

**第一件事要验证**:ModelScope 仓库里 `tokenizer/` 目录**没有**
`tokenizer.json`(标准 HF fast tokenizer 格式),只有 `vocab.json` +
`merges.txt` + `tokenizer_config.json` + `chat_template.json` +
`preprocessor_config.json`。

所以我们要自己用 `tokenizers` 库手工构造:

```python
from tokenizers import Tokenizer, decoders, pre_tokenizers
from tokenizers.models import BPE

def build_qwen_tokenizer(tokenizer_dir: Path) -> Tokenizer:
    tok = Tokenizer(BPE.from_file(
        vocab=str(tokenizer_dir / "vocab.json"),
        merges=str(tokenizer_dir / "merges.txt"),
    ))
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    # 从 tokenizer_config.json 注册 added_tokens (特殊 token 如
    # <|im_start|> / <|audio_pad|> 等)
    special_tokens = _read_added_tokens(
        tokenizer_dir / "tokenizer_config.json"
    )
    tok.add_special_tokens(special_tokens)
    return tok
```

如果这条路走不通(加载或 encode/decode 行为跟 Wasser1462 对不齐),
**fallback 方案**是 pin `transformers` 的最小子集 —— 但这违反硬约束,
只能是最后手段。发现走不通时要回到 PLAN 重新决策,不要隐式切换。

**单测**:

- `test_encode_decode_roundtrip()`:一批文本 encode 再 decode 等于原
  文本
- `test_special_tokens_present()`:`<|im_start|>` / `<|im_end|>` /
  `<|audio_start|>` / `<|audio_pad|>` / `<|audio_end|>` /
  `<|endoftext|>` / `<asr_text>` 都能正确编码为已知 token_id
- `test_encode_special_token_ids_match_reference()`:跟用
  `transformers.AutoTokenizer` 加载同一目录的 token_id 结果对齐(dev
  fixture)
- `test_decode_skip_special_tokens()`:decode 时过滤特殊 token

### A.5 Prompt 构建

`_prompt.py`(极薄,纯字符串拼接):

```python
def build_prompt(audio_token_count: int, language: str | None = None) -> str:
    system = "<|im_start|>system\n<|im_end|>\n"
    user = "<|im_start|>user\n"
    if language:
        user += f"language {language}<asr_text>\n"
    user += (
        f"<|audio_start|>{'<|audio_pad|>' * audio_token_count}"
        f"<|audio_end|><|im_end|>\n"
    )
    assistant = "<|im_start|>assistant\n"
    return system + user + assistant
```

**单测**:

- `test_prompt_no_language()`:默认无语言提示
- `test_prompt_with_language()`:带 language=zh 参数
- `test_prompt_audio_pad_count()`:`<|audio_pad|>` 重复次数正确
- `test_prompt_structure()`:prompt 各段顺序正确

### A.6 ONNX 推理 runner

`_onnx_runner.py` 封装三段 session:

```python
import numpy as np
import onnxruntime as ort

class ONNXRunner:
    def __init__(self, model_dir: Path):
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.conv = ort.InferenceSession(
            str(model_dir / "conv_frontend.onnx"),
            sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        self.encoder = ort.InferenceSession(
            str(model_dir / "encoder.int8.onnx"), ...
        )
        self.decoder = ort.InferenceSession(
            str(model_dir / "decoder.int8.onnx"), ...
        )
        # 从 decoder 的 input schema 推断 KV cache 层数、head 维度等
        self._decoder_meta = self._inspect_decoder()

    def encode_audio(self, mel: np.ndarray) -> np.ndarray:
        """log-mel (n_frames, 128) → audio_features (1, A, 1024)"""
        # 1. conv_frontend
        # 2. 计算 audio_token length
        # 3. encoder
        # 4. 修整(去掉 padding 尾部)
        ...

    def alloc_decoder_caches(self, max_total_len: int) -> list[np.ndarray]:
        """分配 28 对 KV cache"""
        ...

    def decoder_step(
        self,
        input_ids: np.ndarray,       # (1, S)
        audio_features: np.ndarray,  # (1, A, 1024)
        caches: list[np.ndarray],    # 28 对 KV
        cur_len: int,
    ) -> tuple[np.ndarray, list[np.ndarray]]:
        """运行 decoder 一步,返回 logits + 更新后的 caches"""
        ...
```

KV cache 结构(spike 已确认):28 层,每层 `(1, max_total_len, 8, 128)`
float32。`max_total_len` 我们自己定,本轮设 **1024**(prompt ~300 +
生成 ~200 + 缓冲 ~500,足够短录音用)。

cache_position 是绝对位置寻址 —— 对本轮离线模式没意义(单次自回归,不
rollback),但**保持接口支持绝对寻址**是为 27 轮流式准备。

**单测**:

- `test_runner_init_sanity()`:加载真实 0.6B 模型(先 cache 到 CI),
  检查三个 session 的输入输出名字和形状符合 spike 结论
- `test_runner_encode_audio_shape()`:喂 500 帧 random log-mel,输出
  `(1, A, 1024)` 且 A = 预期 audio token 数
- `test_runner_decoder_step_shape()`:喂假 audio_features + 假
  input_ids + 全零 KV cache,输出 logits 形状 `(1, S, 151936)` 且
  new_caches 数量 = 56(28 × 2)
- `test_runner_decoder_step_cache_update()`:连续两步,第二步
  cache_position 跳到 S,验证 new_caches 在该位置被写入

### A.7 后处理

`_postprocess.py`:

```python
def parse_asr_output(raw_text: str) -> str:
    """从 decoder 生成的原始 token 序列 decode 后的字符串里,
    截取 <asr_text>...<|im_end|> 之间的内容,清理特殊 token。"""
    ...
```

**单测**:

- `test_parse_asr_output_normal()`:标准输出
- `test_parse_asr_output_no_marker()`:没有 `<asr_text>` 标记
- `test_parse_asr_output_trailing_special()`:尾部带 `<|endoftext|>` /
  `<|im_end|>`

### A.8 Qwen3ASRSTT 主类

`qwen3_asr.py`:

```python
class Qwen3ASRSTT(BaseSTT):
    def __init__(self, variant: str = "0.6B"):
        self.variant = variant
        self._runner: ONNXRunner | None = None
        self._tokenizer: Tokenizer | None = None
        self._eos_id: int | None = None

    def load(self) -> None:
        if self._runner is not None:
            return  # 幂等
        model_dir = download_qwen3_asr(self.variant)
        self._runner = ONNXRunner(model_dir / f"model_{self.variant}")
        self._tokenizer = build_qwen_tokenizer(model_dir / "tokenizer")
        self._eos_id = self._tokenizer.token_to_id("<|im_end|>")
        # 预热:跑一次很短的虚假音频,让 ORT 完成 graph init
        # 这样首次真实推理的延迟更平稳
        self._warmup()

    def transcribe(self, wav_bytes: bytes) -> str:
        if len(wav_bytes) == 0:
            return ""
        audio = _wav_bytes_to_float32(wav_bytes)
        if len(audio) < 0.1 * SAMPLE_RATE:
            return ""

        mel = log_mel_spectrogram(audio)
        audio_features = self._runner.encode_audio(mel)
        audio_token_len = audio_features.shape[1]

        prompt = build_prompt(audio_token_len, language=None)
        input_ids = np.array(
            [self._tokenizer.encode(prompt).ids], dtype=np.int64
        )
        caches = self._runner.alloc_decoder_caches(max_total_len=1024)

        # Prefill
        logits, caches = self._runner.decoder_step(
            input_ids, audio_features, caches, cur_len=0
        )
        cur_len = input_ids.shape[1]

        # 自回归生成
        generated = []
        for _ in range(MAX_NEW_TOKENS):  # e.g. 256
            next_id = int(np.argmax(logits[0, -1]))
            if next_id == self._eos_id:
                break
            generated.append(next_id)
            next_ids = np.array([[next_id]], dtype=np.int64)
            logits, caches = self._runner.decoder_step(
                next_ids, audio_features, caches, cur_len
            )
            cur_len += 1

        raw = self._tokenizer.decode(generated)
        return parse_asr_output(raw)
```

**单测**:

- `test_transcribe_empty_wav()`:空 bytes 返回 ""
- `test_transcribe_very_short_wav()`:<0.1 秒音频返回 ""
- `test_load_idempotent()`:多次 load 不重复加载
- `test_transcribe_zh_wav()`(端到端 smoke test):跑
  `tests/fixtures/zh.wav`,输出稳定(regex 匹配关键字 or 跟 golden 字
  符串精确对齐,取决于 Qwen3-ASR 输出的稳定性)

### A.9 阶段 A 验收

- [ ] `uv run python -c "from whisper_input.stt.qwen3 import
      Qwen3ASRSTT; s = Qwen3ASRSTT(); s.load(); print(s.transcribe(
      open('tests/fixtures/zh.wav', 'rb').read()))"` 输出正确的中文
- [ ] Log-mel golden 测试通过
- [ ] 所有 qwen3 子模块单测通过

## 阶段 B:多模型选项 + 热切换 + 清理

### B.1 更新 stt 工厂

`stt/__init__.py`:

```python
def create_stt(engine: str, config: dict) -> BaseSTT:
    if engine == "qwen3":
        from whisper_input.stt.qwen3 import Qwen3ASRSTT
        return Qwen3ASRSTT(variant=config.get("variant", "0.6B"))
    raise ValueError(f"unknown STT engine: {engine}")
```

`BaseSTT` **不动**(不加 streaming 接口,那是 27 轮的事)。

### B.2 Config schema 升级

`config.example.yaml`:

```yaml
stt:
  engine: qwen3
  qwen3:
    variant: "0.6B"   # "0.6B" | "1.7B"
```

`config_manager.py` 增加一次迁移:如果读到老 config 里
`stt.engine: sensevoice`,自动改成 `qwen3` + variant `0.6B`,日志记录
迁移发生,保存。

**单测**:`test_config_migration_sensevoice_to_qwen3()`:构造一个
sensevoice config,验证 load 后字段被改写并持久化。

### B.3 Settings Web UI 改造

分两部分:

**后端(settings_server.py)**:

- `GET /api/config`:响应里新增 `stt.qwen3.variant`
- `POST /api/config`:支持修改 `stt.qwen3.variant`,变更后触发
  `WhisperInput._switch_stt_variant()` 回调

**前端**:设置页加一个"识别模型"下拉,两个选项:

- `0.6B (快速,~1.5GB 内存)`
- `1.7B (更准,~3GB 内存,~2.4GB 首次下载)`

下拉变更 → POST → 前端 UI 立刻标灰并显示"模型切换中...",后端完成后
通过 polling `/api/switch_status` 知道切换完成 → 页面恢复。

**单测**:

- `test_settings_post_variant()`:模拟 POST variant 变更,验证 config
  持久化 + 回调被调用
- `test_settings_switch_status_endpoint()`:polling endpoint 返回 状态
  字段

### B.4 热切换逻辑

`WhisperInput` 主控加 `_switch_stt_variant(new_variant)` 方法:

```python
def _switch_stt_variant(self, new_variant: str) -> None:
    """在后台线程加载新 variant,加载完成后原子替换 self.stt。"""
    with self._switch_lock:
        if self._switching:
            return  # 避免并发切换
        self._switching = True

    def _worker():
        try:
            new_stt = Qwen3ASRSTT(variant=new_variant)
            new_stt.load()
            # 原子替换
            old_stt = self.stt
            self.stt = new_stt
            # 显式释放旧 session
            del old_stt
            gc.collect()
        finally:
            self._switching = False

    threading.Thread(target=_worker, daemon=True).start()
```

**切换期间的 UX**:

- 托盘图标变灰(`pystray` API 支持)
- 热键按下时 overlay 显示"模型切换中,稍后再试"

**单测**(mock 掉 Qwen3ASRSTT 的 load):

- `test_switch_variant_updates_stt_reference()`
- `test_switch_variant_concurrent_request_rejected()`:切换途中再请求
  被忽略
- `test_switch_variant_old_stt_released()`:切换完成后旧引用不再持有

### B.5 删除 SenseVoice + 依赖清理

**删除的文件**:

- `src/whisper_input/stt/sense_voice.py`
- `src/whisper_input/stt/_wav_frontend.py`
- `src/whisper_input/stt/_tokenizer.py`
- `src/whisper_input/stt/_postprocess.py`
- `tests/test_sense_voice.py`
- `tests/test_postprocess.py`
- `tests/test_wav_frontend.py`(如果存在)

**依赖变动**(`pyproject.toml`):

- 删除:`kaldi-native-fbank>=1.22.3`、`sentencepiece>=0.2.1`
- 新增:`tokenizers>=0.20` (~10MB,HF 的 Rust tokenizer 库)
- `modelscope` 保留(换模型后还用)
- `soundfile` 保留(读 WAV 仍要用)
- `numpy` / `onnxruntime` / `pyyaml` 等都不变

**净变化**:去掉 kaldi-native-fbank (230KB) + sentencepiece (1.5MB),加
上 tokenizers (~10MB)。净增 ~8MB,完全可接受。

### B.6 测试改造

全部替换 SenseVoice 测试套:

| 原测试 | 新测试 |
|---|---|
| `tests/test_sense_voice.py` | `tests/test_qwen3_asr.py` |
| `tests/test_postprocess.py` | `tests/test_qwen3_postprocess.py` |
| `tests/test_wav_frontend.py` | `tests/test_qwen3_feature.py` |
| `tests/conftest.py` 里 SenseVoice mock | Qwen3 相关 mock(暂无) |

新增测试:

| 文件 | 覆盖 |
|---|---|
| `tests/test_qwen3_feature.py` | log-mel + golden 对齐 |
| `tests/test_qwen3_tokenizer.py` | BPE tokenizer 加载 + 特殊 token |
| `tests/test_qwen3_prompt.py` | prompt 构建 |
| `tests/test_qwen3_postprocess.py` | asr_text 解析 |
| `tests/test_qwen3_downloader.py` | ModelScope 下载(mock) |
| `tests/test_qwen3_runner.py` | ONNX runner(真实小规模推理) |
| `tests/test_qwen3_asr.py` | 端到端 smoke test(跑 zh.wav) |
| `tests/test_stt_factory.py` | create_stt('qwen3', ...) 工厂 |
| `tests/test_config_migration.py` | sensevoice → qwen3 自动迁移 |
| `tests/test_settings_variant.py` | 设置页切换 variant |

**CI cache key**:`modelscope-qwen3-asr-0.6b-int8-v1`

**目标覆盖率**:

- 纯 Python 模块(feature、tokenizer、prompt、postprocess、downloader)≥
  95%
- 有 ONNX 依赖的模块(runner、qwen3_asr)≥ 85%
- 整体 **≥ 70%**(当前 51%)

### B.7 CLAUDE.md 重写

章节改动:

**Project Overview**(删 SenseVoice,改 Qwen3-ASR):

> Whisper Input is a cross-platform desktop voice input tool. Uses
> Qwen3-ASR (Alibaba Qwen team, 2026 open release) int8 ONNX via
> `onnxruntime`—a 0.6B encoder-decoder transformer with Whisper-style
> log-mel frontend. **No torch, no transformers, no vLLM.** Supports
> optional 1.7B variant for higher accuracy.

**Architecture** 里 `stt/` 部分重写为 `stt/qwen3/` 子模块布局描述。

**Key Technical Decisions** 添加段落:

- **Encoder-decoder transformer over CTC**:Qwen3-ASR 的自回归 decoder
  架构是为未来流式做技术储备(第 27 轮)
- **Whisper log-mel self-implemented**:数值对齐官方,不走 librosa/scipy
- **ModelScope-only, no HF fallback**:用户在中国,单一分发源更稳

**Dependencies** 节:去掉 kaldi-native-fbank / sentencepiece,加上
tokenizers。

### B.8 BACKLOG.md 更新

- **保留并更新**"中英混杂 / 专业词汇"条目:改为"Qwen3-ASR prompt
  biasing 实现(第 28 轮目标)",scope 降到"小",因为模型已原生支持
- **保留**"实时语音识别(streaming)"条目:改为"第 27 轮目标",但
  技术分析重写 —— 删掉 SenseVoice streaming 相关猜测,换成"Qwen3-ASR
  encoder-decoder 架构 + chunked encoder + rollback decoder"
- **新增**:"自适应纠错系统"(Layer 1 + Layer 2,第 29 轮+)
- **新增**:"1.7B int4 量化"(等社区产出)

### B.9 README.md / 其他文档

- README 里"基于 SenseVoice-Small"的描述改成"基于 Qwen3-ASR 0.6B/1.7B"
- 图标、截图等不动
- 如果有 "支持的语种" 描述,Qwen3-ASR 支持 30 语言 + 22 中文方言,写过来

### B.10 阶段 B 验收

阶段 A 验收项全部通过,加上:

- [ ] 设置页切换 0.6B ↔ 1.7B 正常工作,切换过程 UX 提示清楚
- [ ] 切换后 RSS 不持续累积(手动验证:切换 5 次,观察内存)
- [ ] SenseVoice 相关文件全部删除,`grep -r SenseVoice src/ tests/` 无
      匹配(除了 docs/ 里的历史文档,那些保留)
- [ ] `uv sync` 成功,新依赖都装上
- [ ] `uv run pytest` 全绿,覆盖率报告 ≥ 70%
- [ ] `uv run ruff check .` 无警告

## 工作量估算

| 阶段 | 任务 | 估时 |
|---|---|---|
| A | 目录搭建 + downloader + 单测 | 0.5 天 |
| | Log-mel 特征提取 + golden + 单测 | **1.5 天** |
| | Tokenizer 包装 + 单测 | 0.5 天 |
| | Prompt / postprocess + 单测 | 0.5 天 |
| | ONNX runner + 单测(真实模型) | 1 天 |
| | Qwen3ASRSTT 主类 + 端到端 smoke | 0.5 天 |
| | 阶段 A 联调 + bug 修 | 0.5 天 |
| **阶段 A 小计** | | **5 天** |
| B | stt 工厂 + config 迁移 | 0.5 天 |
| | 设置页 UI(前端 + 后端 + 单测) | 1 天 |
| | 热切换逻辑 + 单测 | 0.5 天 |
| | 删除 SenseVoice + 依赖清理 | 0.5 天 |
| | CLAUDE.md / BACKLOG.md / README 更新 | 0.5 天 |
| | 测试补齐 + 覆盖率推到 70% | 1 天 |
| | 端到端验证 + 回归测 + bug 修 | 1 天 |
| **阶段 B 小计** | | **5 天** |
| **总计** | | **~10 工作日 / 2 周** |

## 风险清单

| 风险 | 影响 | 缓解 |
|---|---|---|
| Log-mel 对不齐 Whisper 约定 | 识别崩 | Golden 文件回归测试,阶段 A 早验 |
| Tokenizer 没 tokenizer.json,`tokenizers` 库构建失败 | 阶段 A 卡住 | 降级:pin transformers 最小子集(违约束,最后手段) |
| KV cache 28 × 1024 × 8 × 128 × 4 ≈ 115 MB | 单次推理内存占用 | 本轮可接受 |
| Qwen3-ASR 输出 token 不稳定(同音频不同运行结果不同) | 端到端 smoke 难写 | 用 greedy 解码 + 固定随机 seed + 音频内容宽松匹配 |
| 切换 1.7B ↔ 0.6B 时 ORT session 释放不净 | 内存累积 | `del` + `gc.collect()`,手动测 RSS |
| ModelScope 在 CI 里下载超时 | CI 失败 | `actions/cache@v4` 缓存,cache key v1 首次拉满 |
| Wasser1462 license 不明 | 发布阻塞 | 本轮暂搁,发布前解决 |

## 执行顺序

以下步骤严格顺序执行,**每步完成前不开始下一步**:

1. **落地 spike 模型 cache**:确认 `/tmp/qwen3-asr-spike` 还在,或者重
   新走 modelscope download 到系统 cache
2. 写 `_feature.py` + `scripts/generate_whisper_mel_golden.py` + golden
   测试 **→ 必须通过**
3. 写 `_tokenizer.py` + 单测
4. 写 `_prompt.py` + 单测
5. 写 `_postprocess.py` + 单测
6. 写 `_downloader.py` + 单测
7. 写 `_onnx_runner.py` + 单测(这一步要用真实模型,所以 CI cache 也要
   先配好)
8. 写 `qwen3_asr.py` + 端到端 smoke test
9. **阶段 A 验收 checkpoint**
10. 改 `stt/__init__.py`
11. 改 `config.example.yaml` + config_manager 迁移
12. 改 `settings_server.py` + 前端模板
13. 在 WhisperInput 主控加 `_switch_stt_variant`
14. 删除 SenseVoice 文件 + pyproject.toml 依赖
15. 改 CLAUDE.md / BACKLOG.md / README
16. 整体跑 `uv run pytest` + `uv run ruff check .` 全绿
17. 手动验证:0.6B 录音识别 → 切 1.7B 再录音 → 切回 0.6B
18. **阶段 B 验收 checkpoint**
19. 写 SUMMARY.md,收尾轮次
