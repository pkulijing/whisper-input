"""STT 后端抽象包。

当前只有一种后端:
  - sensevoice: SenseVoice-Small int8 ONNX,用 onnxruntime 裸跑

未来扩展点(已预留):
  - fun_asr_nano: Fun-ASR-Nano(更高精度、含热词、支持流式)
  - qwen3_asr:    Qwen3-ASR(待上游出 ONNX 落地件)
新增后端时只需在 stt/ 下加一个模块 + 在 create_stt 里加一个分支。

**懒加载原则**:本包的 __init__.py 刻意不做任何 eager 导入 —— numpy /
onnxruntime / modelscope 的 import 成本留给真正需要推理时再付,让
`whisper-input --help` 之类的轻量调用路径保持启动毫秒级。
"""

from whisper_input.stt.base import BaseSTT


def create_stt(engine: str, config: dict) -> BaseSTT:
    """根据 engine 名称和配置创建 STT 实例。"""
    if engine == "sensevoice":
        # 延迟 import:只有真正需要推理时才触发 numpy/onnxruntime 加载
        from whisper_input.stt.sense_voice import SenseVoiceSTT

        return SenseVoiceSTT(
            use_itn=config.get("use_itn", True),
        )
    from whisper_input.i18n import t

    raise ValueError(t("stt.unknown_engine", engine=engine))


__all__ = ["BaseSTT", "create_stt"]
