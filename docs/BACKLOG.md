# Daobidao — Backlog

未来开发项的**速览索引**。每条都对应一个 GitHub Issue,**详情、讨论、跨轮上下文都在 issue 里**。

**为什么这样组织**:GitHub Issues 是真源(permanent history + 通过 `Closes #N` 跟 commit/PR 永久关联,开发完归档进 closed 仍可检索)。这个文件是**当前还没开发的项**的扁平快照,方便一眼扫到全图、决定下一轮挑哪个。

## 工作流

- **新增想法** → `gh issue create` 走 [issue templates](../.github/ISSUE_TEMPLATE/),挂三轴 label(type / area / priority),建完顺手在本文件相应分组里加一行
- **开新轮** → 从下面挑一条 → 复制 issue 链接到 `docs/N-*/PROMPT.md` 顶部 → 开干
- **收尾一轮** → PR / 合并 commit message 里写 `Closes #<issue 号>` 自动关 issue → **从本文件删掉这一行**(不打勾,整行删,避免腐烂)
- **SUMMARY.md "后续 TODO"** 里的新线索 → 直接 `gh issue create` 占位,把 issue 链接补到 SUMMARY 那一段;不要再让任何想法只活在某轮 SUMMARY 里

## 三轴分类约定

- **type**(类型):`type:feat` 新功能 / `type:bug` bug 修复 / `type:refactor` 重构 / `type:perf` 性能 / `type:test` 测试基建 / `type:docs` 文档
- **area**(模块):`area:stt` STT 与模型 / `area:ui` 设置页 / overlay / 托盘 / `area:backend` 录音 / 输入设备 / 输入法 / `area:packaging` 安装升级 / `area:test` 测试套 / `area:devexp` CI 与工具链
- **priority**(优先级):`priority:P0` 必须做、不做有重大风险 / `priority:P1` 重大新功能,做了有很大提升 / `priority:P2` 一般小功能小修复

排序粒度只到 P0/P1/P2 三档,同档内不再细排 —— 真要挑下一个时凭当下心情和痛点选。

## P0 — 必须做

- [#2 中英混杂 / 专业词汇的识别后处理](https://github.com/pkulijing/daobidao/issues/2) · `type:feat` `area:stt` —— 用户原话「中英文混合体验不是很好」,目标用户画像核心痛点
- [#7 1.7B 端到端测试在非 Linux x86 上不稳定](https://github.com/pkulijing/daobidao/issues/7) · `type:bug` `area:test` —— 测试套不能长期靠 `DAOBIDAO_SKIP_E2E_STT` 兜底,研发严肃性问题

## P1 — 重大新功能

(暂无)

## P2 — 一般小功能小修复

- [#3 跟随系统默认输入设备切换](https://github.com/pkulijing/daobidao/issues/3) · `type:bug` `area:backend`
- [#4 流式 preview 浮窗显示 pending](https://github.com/pkulijing/daobidao/issues/4) · `type:feat` `area:ui`
- [#5 模型管理加「删除」按钮](https://github.com/pkulijing/daobidao/issues/5) · `type:feat` `area:ui`
- [#6 流式 worker 落后于音频时的 backpressure 提示](https://github.com/pkulijing/daobidao/issues/6) · `type:feat` `area:ui`
- [#8 测试套增强(v2)](https://github.com/pkulijing/daobidao/issues/8) · `type:test` `area:test`
- [#9 并发模型迁移到 asyncio](https://github.com/pkulijing/daobidao/issues/9) · `type:refactor` `area:backend`
- [#10 ORT optimized_model 持久化](https://github.com/pkulijing/daobidao/issues/10) · `type:perf` `area:stt`
- [#11 1.7B 模型启用 GPU 推理后端(CUDA / CoreML)](https://github.com/pkulijing/daobidao/issues/11) · `type:perf` `area:stt`
- [#15 识别结果中英混合时英文后第一个中文字偶发乱码](https://github.com/pkulijing/daobidao/issues/15) · `type:bug` `area:stt` —— 偶发，目前无必现条件，触发面窄

## 已完成 / 不再追踪

历史已完成项**不在本文件追踪**,直接看 [closed issues with label `priority:*`](https://github.com/pkulijing/daobidao/issues?q=is%3Aissue+is%3Aclosed+label%3Apriority%3AP0%2Cpriority%3AP1%2Cpriority%3AP2)。

下面只列**刻意决定不做**的条目(避免未来自己或后续 agent 翻老 SUMMARY 发现"为什么这条没做",误以为是遗漏):

- **首次模型下载进度 UI**(14 轮 SUMMARY 局限性 #3)—— 实测下载速度已经够快(ModelScope 国内 CDN 秒级),用户痛点不明显,不值得做
- **Linux 实机验证**(14 轮 SUMMARY 局限性 #4)—— 已在干净 Ubuntu 上手动验证通过
- **跨平台 Pythonic overlay 统一代码**(16 轮遗留)—— 视觉已在 16 轮对齐(微信输入法风格深蓝药丸),双份原生实现(GTK3+Cairo / AppKit)维持现状。Tkinter 与 pystray 主线程冲突、子进程方案引入退出清理复杂度,真要统一得换 Tauri 这类方案全面接管 UI 层,不是 overlay 一个模块的事,当前版本满意,不再追
- **录音时实时检测麦克风离线 - macOS 替代 query_devices**(32 轮遗留 A)—— 32 轮 macOS 仍走 `sd.query_devices`,MacBook(内置麦永远在)主流场景可靠;Mac mini / Studio / Pro 等无内置麦的桌面机用户拔 USB 麦后 CoreAudio 会留 `CADefaultDeviceAggregate-xxxx-x` 占位设备 → probe 通过 → 录到 0 字节 → 幻觉 token。触发面太窄(无内置麦桌面 Mac + 拔外接麦 + 立刻按热键),等真有 Mac mini / Studio 用户报问题再做
- **录音时实时检测麦克风离线 - 录音中途断开监控**(32 轮遗留 B)—— 32 轮的 callback 连续 5 次 `input_overflow` 升级 device_lost 在 PipeWire 上完全失效(拔麦后 PipeWire 给的是干净静音流,无任何 status flag)。触发面只有"按住热键说话过程中精准拔麦"那一句,下一次按热键时 32 轮的 pactl probe 会兜底,影响面就一句话,不值得为它再加一条 daemon 线程
