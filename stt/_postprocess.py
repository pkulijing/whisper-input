"""SenseVoice 输出后处理 —— 从 funasr_onnx 移植。

Port 来源:
  https://github.com/modelscope/FunASR/blob/main/runtime/python/onnxruntime/funasr_onnx/utils/postprocess_utils.py

作者 / 版权:
  Copyright FunASR (https://github.com/alibaba-damo-academy/FunASR)
  MIT License (https://opensource.org/licenses/MIT)
  Speech Lab of DAMO Academy, Alibaba Group

SenseVoice 的原始输出含 meta 标签,例如:
  '<|zh|><|NEUTRAL|><|Speech|><|withitn|>欢迎大家来体验...'

rich_transcription_postprocess 把这些标签:
  - 语种标签(<|zh|>/<|en|>/...)直接剥掉
  - 情感标签(<|HAPPY|>/<|SAD|>/...)渲染成 emoji 或空串
  - 事件标签(<|Applause|>/<|Laughter|>/...)渲染成 emoji 或空串
  - withitn/woitn/Speech/Speech_Noise 等中性标签剥掉

只 port 了 SenseVoice 推理需要的函数和常量。Paraformer 用的
sentence_postprocess / sentence_postprocess_sentencepiece / abbr_dispose 没 port。
"""


emo_dict = {
    "<|HAPPY|>": "😊",
    "<|SAD|>": "😔",
    "<|ANGRY|>": "😡",
    "<|NEUTRAL|>": "",
    "<|FEARFUL|>": "😰",
    "<|DISGUSTED|>": "🤢",
    "<|SURPRISED|>": "😮",
}

event_dict = {
    "<|BGM|>": "🎼",
    "<|Speech|>": "",
    "<|Applause|>": "👏",
    "<|Laughter|>": "😀",
    "<|Cry|>": "😭",
    "<|Sneeze|>": "🤧",
    "<|Breath|>": "",
    "<|Cough|>": "🤧",
}

lang_dict = {
    "<|zh|>": "<|lang|>",
    "<|en|>": "<|lang|>",
    "<|yue|>": "<|lang|>",
    "<|ja|>": "<|lang|>",
    "<|ko|>": "<|lang|>",
    "<|nospeech|>": "<|lang|>",
}

emoji_dict = {
    "<|nospeech|><|Event_UNK|>": "❓",
    "<|zh|>": "",
    "<|en|>": "",
    "<|yue|>": "",
    "<|ja|>": "",
    "<|ko|>": "",
    "<|nospeech|>": "",
    "<|HAPPY|>": "😊",
    "<|SAD|>": "😔",
    "<|ANGRY|>": "😡",
    "<|NEUTRAL|>": "",
    "<|BGM|>": "🎼",
    "<|Speech|>": "",
    "<|Applause|>": "👏",
    "<|Laughter|>": "😀",
    "<|FEARFUL|>": "😰",
    "<|DISGUSTED|>": "🤢",
    "<|SURPRISED|>": "😮",
    "<|Cry|>": "😭",
    "<|EMO_UNKNOWN|>": "",
    "<|Sneeze|>": "🤧",
    "<|Breath|>": "",
    "<|Cough|>": "😷",
    "<|Sing|>": "",
    "<|Speech_Noise|>": "",
    "<|withitn|>": "",
    "<|woitn|>": "",
    "<|GBG|>": "",
    "<|Event_UNK|>": "",
}

emo_set = {"😊", "😔", "😡", "😰", "🤢", "😮"}
event_set = {
    "🎼",
    "👏",
    "😀",
    "😭",
    "🤧",
    "😷",
}


def format_str_v2(s: str) -> str:
    """处理单个 <|lang|> 段:挑出最频繁的情感 + 事件标签,其他全部剥掉。"""
    sptk_dict = {}
    for sptk in emoji_dict:
        sptk_dict[sptk] = s.count(sptk)
        s = s.replace(sptk, "")
    emo = "<|NEUTRAL|>"
    for e in emo_dict:
        if sptk_dict[e] > sptk_dict[emo]:
            emo = e
    for e in event_dict:
        if sptk_dict[e] > 0:
            s = event_dict[e] + s
    s = s + emo_dict[emo]

    for emoji in emo_set.union(event_set):
        s = s.replace(" " + emoji, emoji)
        s = s.replace(emoji + " ", emoji)
    return s.strip()


def rich_transcription_postprocess(s: str) -> str:
    """SenseVoice 输出的主后处理函数。

    输入: 含 meta 标签的原始解码文本,如
        '<|zh|><|NEUTRAL|><|Speech|><|withitn|>欢迎大家来体验达摩院推出的语音识别模型。'
    输出: 干净的用户可读文本
        '欢迎大家来体验达摩院推出的语音识别模型。'

    如果有情感或事件,会在结果里插入对应 emoji。
    """

    def get_emo(s):
        return s[-1] if s[-1] in emo_set else None

    def get_event(s):
        return s[0] if s[0] in event_set else None

    s = s.replace("<|nospeech|><|Event_UNK|>", "❓")
    for lang in lang_dict:
        s = s.replace(lang, "<|lang|>")
    s_list = [format_str_v2(s_i).strip(" ") for s_i in s.split("<|lang|>")]
    new_s = " " + s_list[0]
    cur_ent_event = get_event(new_s)
    for i in range(1, len(s_list)):
        if len(s_list[i]) == 0:
            continue
        if (
            get_event(s_list[i]) == cur_ent_event
            and get_event(s_list[i]) is not None
        ):
            s_list[i] = s_list[i][1:]
        cur_ent_event = get_event(s_list[i])
        if get_emo(s_list[i]) is not None and get_emo(s_list[i]) == get_emo(
            new_s
        ):
            new_s = new_s[:-1]
        new_s += s_list[i].strip().lstrip()
    new_s = new_s.replace("The.", " ")
    return new_s.strip()
