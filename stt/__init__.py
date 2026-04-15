"""STT 后端抽象包。

当前只有一种后端:
  - sensevoice: SenseVoice-Small int8 ONNX,用 onnxruntime 裸跑

未来扩展点(已预留):
  - fun_asr_nano: Fun-ASR-Nano(更高精度、含热词、支持流式)
  - qwen3_asr:    Qwen3-ASR(待上游出 ONNX 落地件)
新增后端时只需在 stt/ 下加一个模块 + 在 create_stt 里加一个分支。

**懒加载原则**:本包的 __init__.py 刻意不做任何 eager 导入 —— setup_window
引导向导跑在 bundled python-build-standalone 里,没有 numpy / onnxruntime
等第三方库,但它需要 import stt.downloader(纯 stdlib)。eager 导入
SenseVoiceSTT 会触发 numpy import 链,导致引导阶段崩溃。
"""

from stt.base import BaseSTT


def create_stt(engine: str, config: dict) -> BaseSTT:
    """根据 engine 名称和配置创建 STT 实例。"""
    if engine == "sensevoice":
        # 延迟 import:只有真正需要推理时才触发 numpy/onnxruntime 加载
        from stt.sense_voice import SenseVoiceSTT

        return SenseVoiceSTT(
            language=config.get("language", "auto"),
            use_itn=config.get("use_itn", True),
        )
    raise ValueError(f"未知的 STT 引擎: {engine}")


__all__ = ["BaseSTT", "create_stt"]
