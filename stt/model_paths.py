"""SenseVoice ONNX 模型的版本常量、路径与本地缓存清单。

**必须纯 stdlib** —— debian/setup_window.py 和 macos/setup_window.py 的
引导向导阶段运行在 bundled python-build-standalone 环境,此时 user venv
还没建好,不能依赖 numpy/yaml/onnxruntime 之类的第三方库。

模型来源: 达摩院官方的两个 ModelScope 仓库
  - iic/SenseVoiceSmall-onnx       (ONNX 量化模型 + tokens + am.mvn + config)
  - iic/SenseVoiceSmall            (PyTorch 版仓库,但这里只用它的 BPE tokenizer 文件)

为什么 BPE 从另一个仓库拿: ONNX 仓库刻意省掉了 BPE 模型文件,但我们 port 的
SentencepiecesTokenizer 必须要这个文件。升级模型时需要同步更新这里的 5 个
SHA256 和 size。
"""

import json
import os
import sys
from pathlib import Path

# ==== 模型版本锁 ====
# iic 达摩院官方量化版本,由训练 SenseVoice 的同一个团队维护,被 FunASR
# 生产级 runtime SDK 使用(77 万次下载)。质量和 fp32 等价,体积 230 MB。
MODEL_VERSION = "iic-SenseVoiceSmall-onnx-v1"

# 本地缓存目录名
MODEL_DIR_NAME = "iic-SenseVoiceSmall-onnx"

# 5 个要下载的文件,全部来自 ModelScope 官方仓库
# 格式: (仓库路径, 文件名, 字节数, sha256)
MODEL_FILES: tuple[tuple[str, str, int, str], ...] = (
    (
        "iic/SenseVoiceSmall-onnx",
        "model_quant.onnx",
        241216270,
        "21dc965f689a78d1604717bf561e40d5a236087c85a95584567835750549e822",
    ),
    (
        "iic/SenseVoiceSmall-onnx",
        "tokens.json",
        352064,
        "a2594fc1474e78973149cba8cd1f603ebed8c39c7decb470631f66e70ce58e97",
    ),
    (
        "iic/SenseVoiceSmall-onnx",
        "am.mvn",
        11203,
        "29b3c740a2c0cfc6b308126d31d7f265fa2be74f3bb095cd2f143ea970896ae5",
    ),
    (
        "iic/SenseVoiceSmall-onnx",
        "config.yaml",
        1855,
        "f71e239ba36705564b5bf2d2ffd07eece07b8e3f2bbf6d2c99d8df856339ac19",
    ),
    (
        # BPE tokenizer 来自 PyTorch 版的姐妹仓库,
        # -onnx 仓库刻意没打包这个文件
        "iic/SenseVoiceSmall",
        "chn_jpn_yue_eng_ko_spectok.bpe.model",
        377341,
        "aa87f86064c3730d799ddf7af3c04659151102cba548bce325cf06ba4da4e6a8",
    ),
)

# 简单列表供 is_model_complete 等函数使用
REQUIRED_FILES: tuple[str, ...] = tuple(f[1] for f in MODEL_FILES)

# 估计的总下载体积(字节),供进度回调计算百分比
TOTAL_BYTES: int = sum(f[2] for f in MODEL_FILES)


def modelscope_file_url(repo: str, filename: str) -> str:
    """ModelScope 匿名文件下载 URL。不需要 token,国内 CDN 直连。"""
    return (
        f"https://www.modelscope.cn/api/v1/models/{repo}"
        f"/repo?Revision=master&FilePath={filename}"
    )


def user_data_dir() -> Path:
    """whisper-input 在用户家目录下的数据目录。

    macOS:  ~/Library/Application Support/Whisper Input
    Linux:  $XDG_DATA_HOME/whisper-input 或 ~/.local/share/whisper-input
    """
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Whisper Input"
    xdg = os.environ.get("XDG_DATA_HOME") or str(
        Path.home() / ".local/share"
    )
    return Path(xdg) / "whisper-input"


def models_root() -> Path:
    """所有 STT 后端模型的根目录。"""
    return user_data_dir() / "models"


def sense_voice_model_dir() -> Path:
    """当前 SenseVoice 模型版本的默认目录。"""
    return models_root() / MODEL_DIR_NAME


def manifest_path() -> Path:
    """本地缓存清单文件路径(和 model_state.py 历史位置一致)。"""
    return user_data_dir() / ".model_state.json"


def is_model_complete(model_dir: Path) -> bool:
    """检查 model_dir 下 REQUIRED_FILES 全部非空存在。"""
    if not model_dir.is_dir():
        return False
    for name in REQUIRED_FILES:
        f = model_dir / name
        try:
            if not f.is_file() or f.stat().st_size == 0:
                return False
        except OSError:
            return False
    return True


def load_manifest() -> dict | None:
    """读 manifest,损坏或不存在都返回 None。"""
    p = manifest_path()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_manifest(model_version: str, model_dir: Path) -> None:
    """原子写入 manifest(.tmp + os.replace)。"""
    p = manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model_version": model_version,
        "model_path": str(model_dir),
        "files": list(REQUIRED_FILES),
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def find_local_model() -> Path | None:
    """查找本地已缓存的 SenseVoice 模型目录,找不到返回 None。

    查找顺序:
      1. manifest 里记录的路径(只认当前 MODEL_VERSION)
      2. 默认目录 models_root() / MODEL_DIR_NAME
    """
    state = load_manifest()
    if state and state.get("model_version") == MODEL_VERSION:
        candidate = Path(state.get("model_path", ""))
        if is_model_complete(candidate):
            return candidate

    default = sense_voice_model_dir()
    if is_model_complete(default):
        return default

    return None
