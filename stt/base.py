"""STT 后端极简抽象基类。

只有两个方法:
  - load():      加载/准备模型(可能触发首次下载)
  - transcribe:  把 16kHz 单声道 WAV bytes 转成识别文本

刻意不放 device/config 等字段 —— 后端各自的初始化参数由子类
的构造函数接收,避免基类膨胀。
"""

from abc import ABC, abstractmethod


class BaseSTT(ABC):
    @abstractmethod
    def load(self) -> None:
        """加载模型到内存,多次调用应幂等。"""

    @abstractmethod
    def transcribe(self, wav_data: bytes) -> str:
        """把 16kHz 16bit 单声道 WAV bytes 转成文本。

        空输入返回 "",过短(< 0.1s)也返回 ""。
        """
