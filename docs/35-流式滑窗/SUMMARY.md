# 35 轮总结:流式识别长音频滑动窗口

## 开发项背景

正向开发 + 顺带修一个无声 bug。

28 轮上线流式识别(策略 E:prefix-cached re-prefill + rollback=10)后,有 `max_total_len=1200` 个 decoder token 的硬墙,典型 35-80s 之间撞到(BACKLOG 写的"33-38s"低估了实际,用户实测在自然语速 + 间歇停顿下 80s 才撞)。撞墙后的"无声损坏":

1. `StreamingKVOverflowError` 在 `__main__.py` 被 catch,设 `_stream_overflow_hit = True`
2. 后续 chunk **全部丢弃**,后续音频再也不识别
3. 浮窗本应弹"已超上限"提示,但 `overlay_macos.py:228` / `overlay_linux.py:184` 的 `update(text)` **完全无视 text 参数**(只调 `_do_fade_out`),用户**没有任何视觉反馈**
4. 28s "接近上限"提示也被同款 bug 吞掉

用户的视角:松手后才发现"我后面那段哪去了"。

## 实现方案

### 关键设计

真正的滑动窗口,只动两个东西(详细论证见 `PLAN.md`):

1. **audio 端**:每次 stream_step concat 完所有 `audio_features_pieces` 后,如果总长 > `MAX_AUDIO_TOKENS=700` 就截到最后 700 + 折叠 pieces 为单片(避免下次又 grow)
2. **committed 端**:`state.committed_tokens` **本体不动**(跟 `committed_text` 强绑定,改本体会让粘贴出去的字和 state 内部记录不一致 → 重复/漏字),只在拼 `mid_ids` 时用 slice `committed_for_prefill = state.committed_tokens[-MAX_COMMITTED_TOKENS=400:]`

为啥 marker 检测不受影响:`_stream.py:346-348` 扫的是 state 本体(没动),不是 prefill slice。

为啥 28s 警告 + overflow 文案的整套 UI 通路被删:滑窗实现正确就永远不撞墙,反馈代码可一并删。已 commit 的早期文本本来就是用户视线已离开的内容,丢弃无感。

阈值取舍:`max_total_len=1200` - chat_prefix(~30) - chat_suffix(~5) - `MAX_NEW_TOKENS_PER_CHUNK`(32) = 1133 给 audio + committed 分。700 + 400 = 1100 ≤ 1133,留 33 token headroom 防 off-by-one。

**实测验证**(`scripts/spike_qwen3_long_audio.py` + `tests/test_qwen3_stream_sliding_real.py` 跑 122s 中文朗读):

| 项 | 实测 | 设计预期 |
|---|---|---|
| Audio token 速率 | **13 token/s**(每 chunk 26 token) | 10-12.5 token/s |
| Committed token 速率 | **3.4 token/s**(122s 出 418 token) | 未量化 |
| Audio 滑窗首次触发 | **chunk 26 = 54s** | ~56-70s |
| Committed 滑窗首次触发 | **chunk 59 = 120s** | ~80-130s |
| 滑窗触发后输出连贯 | ✓(完整 transcript 含"太平天国"等关键词) | Risk #4 验证通过 |

起始阈值 700/400 实测合理,无需调整。

### 开发内容概括

**滑窗本体**:

- `src/daobidao/stt/qwen3/_stream.py`:加 `MAX_AUDIO_TOKENS=700` / `MAX_COMMITTED_TOKENS=400` 常量 + `stream_step` 在 audio_features concat 后插入双滑窗逻辑
- `src/daobidao/stt/base.py`:`StreamingKVOverflowError` docstring 更新(理论永不触发,改防御性兜底)

**死代码清理**:

- `src/daobidao/__main__.py`:删 ~50 行(28s near_limit 提示 / `_stream_overflow_hit` 状态字段 / `_notify_near_limit` 方法 / 各 reset 行 / `_STREAM_WARN_SAMPLES` 常量)
- overflow catch 改写为 `logger.warning("stream_kv_overflow_unexpected") + _finalize_stream_session()`,**不丢 chunk、不调坏掉的 overlay**
- `src/daobidao/assets/locales/{zh,en,fr}.json`:三语各删 2 行(`streaming_overflow` / `streaming_near_limit`)

**测试**:

