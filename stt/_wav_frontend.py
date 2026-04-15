"""SenseVoice WavFrontend —— 从 funasr_onnx 移植的特征提取。

Port 来源:
  https://github.com/modelscope/FunASR/blob/main/runtime/python/onnxruntime/funasr_onnx/utils/frontend.py

作者 / 版权:
  Copyright FunASR (https://github.com/alibaba-damo-academy/FunASR)
  MIT License (https://opensource.org/licenses/MIT)
  Speech Lab of DAMO Academy, Alibaba Group

为什么 port 而不是 pip install funasr_onnx:
  funasr_onnx 包顶层依赖 librosa + scipy + jieba + sentencepiece,其中 librosa
  会触发 numba/llvmlite 一大坨编译器基础设施(几十 MB),jieba 是给另一个模型
  (CT-Transformer 标点)用的,scipy 源码 0 处 import(死依赖)。我们只需要
  WavFrontend 这个类 + load_cmvn 函数(总共约 100 行),依赖仅有 numpy 和
  kaldi-native-fbank。所以 port 这一个文件是最小代价的干净做法。

和原版的差异:
  - 只保留离线推理需要的部分(WavFrontend.__init__ / fbank / lfr_cmvn /
    apply_lfr / apply_cmvn 和 load_cmvn 顶层函数)
  - 去掉 WavFrontendOnline 类(流式推理用,我们不需要)
  - 去掉 SinusoidalPositionEncoderOnline / load_bytes / test() 等无关工具
  - 默认 dither=1.0 不变(和上游一致),但推理时调用方应该显式传 dither=0
    保证确定性(参见 stt/sense_voice.py 里的用法)

fbank + LFR + CMVN 数值和 FunASR 的 torch 版 WavFrontend bit-aligned,
因为都用同一个 kaldi_native_fbank C++ 库。
"""
# Port 自上游 funasr_onnx,保留原始大写变量名(T/LFR_inputs/LFR_outputs)
# 以便未来和 upstream 做 diff,ruff 的 N806 在本文件整体禁用。
# ruff: noqa: N806

from functools import lru_cache
from pathlib import Path

import kaldi_native_fbank as knf
import numpy as np


