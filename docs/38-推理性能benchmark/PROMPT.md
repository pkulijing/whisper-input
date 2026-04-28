# Round 38 — 推理性能 benchmark(调研轮)

## 性质

**本轮是调研轮,不是开发轮**。产出"性能数据 + 测量框架",不动产品代码,
不解决性能问题。**性能优化的实际开发由独立的下一轮承接**(由本轮 baseline
数据驱动选方案)。

## 背景

Round 37 把 ONNX 模型从 `zengshuishui/Qwen3-ASR-onnx` (int8) 切到
`baicai1145/Qwen3-ASR-{0.6B,1.7B}-ONNX` (fp16),修了 1.7B 在 offline path
上确定性返空的 issue [#7](https://github.com/pkulijing/daobidao/issues/7)。
正确性问题解决了,但代价是 **推理速度肉眼可见地慢** ——
真机实测按住说话出字延迟、松开后等到崩溃感是常态;0.6B 还能将就,1.7B 在
Apple Silicon CPU 上几乎卡到不可用。

Round 37 SUMMARY 已明确写了"本分支不可上线",issue #7 也没真正关。

## 本轮目标

正式做性能优化之前,先建立**严谨的性能 baseline + 可重复的测量工具**:

1. 测一组 baseline 数据(当前 fp16 vs 老 int8,两种模型大小,多个音频长度),
   出一份带分析结论的报告
2. 把测量过程从一次性 spike 升级成可重复的 **`benchmarks/` 框架**(顶层
   目录,跟 `tests/` 同级),后续每来一个候选 backend(CoreML EP / CUDA EP /
   GGUF / 回滚 int8 等)都用同一套 fixture + harness 跑出可比数据,入
   `results/baselines/` 长期累积

### 测量约束

- 老 int8 已知 1.7B 在某些音频上确定性返空(round 37 spike 已记录翻车谱),
  挑选**它能稳定跑通的长度**(比如 zh.wav[:10.5s]、zh_long[8s~28s])对比,
  避免 FAIL case 的时间数据没意义
- 用项目当前两段测试音频(`tests/fixtures/zh.wav` 10.6s、
  `tests/fixtures/zh_long.wav` 122.86s),不引入新 fixture
- 测短(~5s)、中(~10s)、长(~25s)三档,体现长度对推理时间的影响
- 每个 case 报 encode / prefill / decode / total 四个时间分量 + RTF
  (real-time factor),方便后续优化轮定位瓶颈
- 同硬件(本地 Apple Silicon),同 onnxruntime CPU EP,排除外部变量

## 不在本轮范围

- **不改产品代码** —— `src/daobidao/` 一行不动
- **不做任何性能优化** —— 优化方案的选型 + 实施,由下一轮另开
- **不进 CI / 不进 pytest** —— benchmark 框架是"模型选型对比工具",不是
  "性能回归监控"。详细论证见 BACKLOG.md「不再追踪」段
- **不测 streaming 路径** —— 只测 offline `transcribe()`

## 交付物

- `benchmarks/` 顶层目录(README + CLI + harness + reporting + backend
  adapter 集合 + `results/baselines/` 入库的基线数据)
- 本轮 docs 目录下的:
  - `PLAN.md` —— 实现计划 + 关键测试 case 的"输入 → 期待输出"契约
  - `SUMMARY.md` —— 本轮总结
  - `baseline_results.md` —— 指向 `benchmarks/results/baselines/...` 的
    pointer + 人工分析结论
  - `benchmark.py` —— 第一次的 spike 脚本(历史快照,不再维护;真源是 `benchmarks/`)
