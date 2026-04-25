"""STT 后端极简抽象基类。

离线模式两个必实现方法:
  - load():      加载/准备模型(可能触发首次下载)
  - transcribe:  把 16kHz 单声道 WAV bytes 转成识别文本

流式模式三件套(可选,子类通过把 supports_streaming 置 True 并实现):
  - supports_streaming: 类变量,True 表示支持流式
  - init_stream_state(): 新建一次"按键→说话→松手"周期的私有状态对象
  - stream_step(audio_chunk, state, is_last): 增量喂音频,返回 StreamEvent

刻意不放 device/config 等字段 —— 后端各自的初始化参数由子类
的构造函数接收,避免基类膨胀。state 用 Any 而不是 dataclass —— 它是
引擎私有(Qwen3 里装了 KV cache / audio_features buffer 等),别的引擎
没必要遵循同一结构。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

# 流式编排层共用的 chunk 粒度。引擎可以接受任意长度的 chunk,但编排层需要
# 一个统一值决定"多久触发一次 stream_step"。
# 试过 1.0 —— 模型单 chunk 音频太短,早期 commit 的 token 往往错(把"先帝"听成
# "先地"这类),后面 chunk 基于错误前缀继续生成就一路走偏,最终 transcript
# 满是重复残渣。2.0 给模型足够上下文,是质量和延迟的现实折中。若要更低延迟
# 需要换模型 / 加 rescoring,不是改 chunk size 能解决的。
STREAMING_CHUNK_SEC = 2.0
STREAMING_CHUNK_SAMPLES = int(STREAMING_CHUNK_SEC * 16000)


class StreamingKVOverflowError(RuntimeError):
    """流式识别时 KV cache 容量不够。

    语义上是"连续说话超过引擎能处理的上限"。WhisperInput 层捕获后应 flush
    已 committed 的文本并弹 toast 提示用户分段(见
    ``main.streaming_overflow`` i18n key)。

    放在 ``stt.base`` 而不是 ``stt.qwen3._stream`` 是因为其他后端未来实现
    流式时也会抛同类错误,编排层的捕获点应该跟引擎无关。
    """


@dataclass
class StreamEvent:
    """一次 stream_step 的产出,供编排层决定粘贴什么。"""

    committed_delta: str
    """本步新 commit 的文本增量(相对上次 committed_text 的尾部新增)。
    编排层直接把这段文字 paste 到焦点窗口。"""

    pending_text: str
    """本步生成但未 commit 的尾部文本(= pending_tokens 的解码)。
    本轮不用,留给后续"流式 preview 灰色浮窗"功能。"""

    is_final: bool
    """is_last=True 调用时置 True,通知编排层这一 stream 结束。"""


class BaseSTT(ABC):
    # 默认不支持流式;子类按需覆盖为 True 并实现下面两个方法
    supports_streaming: ClassVar[bool] = False

    @abstractmethod
    def load(self) -> None:
        """加载模型到内存,多次调用应幂等。"""

    @abstractmethod
    def transcribe(self, wav_data: bytes) -> str:
        """把 16kHz 16bit 单声道 WAV bytes 转成文本。

        空输入返回 "",过短(< 0.1s)也返回 ""。
        """

    def init_stream_state(self) -> Any:
        """为一次"按键→说话→松手"周期创建私有流式状态。

        子类若 supports_streaming=True 必须实现。
        """
        raise NotImplementedError

    def stream_step(
        self,
        audio_chunk: np.ndarray,
        state: Any,
        is_last: bool,
    ) -> StreamEvent:
        """增量喂一段音频 chunk,返回本步 committed 增量。

        Parameters
        ----------
        audio_chunk:
            float32 1D array,16kHz 单声道,长度任意(通常 ~2s)。
            空数组 (``len==0``) 合法 —— 用于 ``is_last=True`` 的纯 flush 调用。
        state:
            :meth:`init_stream_state` 返回的对象,会被原地修改。
        is_last:
            True 时触发最后一次 flush(所有 pending token 全部 commit,生成到
            EOS 或上限)。

        Returns
        -------
        StreamEvent
            本步的 committed 增量 + pending 文本 + 是否结束。

        子类若 supports_streaming=True 必须实现。
        """
        raise NotImplementedError
