# 需求:修复 CI 必现失败

## 现状

CI 在 master / fix/ci 上**连续 3 次** build 都挂在同 4 个 case:

- `tests/test_qwen3_asr.py::test_transcribe_zh_wav[0.6B]`
- `tests/test_qwen3_asr.py::test_transcribe_zh_wav[1.7B]`
- `tests/test_qwen3_stream_smoke.py::test_streaming_via_full_whisperinput_pipeline[0.6B]`
- `tests/test_qwen3_stream_smoke.py::test_streaming_via_full_whisperinput_pipeline[1.7B]`

症状一律是 `Qwen3ASRSTT.transcribe(zh.wav)` 返空字符串。GitHub Actions cache **HIT** v2(模型文件已 warm),仍然返空。本地全过(本地 cache 跟 CI cache 是两份独立副本)。

30 轮当时只挂了 1 次,我把它当 flaky 写进了 BACKLOG「先观察」。事后看是错的——它不是 flaky,是从 5d4f448 开始**必现**,堵后续所有 PR。

## 期望

1. **CI 立刻绿** —— 现在堵着,后续任何改动合不进去
2. **未来再 flaky 时可观测** —— 现在 transcribe 返空只能盲猜,要从 log 直接看到 generated tokens / logits 统计 / 文件 size 等关键信号
3. **load 阶段就 fail-fast** —— 现在 `_warmup` 用全零静音空跑一次,**输出不检查**,真出 garbage(全 NaN / 全选 EOS)load 静默通过,等 transcribe 时才暴露成"返空"。要改成 warmup 喂真音频 + 检查输出非平凡,模型坏了直接抛在 load 里,不让 silent garbage 流到调用方

## 不做

- 不修 ORT 内部 quirk(那是上游问题,我们 workaround)
- 不上 transcribe 的 retry fallback(BACKLOG 里那条 — 治标且可能掩盖真 bug)
- 不重写流式路径
