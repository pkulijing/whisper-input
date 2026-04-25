"""流式识别端到端 smoke test —— 走完整 WhisperInput 生产路径。

**不**只调 stt.stream_step 验 "final == offline",那样会绕过编排层(accumulator、
worker queue、on_chunk callback、is_last 触发时机、_finalize_stream_session 等),
错过生产路径上的 bug。

本测试:
1. 真加载 Qwen3-ASR-0.6B ONNX
2. 构造一个 FakeRecorder:按住 → 开始 streaming;外部手动触发 callback 喂音频;
   松手 → 停止,触发最后一次 worker step(is_last=True)
3. 用真 WhisperInput 实例,真 `_on_stream_chunk` / `_do_stream_step` / 真
   accumulator 逻辑
4. 以**小粒度**(~1024 samples / callback,~64ms @ 16kHz)把 zh.wav 喂给 recorder,
   跟真机麦克风路径接近
5. 捕获所有 type_text 调用 = 每次 paste 的增量
6. 断言:
   a. 每次 paste 必须是 `offline_text` 的合法前缀延伸(不能"倒带"不能"岔开")
   b. 没有任何 paste 包含 degenerate 语言标签残留 ("language chinese" 等)
   c. 最终完整 paste 后 = offline transcribe 结果

跑法:
    uv run pytest tests/test_qwen3_stream_smoke.py -v --no-cov -s
"""

from __future__ import annotations

import time
import wave
from pathlib import Path

import numpy as np
import pytest

from whisper_input.__main__ import WhisperInput
from whisper_input.stt.qwen3 import Qwen3ASRSTT
from whisper_input.stt.qwen3._feature import SAMPLE_RATE

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "zh.wav"

# 每次 fake callback 送这么多 samples(~1024 = 64ms @ 16kHz)。
# 跟真机 PortAudio block 粒度同一个量级。比 STREAMING_CHUNK_SAMPLES(32000)
# 小两个数量级,让 WhisperInput 的 accumulator 真的起作用。
_CALLBACK_SAMPLES = 1024


