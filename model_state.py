"""SenseVoice 模型本地状态 —— stt/model_paths.py 的兼容壳。

历史上这个模块直接管 FunASR + PyTorch 版 SenseVoice 的 modelscope 缓存,
迁移到 sherpa-onnx + ONNX 后真正的实现搬到了 stt/model_paths.py。本文件
保留以下顶层符号,让 debian/setup_window.py 和 macos/setup_window.py 的
老 import 路径 `from model_state import find_local_model, save_state`
继续有效:

  - find_local_model(model_id=...) -> str | None
  - save_state(model_id: str, model_path: str) -> None
  - DEFAULT_MODEL_ID

参数签名刻意和老版本保持兼容(接收一个 model_id 字符串),但内部忽略它 ——
现在只有一个 SenseVoice 版本,由 stt/model_paths.py 硬编码。
"""

from pathlib import Path

from stt.model_paths import (
    MODEL_VERSION,
)
from stt.model_paths import (
    find_local_model as _find,
)
from stt.model_paths import (
    save_manifest as _save,
)

# 历史常量,保持老调用方的符号可 import。语义已经变了:不再是 modelscope
# 的 "owner/name" 风格 id,而是 sherpa-onnx 模型版本字符串。老调用方传
# 进来的 "iic/SenseVoiceSmall" 会被 find_local_model 忽略。
DEFAULT_MODEL_ID = MODEL_VERSION


def find_local_model(model_id: str = DEFAULT_MODEL_ID) -> str | None:
    """查找本地已缓存的 SenseVoice 模型目录,返回字符串路径或 None。

    参数 model_id 保留是为了兼容老调用方,实际不使用 —— 现在整个项目
    只认一个由 stt/model_paths.py MODEL_VERSION 锁定的模型版本。
    """
    del model_id
    p = _find()
    return str(p) if p is not None else None


def save_state(model_id: str, model_path: str) -> None:
    """把给定目录登记为当前 SenseVoice 模型的 manifest。

    参数 model_id 同上,保留兼容签名但内部用 MODEL_VERSION 写入。
    """
    del model_id
    _save(MODEL_VERSION, Path(model_path))
