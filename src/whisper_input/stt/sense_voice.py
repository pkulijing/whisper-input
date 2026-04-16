"""SenseVoice 本地 STT —— 基于 Microsoft onnxruntime + 达摩院官方 ONNX。

依赖链:
  stt.sense_voice
    ├─ stt._wav_frontend  (port 自 funasr_onnx,kaldi_native_fbank)
    ├─ stt._tokenizer     (port 自 funasr_onnx,sentencepiece)
    ├─ stt._postprocess   (port 自 funasr_onnx,纯字符串处理)
    ├─ modelscope         (官方库的 snapshot_download,只拉 hub base 依赖)
    ├─ onnxruntime        (Microsoft 官方 PyPI 包)
    ├─ yaml               (读 config.yaml 的 frontend_conf)
    └─ numpy

整个推理链路和 FunASR 原版 bit-aligned,但不依赖 torch / funasr / sherpa-onnx。
"""

import io
import wave
from pathlib import Path

import numpy as np

from whisper_input.stt.base import BaseSTT

_SAMPLE_RATE = 16000
_BLANK_ID = 0

# FunASR SenseVoiceSmall 的语种和 text_norm prompt id 常量
# 来自 https://github.com/FunAudioLLM/SenseVoice/blob/main/model.py
# 也和 funasr_onnx/sensevoice_bin.py 里的 lid_dict/textnorm_dict 一致
_LANG_ID = {
    "auto": 0,
    "zh": 3,
    "en": 4,
    "yue": 7,
    "ja": 11,
    "ko": 12,
    "nospeech": 13,
}
_WITH_ITN = 14
_WITHOUT_ITN = 15


class SenseVoiceSTT(BaseSTT):
    """SenseVoice-Small 官方量化 ONNX 离线推理。

    首次 load() 若本地缓存不存在,会从 ModelScope 自动下载 ~231 MB(5 个
    文件),之后永久离线。
    """

    def __init__(
        self,
        use_itn: bool = True,
        num_threads: int = 4,
    ):
        self.use_itn = use_itn
        self.num_threads = num_threads
        self._session = None
        self._frontend = None
        self._tokenizer = None

    def load(self) -> None:
        if self._session is not None:
            return

        # 延迟 import 第三方库,让 `from whisper_input.stt import ...` 在
        # 不真正推理的场景(CLI --help、测试)下不用背 numpy/onnxruntime 启动成本
        from modelscope import snapshot_download

        from whisper_input.i18n import t

        print(f"[sensevoice] {t('sensevoice.preparing')}")
        # 主仓库:ONNX 量化模型 + tokens.json + am.mvn + config.yaml(4 个文件,~231 MB)
        onnx_dir = Path(snapshot_download("iic/SenseVoiceSmall-onnx"))
        # 姐妹仓库是 PyTorch 原版,体积 ~900 MB。这里只为取 BPE tokenizer 一个文件,
        # allow_patterns 限制只下载这一个,避免误拉权重
        bpe_dir = Path(
            snapshot_download(
                "iic/SenseVoiceSmall",
                allow_patterns=["chn_jpn_yue_eng_ko_spectok.bpe.model"],
            )
        )
        bpe_file = bpe_dir / "chn_jpn_yue_eng_ko_spectok.bpe.model"

        print(f"[sensevoice] {t('sensevoice.loading', path=onnx_dir)}")

        import onnxruntime as ort
        import yaml

        from whisper_input.stt._postprocess import (
            rich_transcription_postprocess,
        )
        from whisper_input.stt._tokenizer import SentencepiecesTokenizer
        from whisper_input.stt._wav_frontend import WavFrontend

        self._postprocess = rich_transcription_postprocess

        config = yaml.safe_load(
            (onnx_dir / "config.yaml").read_text(encoding="utf-8")
        )
        frontend_conf = dict(config["frontend_conf"])
        frontend_conf["cmvn_file"] = str(onnx_dir / "am.mvn")
        # 推理时强制 dither=0 保证确定性(config.yaml 里默认没写,WavFrontend
        # 默认是 1.0 会引入噪声扰动)
        frontend_conf["dither"] = 0
        self._frontend = WavFrontend(**frontend_conf)

        self._tokenizer = SentencepiecesTokenizer(bpemodel=str(bpe_file))

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = self.num_threads
        self._session = ort.InferenceSession(
            str(onnx_dir / "model_quant.onnx"),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        print(
            f"[sensevoice] "
            f"{t('sensevoice.loaded', threads=self.num_threads)}"
        )

    def transcribe(self, wav_data: bytes) -> str:
        """16kHz 16bit 单声道 WAV bytes → 识别文本。"""
        if not wav_data:
            return ""
        self.load()

        buf = io.BytesIO(wav_data)
        with wave.open(buf, "rb") as wf:
            # 解码成归一化 float32 [-1, 1]
            # WavFrontend 内部会再 *32768 还原到 int16 尺度,
            # 这是 FunASR 约定的输入契约
            audio = (
                np.frombuffer(
                    wf.readframes(wf.getnframes()), dtype=np.int16
                )
                .astype(np.float32)
                / 32768.0
            )

        if len(audio) < 1600:  # < 0.1s
            return ""

        feat, _ = self._frontend.fbank(audio)
        feat, _ = self._frontend.lfr_cmvn(feat)
        # feat shape: (T_lfr, 560)

        x = feat[np.newaxis, :, :].astype(np.float32)
        xl = np.array([feat.shape[0]], dtype=np.int32)
        lang = np.array(
            [_LANG_ID["auto"]],
            dtype=np.int32,
        )
        tn = np.array(
            [_WITH_ITN if self.use_itn else _WITHOUT_ITN], dtype=np.int32
        )

        ctc_logits, encoder_out_lens = self._session.run(
            ["ctc_logits", "encoder_out_lens"],
            {
                "speech": x,
                "speech_lengths": xl,
                "language": lang,
                "textnorm": tn,
            },
        )

        # (T, vocab) 取有效长度内的 logits
        logits = ctc_logits[0, : encoder_out_lens[0], :]
        yseq = logits.argmax(axis=-1)

        # CTC 去连续重复
        mask = np.concatenate(([True], np.diff(yseq) != 0))
        yseq = yseq[mask]
        # 过滤 blank
        token_int = yseq[yseq != _BLANK_ID].tolist()

        raw = self._tokenizer.decode(token_int)
        return self._postprocess(raw)