class WavFrontend:
    """SenseVoice-Small / Paraformer 系列的常规特征提取管线。

    用法:
        fe = WavFrontend(
            cmvn_file="/path/to/am.mvn",
            fs=16000, window="hamming", n_mels=80,
            frame_length=25, frame_shift=10,
            lfr_m=7, lfr_n=6,
            dither=0,  # 推理时传 0 保证确定性
        )
        feat, _ = fe.fbank(waveform)           # (T, 80)  float32
        feat, _ = fe.lfr_cmvn(feat)            # (T', 560) float32
    """

    def __init__(
        self,
        cmvn_file: str | None = None,
        fs: int = 16000,
        window: str = "hamming",
        n_mels: int = 80,
        frame_length: int = 25,
        frame_shift: int = 10,
        lfr_m: int = 1,
        lfr_n: int = 1,
        dither: float = 1.0,
        **kwargs,
    ) -> None:
        opts = knf.FbankOptions()
        opts.frame_opts.samp_freq = fs
        opts.frame_opts.dither = dither
        opts.frame_opts.window_type = window
        opts.frame_opts.frame_shift_ms = float(frame_shift)
        opts.frame_opts.frame_length_ms = float(frame_length)
        opts.mel_opts.num_bins = n_mels
        opts.energy_floor = 0
        opts.frame_opts.snip_edges = True
        opts.mel_opts.debug_mel = False
        self.opts = opts

        self.lfr_m = lfr_m
        self.lfr_n = lfr_n
        self.cmvn_file = cmvn_file

        if self.cmvn_file:
            self.cmvn = load_cmvn(self.cmvn_file)
        else:
            self.cmvn = None

    def fbank(
        self, waveform: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """计算 80 维 fbank 特征。

        waveform: 1-D float32,归一化到 [-1, 1]。内部会 *32768 转成 int16
        尺度供 kaldi-native-fbank 使用(FunASR 的约定)。
        """
        waveform = waveform * (1 << 15)
        fbank_fn = knf.OnlineFbank(self.opts)
        fbank_fn.accept_waveform(
            self.opts.frame_opts.samp_freq, waveform.tolist()
        )
        frames = fbank_fn.num_frames_ready
        mat = np.empty([frames, self.opts.mel_opts.num_bins])
        for i in range(frames):
            mat[i, :] = fbank_fn.get_frame(i)
        feat = mat.astype(np.float32)
        feat_len = np.array(mat.shape[0]).astype(np.int32)
        return feat, feat_len

    def lfr_cmvn(
        self, feat: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """应用 LFR 拼帧(lfr_m, lfr_n) + CMVN 归一化。"""
        if self.lfr_m != 1 or self.lfr_n != 1:
            feat = self.apply_lfr(feat, self.lfr_m, self.lfr_n)

        if self.cmvn is not None:
            feat = self.apply_cmvn(feat)

        feat_len = np.array(feat.shape[0]).astype(np.int32)
        return feat, feat_len

    @staticmethod
    def apply_lfr(
        inputs: np.ndarray, lfr_m: int, lfr_n: int
    ) -> np.ndarray:
        """FunASR 风格的 LFR(Low Frame Rate)拼帧。

        左边补 (lfr_m - 1) // 2 帧,然后从头到尾每 lfr_n 步拼 lfr_m 帧成
        一个 LFR 大帧。最后一帧如果不够 lfr_m,用最后一帧重复补齐。
        """
        LFR_inputs = []

        T = inputs.shape[0]
        T_lfr = int(np.ceil(T / lfr_n))
        left_padding = np.tile(inputs[0], ((lfr_m - 1) // 2, 1))
        inputs = np.vstack((left_padding, inputs))
        T = T + (lfr_m - 1) // 2
        for i in range(T_lfr):
            if lfr_m <= T - i * lfr_n:
                LFR_inputs.append(
                    (inputs[i * lfr_n : i * lfr_n + lfr_m]).reshape(1, -1)
                )
            else:
                # process last LFR frame
                num_padding = lfr_m - (T - i * lfr_n)
                frame = inputs[i * lfr_n :].reshape(-1)
                for _ in range(num_padding):
                    frame = np.hstack((frame, inputs[-1]))
                LFR_inputs.append(frame)
        LFR_outputs = np.vstack(LFR_inputs).astype(np.float32)
        return LFR_outputs

    def apply_cmvn(self, inputs: np.ndarray) -> np.ndarray:
        """(inputs + neg_mean) * inv_stddev,都来自 am.mvn 文件。"""
        frame, dim = inputs.shape
        means = np.tile(self.cmvn[0:1, :dim], (frame, 1))
        varss = np.tile(self.cmvn[1:2, :dim], (frame, 1))
        inputs = (inputs + means) * varss
        return inputs


@lru_cache
def load_cmvn(cmvn_file: str | Path) -> np.ndarray:
    """从 FunASR 的 am.mvn 文件加载 CMVN 参数,返回 shape (2, dim)。

    第 0 行是 neg_mean (来自 <AddShift> 块),
    第 1 行是 inv_stddev (来自 <Rescale> 块)。
    """
    cmvn_file = Path(cmvn_file)
    if not cmvn_file.exists():
        raise FileNotFoundError(f"cmvn file not found: {cmvn_file}")

    with open(cmvn_file, encoding="utf-8") as f:
        lines = f.readlines()
    means_list = []
    vars_list = []
    for i in range(len(lines)):
        line_item = lines[i].split()
        if not line_item:
            continue
        if line_item[0] == "<AddShift>":
            line_item = lines[i + 1].split()
            if line_item[0] == "<LearnRateCoef>":
                add_shift_line = line_item[3 : (len(line_item) - 1)]
                means_list = list(add_shift_line)
                continue
        elif line_item[0] == "<Rescale>":
            line_item = lines[i + 1].split()
            if line_item[0] == "<LearnRateCoef>":
                rescale_line = line_item[3 : (len(line_item) - 1)]
                vars_list = list(rescale_line)
                continue

    means = np.array(means_list).astype(np.float64)
    varss = np.array(vars_list).astype(np.float64)
    cmvn = np.array([means, varss])
    return cmvn
