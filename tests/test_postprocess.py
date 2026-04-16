"""测试 SenseVoice 输出后处理。

针对 src/whisper_input/stt/_postprocess.py 的 rich_transcription_postprocess。
这是 FunASR 仓库里 funasr_onnx/utils/postprocess_utils.py 的移植版本,
所以测试用例基于 FunASR 官方文档里的已知输入 / 输出对。
"""

from whisper_input.stt._postprocess import rich_transcription_postprocess


def test_neutral_chinese_strips_meta_tags():
    """中性中文 + Speech + withitn 标签全部被剥掉。"""
    src = (
        "<|zh|><|NEUTRAL|><|Speech|><|withitn|>"
        "欢迎大家来体验达摩院推出的语音识别模型。"
    )
    assert (
        rich_transcription_postprocess(src)
        == "欢迎大家来体验达摩院推出的语音识别模型。"
    )


def test_emotion_tags_stripped_no_emoji():
    """情感标签全部剥掉，不追加 emoji（输入法场景不需要）。"""
    src = "<|zh|><|HAPPY|><|Speech|>太开心了"
    out = rich_transcription_postprocess(src)
    assert out == "太开心了"


def test_applause_event_prepends_emoji():
    """Applause 事件渲染成 👏 加在文本头部。"""
    src = "<|zh|><|NEUTRAL|><|Applause|>掌声雷动"
    out = rich_transcription_postprocess(src)
    assert "👏" in out
    assert "掌声雷动" in out
    # Applause emoji 在文本前面
    assert out.index("👏") < out.index("掌")


def test_nospeech_event_unk_renders_question_mark():
    """<|nospeech|><|Event_UNK|> 整体被渲染成 ❓。"""
    assert rich_transcription_postprocess(
        "<|nospeech|><|Event_UNK|>"
    ) == "❓"


def test_english_lang_tag_stripped():
    """英文 + <|en|> 标签被剥掉,文本保留。"""
    src = "<|en|><|NEUTRAL|><|Speech|><|withitn|>hello world"
    assert rich_transcription_postprocess(src) == "hello world"


def test_empty_string_returns_empty():
    """空输入返回空字符串。"""
    assert rich_transcription_postprocess("") == ""


def test_woitn_tag_stripped():
    """<|woitn|>(without ITN)和 <|withitn|> 一样会被剥掉。"""
    src = "<|zh|><|NEUTRAL|><|Speech|><|woitn|>没有反向规范化的文本"
    assert (
        rich_transcription_postprocess(src) == "没有反向规范化的文本"
    )


def test_multiple_lang_segments_concatenated():
    """两段不同语种拼接,中间标签被剥,文本顺序保留。"""
    src = (
        "<|zh|><|NEUTRAL|><|Speech|>你好"
        "<|en|><|NEUTRAL|><|Speech|>world"
    )
    out = rich_transcription_postprocess(src)
    assert "你好" in out
    assert "world" in out
    assert out.index("你好") < out.index("world")