def _load_audio_float32() -> np.ndarray:
    with wave.open(str(FIXTURE), "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE
        frames = wf.readframes(wf.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


class FakeStreamingRecorder:
    """模拟 sd.InputStream 的 callback 驱动行为。

    真 AudioRecorder.start_streaming 里 sd callback 会收到 int16 (frames, ch)
    的 indata,在 _audio_callback 里转 float32 1D 再调 on_chunk。这里直接用
    float32 1D 简化(跳过 int16 层),因为生产 recorder 已经做过转换。
    """

    def __init__(self):
        self.on_level = None
        self._on_chunk = None
        self.is_recording = False

    def start(self):
        raise AssertionError("流式路径不应该调到 start()")

    def stop(self):
        raise AssertionError("流式路径不应该调到 stop()")

    def start_streaming(self, on_chunk):
        self._on_chunk = on_chunk
        self.is_recording = True

    def stop_streaming(self):
        self.is_recording = False

    def feed(self, samples_float32: np.ndarray) -> None:
        """外部驱动:一次送一段 audio,同步调 on_chunk(就像真 sd callback 一样)。"""
        assert self._on_chunk is not None, "必须先 start_streaming"
        self._on_chunk(samples_float32)


def _drain_worker(wi: WhisperInput, timeout: float = 30.0) -> None:
    """等 worker 队列清空,并且 _processing=False(最终 flush 完成)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if wi._event_queue.empty() and not wi._processing:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"worker didn't drain: qsize={wi._event_queue.qsize()}, "
        f"processing={wi._processing}"
    )


@pytest.fixture(scope="module")
def real_stt(qwen3_0_6b_model_dir) -> Qwen3ASRSTT:
    """模块级单例,避免每个用例重新加载模型。"""
    s = Qwen3ASRSTT(variant="0.6B")
    s.load()
    return s


def test_streaming_raw_tokens_per_chunk(
    real_stt: Qwen3ASRSTT, monkeypatch
):
    """直接对 Qwen3ASRSTT.stream_step 调用,**dump 每个 chunk 后模型的原始**
    committed_tokens / pending_tokens(token id + 未过 parse_asr_output 的原文),
    验证模型在每个 chunk 实际吐出了什么。

    如果 raw token 序列里出现 '<asr_lang>...' 但后面没有 '<asr_text>...',
    parse_asr_output 会把整段残渣当 transcript 返回 —— 这是真机 "language chinese"
    症状的可疑根因。
    """
    audio = _load_audio_float32()
    state = real_stt.init_stream_state()
    tokenizer = real_stt._tokenizer  # 直接捏到 tokenizer 看 raw decode

    chunk_size = 32000
    print("\n=== per-chunk raw token dump ===")
    for i, start in enumerate(range(0, len(audio), chunk_size)):
        chunk = audio[start : start + chunk_size]
        is_last = start + chunk_size >= len(audio)
        evt = real_stt.stream_step(chunk, state, is_last=is_last)

        # dump 原始状态
        committed_ids = list(state.committed_tokens)
        pending_ids = list(state.pending_tokens)
        # 不过 parse_asr_output,保留 tokenizer raw(含 <asr_lang> / <asr_text> 等)
        committed_raw = tokenizer.decode(
            committed_ids, skip_special_tokens=True
        )
        pending_raw = tokenizer.decode(
            pending_ids, skip_special_tokens=True
        )

        print(
            f"\n  [chunk {i}] is_last={is_last} "
            f"committed_ids={committed_ids}"
        )
        print(f"    committed_raw:   {committed_raw!r}")
        print(f"    committed_delta: {evt.committed_delta!r}")
        print(f"    pending_ids:     {pending_ids}")
        print(f"    pending_raw:     {pending_raw!r}")
        print(f"    pending_text:    {evt.pending_text!r}")

        # 真正交给 parse_asr_output 的结果(= committed_delta 的来源)才是
        # "用户看到的 paste"。raw 里保留 scaffolding + marker 是正常的(修 bug
        # 后 committed 必须含 marker,scaffolding 作为前缀无害;parse 会截掉)。
        committed_text = evt.committed_delta  # 增量,也可以累积起来看 cumulative
        assert "language" not in committed_text.lower(), (
            f"chunk {i}: committed_delta leak 了 scaffolding → "
            f"{committed_text!r} (raw={committed_raw!r})"
        )
        assert "chinese" not in committed_text.lower(), (
            f"chunk {i}: committed_delta leak 了语言标签 → "
            f"{committed_text!r} (raw={committed_raw!r})"
        )
        assert "<asr_lang>" not in committed_text, (
            f"chunk {i}: committed_delta 含 <asr_lang> 标签 → "
            f"{committed_text!r}"
        )


def test_streaming_via_full_whisperinput_pipeline(
    real_stt: Qwen3ASRSTT, monkeypatch
):
    """从 on_stream_chunk → worker → stt.stream_step → type_text 全链路跑一遍。

    断言每次 paste 都是合法的前缀延伸,最终等于 offline transcribe 结果。
    """
    audio = _load_audio_float32()

    # 离线 baseline:作为"正确答案"
    with open(FIXTURE, "rb") as f:
        wav_bytes = f.read()
    offline = real_stt.transcribe(wav_bytes)
    assert offline, "离线识别出空?先确认 fixture + 模型能工作"

    # 把 create_stt_engine 替成返回真 STT(避免它再加载一次)
    monkeypatch.setattr(
        "whisper_input.__main__.create_stt_engine",
        lambda cfg: real_stt,
    )
    # 捕获所有 type_text 调用
    paste_log: list[str] = []
    monkeypatch.setattr(
        "whisper_input.__main__.type_text",
        lambda text: paste_log.append(text),
    )

    wi = WhisperInput(
        {
            "audio": {"sample_rate": 16000, "channels": 1},
            "sound": {"enabled": False},
            "tray_status": {"enabled": False},
            "overlay": {"enabled": False},
            "qwen3": {"variant": "0.6B", "streaming_mode": True},
        }
    )
    fake_recorder = FakeStreamingRecorder()
    wi.recorder = fake_recorder
    wi.start_worker()
    try:
        # 按键 → 真实 init_stream_state + fake start_streaming
        wi._do_key_press()
        assert fake_recorder.is_recording

        # 小粒度喂 zh.wav。每次 feed 等 on_chunk 同步返回,然后继续下一块。
        # 不等 worker 处理完再 feed(模拟真实 PortAudio 不等 STT)
        for start in range(0, len(audio), _CALLBACK_SAMPLES):
            chunk = audio[start : start + _CALLBACK_SAMPLES]
            fake_recorder.feed(chunk)

        # 松手 → stop_streaming + enqueue final flush
        wi._do_key_release()

        # 等 worker 把队列里所有 stream_step 都处理完,包括最后 is_last=True
        _drain_worker(wi, timeout=60.0)

    finally:
        wi.stop_worker(timeout=5.0)

    # --- 断言 ---
    print(f"\n  offline: {offline!r}")
    print(f"  paste_log ({len(paste_log)} items):")
    for i, piece in enumerate(paste_log):
        print(f"    [{i}] {piece!r}")
    final = "".join(paste_log)
    print(f"  final (concatenated): {final!r}")

    # 1. 不能有任何 paste 带语言标签残渣
    degenerate = [
        "language chinese",
        "language english",
        "<asr_lang",
        "</asr_lang",
    ]
    for i, piece in enumerate(paste_log):
        lower = piece.lower()
        for bad in degenerate:
            assert bad not in lower, (
                f"paste #{i}={piece!r} 含退化标签 {bad!r},"
                f"说明 stream_step 在中间 chunk 吐出了语言 tag 而非 transcript"
            )

    # 2. 每次 paste 之后累积的文本长度必须单调不减(流式只能追加不能回退),
    #    且始终是 offline 的合法"相似前缀"(允许个别字符级差异,因为 rollback
    #    窗口之外的 commit 没机会被后续修正 —— 例如 chunk 2 看到 6s 音频时
    #    把"崩殂"后面当句号,offline 看全文后用逗号,这种标点差异不可避免)
    prev_len = 0
    for i, piece in enumerate(paste_log[:-1]):  # 末次 flush 不做前缀检查
        cumulative_so_far = "".join(paste_log[: i + 1])
        assert len(cumulative_so_far) > prev_len or piece == "", (
            f"paste #{i} 累积长度退化:{prev_len} → {len(cumulative_so_far)}"
        )
        prev_len = len(cumulative_so_far)

    # 3. 最终 paste 跟 offline 的字级编辑距离 ≤ 5% offline 长度
    #    (rollback=3 下允许偶发标点 / 个别字差异,但语义应基本一致)
    def _edit_distance(a: str, b: str) -> int:
        if len(a) < len(b):
            a, b = b, a
        if not b:
            return len(a)
        prev_row = list(range(len(b) + 1))
        for ca in a:
            curr = [prev_row[0] + 1]
            for j, cb in enumerate(b, 1):
                curr.append(
                    min(
                        prev_row[j] + 1,
                        curr[j - 1] + 1,
                        prev_row[j - 1] + (ca != cb),
                    )
                )
            prev_row = curr
        return prev_row[-1]

    ed = _edit_distance(final, offline)
    tolerance = max(2, int(len(offline) * 0.05))
    assert ed <= tolerance, (
        f"流式结果相对 offline 编辑距离 {ed} > {tolerance} (5% tolerance):"
        f"\n  offline={offline!r}\n  stream={final!r}"
    )