- `tests/test_qwen3_stream_sliding.py`(新):4 个 FakeRunner 单元测试,验证 Python 切片逻辑(audio cap、committed slice、文本一致性、long N=200 chunks 不抛 overflow)
- `tests/test_qwen3_stream_sliding_real.py`(新):122s 真音频端到端测试,4 条断言:滑窗触发位置 / cap 落点 / state 本体未裁 / 转录关键词。跟现有 1.7B e2e 测试同款 `DAOBIDAO_SKIP_E2E_STT` 跳列表
- `tests/test_main_streaming.py`:改写 overflow 测试为"sliding 后理论不触发,真触发了应优雅 finalize 不再设半死状态" + 删 28s near_limit 测试

### 额外产物

- `tests/fixtures/zh_long.wav` + `zh_long.m4a`:122s 中文朗读 fixture(近代史短文,16kHz mono PCM 转自用户 macOS Voice Memos 录音)
- `tests/fixtures/README.md`:新增 zh_long 段
- `scripts/spike_qwen3_long_audio.py`:实测脚本,跑真模型量化 token 速率 + 验证阈值合理性。one-shot tooling,以后想重测阈值还能直接跑

## 局限性

1. **跨段语义连贯性只测了 122s**:更长 session(几小时连讲)、跨段引用("刚才提到的 X 再补充")等场景未验证。在常规"按住即说"场景影响极小,极端用例可能掉点
2. **`state.committed_tokens` 永久累加**:本轮只管"prefill slice 用最后 N 个",但 state 本体随时长线性增长。每秒 ~3.4 token,1 小时 ~12000 token。每 chunk 都 `tokenizer.decode` 全量算 `committed_text` → 几小时后 decode 比推理慢,触发恶性循环。本轮不管
3. **真音频端到端测试在 CI 跳过**:`test_qwen3_stream_sliding_real.py` 跟现有 1.7B e2e 测试同款,挂在 `DAOBIDAO_SKIP_E2E_STT` 跳列表里。0.6B 长 prompt 在 CI runner 上是否稳没验过,没敢赌
4. **`overlay.update(text)` 参数被无视的潜在 bug 没修**:120×34 浮窗太窄渲染不下文本是上游设计取舍。本轮删了所有 update(text) 调用,bug 自动归零(没人调就不显形),但浮窗状态切换的视觉单元仍然只有"颜色 / 形状",不是文字
5. **`_event_queue` 没有 backpressure**:用户实测发现 UP 主级语速会让 worker 落后于音频流入(stream_step ~500-800ms vs chunk 入队 2s),字越出越慢 + 松手后还要等很久。极端连讲几小时会被 OS OOM kill,日常不会

## 后续 TODO

已加 BACKLOG:

- **流式 worker 落后于音频时的 backpressure 提示**(局限性 #5):浮窗变色提示用户停顿,不丢 chunk
- **1.7B 端到端测试在非 Linux x86 上不稳定**(本轮新发现):作者 Mac ARM 上 1.7B 测试也确定性挂(33 轮假设的"本地一直稳"破裂),归因猜测 onnxruntime CPU EP micro-kernel 跨架构数值漂移

未加 BACKLOG,记录在这里供未来参考:

- **`committed_text` 增量 decode 优化**(局限性 #2 的根治):每 chunk 只 decode 新 token + append 到 cached text,而不是全量 decode。让长 session(>1小时)的 decode 时间不再线性增长。之所以没立刻加 BACKLOG 是几小时连讲的真实场景几乎不存在,优先级低
- **真音频测试在 CI 试跑**:摘掉 `test_qwen3_stream_sliding_real` 的 skip 装饰器,推 CI 看 0.6B 长 prompt 是否稳。挂了再加回 skip,无副作用,只是单次 build 红的成本

## 顺带修的独立 bug(单独 commit)

本轮人肉测试时发现 `uv run daobidao` 在作者 Mac 上起不来:`FileNotFoundError: daobidao-launcher`。诊断:

- `daobidao-launcher` 是 ObjC 二进制,`.gitignore`'d,**只在 CI 构建 wheel 时编译**
- `__main__.py:792` 的逻辑在 dev mode 也强制 `install_app_bundle()` → 找不到 launcher → 挂
- 触发条件:之前装过老版 bundle (CFBundleVersion 1.0.3),现在 dev pyproject 是 1.0.4 → `is_app_bundle_outdated()=True` → 触发 reinstall

修法:`__main__.py` 加 dev mode short-circuit,检测 launcher 不存在就跳过 bundle 安装 + relaunch,直接以 venv 进程启动。代价是 macOS TCC 权限会归因到 Python interpreter 而不是 "Daobidao",但这是 dev mode 已有取舍,不是回归。

跟 round 35 主轮次完全独立,**单独 commit**(per `/finish` 参数指示)。
