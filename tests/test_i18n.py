"""i18n 模块测试。"""

import json
from importlib.resources import files

from whisper_input.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    get_all_locales,
    get_language,
    load_locales,
    set_language,
    t,
)


class TestLoadLocales:
    """locale JSON 文件加载。"""

    def test_all_languages_loaded(self):
        load_locales()
        locales = get_all_locales()
        for lang in SUPPORTED_LANGUAGES:
            assert lang in locales
            assert len(locales[lang]) > 0

    def test_all_languages_have_same_keys(self):
        load_locales()
        locales = get_all_locales()
        zh_keys = set(locales["zh"].keys())
        en_keys = set(locales["en"].keys())
        fr_keys = set(locales["fr"].keys())
        assert zh_keys == en_keys, (
            f"zh 和 en 的 key 不一致: "
            f"zh 多 {zh_keys - en_keys}, en 多 {en_keys - zh_keys}"
        )
        assert zh_keys == fr_keys, (
            f"zh 和 fr 的 key 不一致: "
            f"zh 多 {zh_keys - fr_keys}, fr 多 {fr_keys - zh_keys}"
        )

    def test_locale_files_are_valid_json(self):
        locales_dir = files("whisper_input.assets.locales")
        for lang in SUPPORTED_LANGUAGES:
            data = (
                locales_dir.joinpath(f"{lang}.json")
                .read_text(encoding="utf-8")
            )
            parsed = json.loads(data)
            assert isinstance(parsed, dict)
            for key, value in parsed.items():
                assert isinstance(key, str)
                assert isinstance(value, str)


class TestSetLanguage:
    """语言切换。"""

    def test_set_supported_language(self):
        set_language("en")
        assert get_language() == "en"
        set_language("fr")
        assert get_language() == "fr"
        set_language("zh")
        assert get_language() == "zh"

    def test_set_unsupported_language_falls_back(self):
        set_language("ja")
        assert get_language() == DEFAULT_LANGUAGE


class TestTranslate:
    """t() 翻译函数。"""

    def setup_method(self):
        load_locales()
        set_language("zh")

    def test_basic_lookup_zh(self):
        assert t("tray.quit") == "退出"

    def test_basic_lookup_en(self):
        set_language("en")
        assert t("tray.quit") == "Quit"

    def test_basic_lookup_fr(self):
        set_language("fr")
        assert t("tray.quit") == "Quitter"

    def test_format_kwargs(self):
        set_language("en")
        result = t("main.engine", engine="sensevoice")
        assert result == "Engine: sensevoice"

    def test_format_kwargs_zh(self):
        result = t("main.engine", engine="sensevoice")
        assert result == "引擎: sensevoice"

    def test_fallback_to_zh(self):
        """当前语言缺失 key 时 fallback 到 zh。"""
        load_locales()
        locales = get_all_locales()
        # 模拟 en 缺失某个 key
        original = locales["en"].pop("tray.quit", None)
        try:
            set_language("en")
            assert t("tray.quit") == "退出"  # fallback 到 zh
        finally:
            if original is not None:
                locales["en"]["tray.quit"] = original

    def test_missing_key_returns_key(self):
        """所有语言都没有的 key，返回 key 本身。"""
        assert t("nonexistent.key") == "nonexistent.key"

    def test_settings_title_all_languages(self):
        """验证 settings.title 在三种语言中各不相同。"""
        translations = set()
        for lang in SUPPORTED_LANGUAGES:
            set_language(lang)
            translations.add(t("settings.title"))
        assert len(translations) == 3
