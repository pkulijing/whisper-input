"""SenseVoice 本地 STT —— 基于 Microsoft onnxruntime + 达摩院官方 ONNX。

依赖链:
  stt.sense_voice
    ├─ stt._wav_frontend  (port 自 funasr_onnx,kaldi_native_fbank)
    ├─ stt._tokenizer     (port 自 funasr_onnx,sentencepiece)
    ├─ stt._postprocess   (port 自 funasr_onnx,纯字符串处理)
    ├─ stt.model_paths    (纯 stdlib,模型版本 + 缓存路径)
    ├─ stt.downloader     (纯 stdlib,从 ModelScope 直连下载)
    ├─ onnxruntime        (Microsoft 官方 PyPI 包)
    ├─ yaml               (读 config.yaml 的 frontend_conf)
    └─ numpy

整个推理链路和 FunASR 原版 bit-aligned,但不依赖 torch / funasr / sherpa-onnx。
"""

import io
import wave

import numpy as np

from stt.base import BaseSTT
from stt.downloader import download_model
from stt.model_paths import find_local_model

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
        language: str = "auto",
        use_itn: bool = True,
        num_threads: int = 4,
    ):
        self.language = language
        self.use_itn = use_itn
        self.num_threads = num_threads
        self._session = None
        self._frontend = None
        self._tokenizer = None

    def load(self) -> None:
        if self._session is not None:
            return

        model_dir = find_local_model()
        if model_dir is None:
            print("[sensevoice] 未发现本地模型,开始下载...")
            model_dir = download_model()

        print(f"[sensevoice] 加载 SenseVoice ONNX: {model_dir}")

        # 延迟 import,避免上游 setup_window 引导进程误触发第三方库加载
        import onnxruntime as ort
        import yaml

        from stt._postprocess import rich_transcription_postprocess
        from stt._tokenizer import SentencepiecesTokenizer
        from stt._wav_frontend import WavFrontend

        self._postprocess = rich_transcription_postprocess

        # 读 config.yaml 的 frontend_conf,然后 override 关键项
        config = yaml.safe_load(
            (model_dir / "config.yaml").read_text(encoding="utf-8")
        )
        frontend_conf = dict(config["frontend_conf"])
        frontend_conf["cmvn_file"] = str(model_dir / "am.mvn")
        # 推理时强制 dither=0 保证确定性(config.yaml 里默认没写,WavFrontend
        # 默认是 1.0 会引入噪声扰动)
        frontend_conf["dither"] = 0
        self._frontend = WavFrontend(**frontend_conf)

        self._tokenizer = SentencepiecesTokenizer(
            bpemodel=str(model_dir / "chn_jpn_yue_eng_ko_spectok.bpe.model")
        )

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = self.num_threads
        self._session = ort.InferenceSession(
            str(model_dir / "model_quant.onnx"),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        print(
            f"[sensevoice] 模型加载完成"
            f" (num_threads={self.num_threads})"
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
            [_LANG_ID.get(self.language, _LANG_ID["auto"])],
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
