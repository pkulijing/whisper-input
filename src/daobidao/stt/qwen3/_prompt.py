"""Build the chat-template prompt that Qwen3-ASR's decoder expects.

Derived from ``tokenizer/chat_template.json`` plus audio token expansion.
The ``<|audio_pad|>`` placeholder in the template appears once, but at
inference time it is expanded to ``audio_token_count`` copies so that the
decoder has one position per audio frame emitted by the encoder.

Final prompt shape:

    <|im_start|>system
    {system_prompt}<|im_end|>
    <|im_start|>user
    <|audio_start|>{<|audio_pad|> * N}<|audio_end|><|im_end|>
    <|im_start|>assistant

The assistant role is left open (``add_generation_prompt=true``) so the
model can emit ``<asr_text>{transcript}<|im_end|>``. The optional
``system_prompt`` slot is reserved for round 28 (hotword biasing); round 26
always passes an empty string.
"""

from __future__ import annotations

AUDIO_START = "<|audio_start|>"
AUDIO_END = "<|audio_end|>"
AUDIO_PAD = "<|audio_pad|>"
IM_START = "<|im_start|>"
IM_END = "<|im_end|>"


def build_prompt(
    audio_token_count: int,
    system_prompt: str = "",
) -> str:
    """Render the full chat-template prompt.

    Parameters
    ----------
    audio_token_count:
        Number of ``<|audio_pad|>`` positions, equal to the encoder's
        audio-token output length ``A`` (``audio_features.shape[1]``).
    system_prompt:
        Free-form system message. Default empty string reproduces the
        plain ASR template. Reserved for round 28's hotword biasing.
    """
    if audio_token_count < 1:
        raise ValueError(
            f"audio_token_count must be >= 1, got {audio_token_count}"
        )

    system = f"{IM_START}system\n{system_prompt}{IM_END}\n"
    audio_section = f"{AUDIO_START}{AUDIO_PAD * audio_token_count}{AUDIO_END}"
    user = f"{IM_START}user\n{audio_section}{IM_END}\n"
    assistant_open = f"{IM_START}assistant\n"

    return system + user + assistant_open
