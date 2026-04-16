"""国际化（i18n）模块 - 提供多语言翻译支持。"""

import json
from importlib.resources import files

SUPPORTED_LANGUAGES = ("zh", "en", "fr")
DEFAULT_LANGUAGE = "zh"

_locales: dict[str, dict[str, str]] = {}
_current_lang: str = DEFAULT_LANGUAGE


def load_locales() -> None:
    """从 assets/locales/ 加载所有语言的翻译文件。"""
    locales_dir = files("whisper_input.assets.locales")
    for lang in SUPPORTED_LANGUAGES:
        try:
            data = (
                locales_dir.joinpath(f"{lang}.json")
                .read_text(encoding="utf-8")
            )
            _locales[lang] = json.loads(data)
        except (FileNotFoundError, json.JSONDecodeError):
            _locales[lang] = {}


def set_language(lang: str) -> None:
    """设置当前语言。不支持的语言回退到默认语言。"""
    global _current_lang
    _current_lang = lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def get_language() -> str:
    """返回当前语言代码。"""
    return _current_lang


def t(key: str, **kwargs) -> str:
    """翻译指定 key。

    查找顺序：当前语言 → 中文 → key 本身。
    支持 format 占位符：t("main.engine", engine="sensevoice")
    """
    text = (
        _locales.get(_current_lang, {}).get(key)
        or _locales.get("zh", {}).get(key)
        or key
    )
    if kwargs:
        text = text.format(**kwargs)
    return text


def get_all_locales() -> dict[str, dict[str, str]]:
    """返回全部语言翻译数据（供嵌入 HTML 模板）。"""
    if not _locales:
        load_locales()
    return _locales
