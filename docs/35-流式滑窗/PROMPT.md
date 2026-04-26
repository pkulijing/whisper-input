# 需求:流式识别长音频滑动窗口

## 背景

第 28 轮上线了流式识别(策略 E:prefix-cached re-prefill + rollback=10),但有个硬墙:`max_total_len=1200` 个 decoder token,典型在 35-80s 之间撞到(具体秒数跟语速强相关)。

撞墙后用户视角的现状是**无声损坏**:

1. `StreamingKVOverflowError` 在 `__main__.py:540-554` 被 catch,设 `_stream_overflow_hit = True`
2. 此后**剩余 chunk 全部丢弃**,后续音频再也不识别
3. 浮窗本应弹"已超上限"提示,但 `overlay_macos.py:228` 和 `overlay_linux.py:184` 的 `update(text)` **完全无视 text 参数**(只调 `_do_fade_out`),用户**没有任何视觉反馈**
4. 同样的 28s "接近上限"提示(`_notify_near_limit`)也被同款 bug 吞掉

实测在自然语速 + 间歇停顿场景下用户撞墙在 80s(BACKLOG.md 写的"33-38s"是低估)。一旦撞墙,用户继续说的话静默丢失,松手后才发现"我后面那段哪去了"。

## 本轮目标

### 核心目标

1. **真正的滑动窗口**:用户按住热键说话超过 60s / 90s / 180s 也能持续出字,无任何撞墙 / 提示
2. **滑窗触发用户无感**:已 commit 的文本早已粘贴出去(用户视觉焦点已离开),丢的是 prefill 内部的早期上下文,不影响用户能看到的文本
3. **删干净 28s 警告 + overflow 提示的所有死代码**:`__main__.py` ~50 行 + 三语 i18n 各 2 行
4. **保留 `StreamingKVOverflowError` 防御性兜底**:理论上滑窗后永不触发,真触发了改为优雅 `logger.warning + finalize_stream_session`(不丢 chunk、不调坏掉的 overlay)

### 非目标(本轮明确不做)

- **不做识别质量的量化对比**(单元测试 + 人肉测试就行)
- **不修 `overlay.update(text)` 参数被无视的 bug** —— 整条上游调用链都删了,bug 自动归零
- **不做长音频自动分段**(那是另一种思路,跟"按住说话"产品形态冲突)
- **不动 1.7B 路径** —— 同一份代码

## 硬约束

- 不引入新的运行时依赖(`numpy` 切片足够)
- `Qwen3StreamState.committed_tokens` **本体不能改**(跟 `committed_text` 强绑定,改了会让粘贴出去的字和 state 内部记录不一致)
- 滑窗必须以"折叠 `audio_features_pieces` 为单片 + 切片 `committed` 用于 prefill"的方式实现,不要写成"每次重新 encode 全部音频"那种 O(N²) 路径

## 用户视角的验收标准

按住热键念稿子 90s+,期望:

- 浮窗:全程正常声音条波动,无错误态
- 焦点应用:文本持续追加,无 ~80s 处的截断
- log:无 `stream_kv_overflow` warning,无 `streaming_near_limit` info
- 转录质量:语义连贯,滑窗触发后下一段衔接处可能略有不顺(可接受)
