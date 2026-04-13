"""SenseVoice 本地语音识别引擎。"""

import io
import wave

import numpy as np


class SenseVoiceSTT:
    """基于 FunASR SenseVoice-Small 的本地 STT。

    首次调用时加载模型（约 2-3 秒），之后推理极快。
    """

    def __init__(
        self,
        model: str = "iic/SenseVoiceSmall",
        device_priority: list[str] | None = None,
        language: str = "auto",
    ):
        self.model_name = model
        self.device_priority = device_priority or [
            "cuda", "mps", "cpu",
        ]
        self.device: str | None = None  # 实际使用的设备，加载时确定
        self.language = language
        self._model = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return

        self.device = self._select_device(self.device_priority)
        print(
            f"[sensevoice] 正在加载模型 {self.model_name}"
            f" (device={self.device}) ..."
        )
        from funasr import AutoModel

        # 注意：绝对**不能**传 trust_remote_code=True。
        # funasr 的 AutoModel 在 trust_remote_code=True 时会调
        # import_module_from_path("./model.py")，该函数把 "." 加到 sys.path
        # 然后 import_module("model")，也就是从**进程 cwd** 找 model.py——
        # cwd 是 /opt/whisper-input 或仓库根，根本没有这文件，
        # 报 "Loading remote code failed: ./model.py, No module named 'model'"。
        #
        # SenseVoiceSmall 的类定义在 funasr.models.sense_voice.model 里，
        # funasr 包的 __init__.py 导入时 import_submodules 会递归触发
        # @tables.register("model_classes", "SenseVoiceSmall") 装饰器把它注册
        # 进全局 tables。AutoModel 按 config.yaml 里的 model: SenseVoiceSmall
        # 直接从 tables 查类，**不需要** remote_code 路径。
        #
        # 这行坑曾两次出现（ca4b139 错删正确实现、后续有人又错加 remote_code
        # 想"修复"），每次都是因为读 funasr README 照搬示例而示例本身误导。
        # 请不要再加回来。
        self._model = AutoModel(
            model=self.model_name,
            device=self.device,
            disable_update=True,
        )
        print("[sensevoice] 模型加载完成")

    @staticmethod
    def _select_device(priority: list[str]) -> str:
        """按优先级列表选择第一个可用的设备。"""
        try:
            import torch
        except ImportError:
            return "cpu"

        for device in priority:
            if device == "cuda" and torch.cuda.is_available():
                return "cuda"
            if device == "mps" and (
                hasattr(torch.backends, "mps")
                and torch.backends.mps.is_available()
            ):
                return "mps"
            if device == "cpu":
                return "cpu"

        return "cpu"

    def transcribe(self, wav_data: bytes) -> str:
        """将 WAV 音频数据转为文字。

        Args:
            wav_data: 16kHz 16bit 单声道 WAV 格式字节数据

        Returns:
            识别出的文字
        """
        if not wav_data:
            return ""

        self._ensure_model()

        # 解析 WAV 数据为 numpy 数组
        buf = io.BytesIO(wav_data)
        with wave.open(buf, "rb") as wf:
            audio_bytes = wf.readframes(wf.getnframes())
            audio = (
                np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)
                / 32768.0
            )

        # 检查音频长度，太短则跳过
        if len(audio) < 1600:  # < 0.1s
            return ""

        result = self._model.generate(
            input=audio,
            cache={},
            language=self.language,
            use_itn=True,  # 逆文本正则化（数字、日期等）
        )

        if result and len(result) > 0 and "text" in result[0]:
            text = result[0]["text"]
            # SenseVoice 输出可能带有特殊标签如 <|zh|><|NEUTRAL|><|Speech|>，需要清理
            text = self._clean_text(text)
            return text
        return ""

    @staticmethod
    def _clean_text(text: str) -> str:
        """清理 SenseVoice 输出中的特殊标签。"""
        import re

        # 移除 <|...|> 格式的标签
        text = re.sub(r"<\|[^|]*\|>", "", text)
        return text.strip()
