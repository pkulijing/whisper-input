# PLAN：1.7B 模型适配修复

## 目标拆解

P0（修复 1.7B 不可用）

1. `_stream.py` 流式 init 不再硬编码 `audio_features` 维度。
2. `Qwen3ONNXRunner` 暴露 `audio_feature_dim`，从 decoder 的 `audio_features` 输入 schema 推导，作为 1.7B / 0.6B 两份模型的唯一真实来源。

P0（删多余抽象 + 让单测能跑起来 + 1.7B 路径有覆盖）

3. **删 `src/daobidao/stt/qwen3/_downloader.py` 整个文件** + 删 `tests/test_qwen3_downloader.py` 整个文件。`Qwen3ASRSTT.load()` 直接调 `modelscope.snapshot_download(REPO_ID, allow_patterns=[...])`,把 `REPO_ID` / `VALID_VARIANTS` 两个常量挪到 `qwen3_asr.py` 顶部。corruption fallback (`force_network=True`) 也去掉。
4. `Qwen3ASRSTT` 加 `self.cache_root: Path | None = None` 公共属性,`load()` 里赋值。让 STT 自己成为 modelscope 路径的唯一持有者。
5. `tests/conftest.py` 重构成"通过 STT 拿路径"模式:
   - 唯一调下载的入口:`stt_0_6b` / `stt_1_7b` 两个 session-scoped fixture,各自构造 `Qwen3ASRSTT(variant).load()`
   - `qwen3_*_model_dir` / `qwen3_tokenizer_dir` 从 `stt.cache_root` 反推
   - 测 runner / tokenizer 的 fixture 直接复用 `stt._runner` / `stt._tokenizer`,不再单独构造(单 session 内 ONNX session 只加载一次)
   - 删 `_candidate_qwen3_roots` / `_find_qwen3_root` / `DAOBIDAO_QWEN3_DIR` env var(modelscope 自己的 `MODELSCOPE_CACHE` 已经覆盖同语义)
   - **不再有 `pytest.skip` 路径**,找不到就下,CI 第一次下完 cache 命中后续秒过
6. `.github/workflows/build.yml` 把 cache key 从 `modelscope-qwen3-asr-v1` bump 到 `v2`。
7. `tests/test_qwen3_runner.py` 改写:`runner` fixture 直接拿 `stt._runner`,parametrize 到两个 variant,所有写死的 `1024` 换成 `runner.audio_feature_dim`。`test_inspect_decoder_raises_when_no_cache_inputs` 改成完全无 ONNX 的纯 unit test(用 `__new__` bypass __init__),跟共享 runner 解耦避免污染。
8. `tests/test_qwen3_asr.py` 加 1.7B 的 end-to-end smoke;`stt` fixture 改成参数化指向 `stt_0_6b` / `stt_1_7b`;删除 `patched_downloader` fixture + 两个 corruption fallback test。
9. `tests/test_qwen3_stream_smoke.py` 加 1.7B 流式 smoke。
10. 加一条**专门防止本次回归**的 unit test:用 FakeRunner 把 `audio_feature_dim = 2048`,断言 `init_stream_state` 喂给 `decoder_step` 的 `audio_features.shape[-1] == 2048`。

P1（不阻塞修复，但顺手）

11. `_onnx_runner.py` docstring 里固定 1024 / 896 的描述改为"由 ONNX 决定"。
12. `tests/test_qwen3_stream.py` 里 FakeRunner 的 1024 也改为可配。

## 详细方案

### 步骤 1：`Qwen3ONNXRunner` 暴露 `audio_feature_dim`

`src/daobidao/stt/qwen3/_onnx_runner.py`：

- 在 `__init__` 里 `_inspect_decoder` 之后，从 `self.decoder.get_inputs()` 找 `name == "audio_features"` 的那个，取 `shape[-1]`。如果是符号 dim 而不是整数（理论不会，但防御性），fallback 抛 `RuntimeError`，因为这个值不能猜。
- 落到 `self.audio_feature_dim: int`。
- 在 docstring 里把"`(1, n_audio_tokens, 1024)`"这种硬数字换成"由 ONNX 决定，见 `audio_feature_dim`"。

为什么从 decoder 而不是 encoder 拿？两边都对，但 decoder 的输入 schema 是 `_stream.py` dummy 张量真正要匹配的对象，从这边拿语义最直接。

### 步骤 2：`_stream.py` 用 `runner.audio_feature_dim`

`src/daobidao/stt/qwen3/_stream.py:171`：

```python
dummy_af = np.zeros(
    (1, 1, runner.audio_feature_dim), dtype=np.float32
)
```

只此一处。其他流式逻辑不动（rollback / marker-anchored split / committed_text 等都是 variant-agnostic 的）。

### 步骤 3：删 `_downloader.py`,`load()` 内联 snapshot_download

`Qwen3ASRSTT.load()` 改写后的下载片段(替换 [qwen3_asr.py:94-122](../../src/daobidao/stt/qwen3/qwen3_asr.py#L94-L122)):

```python
from modelscope import snapshot_download

allow_patterns = [
    f"model_{self.variant}/conv_frontend.onnx",
    f"model_{self.variant}/encoder.int8.onnx",
    f"model_{self.variant}/decoder.int8.onnx",
    "tokenizer/*",
]
self.cache_root = Path(
    snapshot_download(REPO_ID, allow_patterns=allow_patterns)
)
self._runner = Qwen3ONNXRunner(
    self.cache_root / f"model_{self.variant}"
)
```

`REPO_ID = "zengshuishui/Qwen3-ASR-onnx"` 和 `VALID_VARIANTS = ("0.6B", "1.7B")` 从 `_downloader.py` 搬到 `qwen3_asr.py` 顶部。

文件级删除:
- `src/daobidao/stt/qwen3/_downloader.py` —— 整个文件
- `tests/test_qwen3_downloader.py` —— 整个文件

被这次去掉的能力:
- `local_files_only=True` fast path —— 实测命中 cache 时 `snapshot_download` 自身的 cache check 也是毫秒级
- `force_network=True` corruption fallback —— 罕见场景,让用户重启自愈

### 步骤 4：`Qwen3ASRSTT` 暴露 `cache_root`

[qwen3_asr.py:75-82](../../src/daobidao/stt/qwen3/qwen3_asr.py#L75-L82) `__init__` 加一行:

```python
def __init__(self, variant: str = "0.6B"):
    if variant not in VALID_VARIANTS:
        raise ValueError(...)
    self.variant: Variant = variant
    self.cache_root: Path | None = None  # 新增
    self._runner: Qwen3ONNXRunner | None = None
    self._tokenizer: Qwen3Tokenizer | None = None
```

`load()` 内对 `self.cache_root` 赋值(见步骤 3)。

`cache_root` 是公共属性,产品代码也可用:settings_server 想给 UI 展示"模型存在哪里"、调试时想知道 modelscope 缓存位置,都从这取——不是为测试加的。

### 步骤 5：`conftest.py` 通过 STT 拿路径

唯一调 snapshot_download 的入口落在 `Qwen3ASRSTT.load()`,conftest 不再自己 call snapshot_download,直接构造 STT:

```python
@pytest.fixture(scope="session")
def stt_0_6b() -> Qwen3ASRSTT:
    """加载 0.6B,session 内复用。触发首次下载 + warmup。"""
    s = Qwen3ASRSTT(variant="0.6B")
    s.load()
    return s


@pytest.fixture(scope="session")
def stt_1_7b() -> Qwen3ASRSTT:
    """加载 1.7B,session 内复用。"""
    s = Qwen3ASRSTT(variant="1.7B")
    s.load()
    return s


@pytest.fixture(scope="session")
def qwen3_0_6b_model_dir(stt_0_6b: Qwen3ASRSTT) -> Path:
    return stt_0_6b.cache_root / "model_0.6B"


@pytest.fixture(scope="session")
def qwen3_1_7b_model_dir(stt_1_7b: Qwen3ASRSTT) -> Path:
    return stt_1_7b.cache_root / "model_1.7B"


@pytest.fixture(scope="session")
def qwen3_tokenizer_dir(stt_0_6b: Qwen3ASRSTT) -> Path:
    return stt_0_6b.cache_root / "tokenizer"
```

去掉:`_candidate_qwen3_roots` / `_find_qwen3_root` / `qwen3_cache_root` fixture / `DAOBIDAO_QWEN3_DIR` env var。

`MODELSCOPE_CACHE` 不需要 conftest 显式认 —— `snapshot_download` 库内部就读这个 env var。


### 步骤 6：`.github/workflows/build.yml` cache key bump 到 v2

唯一改动是 cache key:

```yaml
- name: Cache Qwen3-ASR model
  uses: actions/cache@v5
  with:
    path: ~/.cache/modelscope/hub
    key: modelscope-qwen3-asr-v2     # was v1
    restore-keys: modelscope-qwen3-asr-
```

理由:cache 内容现在含 0.6B + 1.7B 两份(~3.5 GB),v1 那个 cache 只有 0.6B,语义不同应该 bump key。`restore-keys` 不变(仍可从旧 cache 部分恢复 0.6B,1.7B 那部分由 conftest fixture 触发的 `snapshot_download` 现下补齐)。

GitHub Actions 单 cache 限 10 GB,repo 总 cache 也 10 GB,3.5 GB 完全够。第一次 CI run 会下两份共 ~3.5 GB(中国到 modelscope 几分钟,GH runner 到 modelscope 估计 5-10 分钟,跑一次接受),之后命中 cache 秒过。

不再有 skip 路径。

### 步骤 7：`tests/test_qwen3_runner.py` 参数化 + 复用 STT 内部 runner

`runner` fixture 直接拿 `stt._runner` —— session 内只加载一次 ONNX,而不是为 runner test 单独再构造一个:

```python
@pytest.fixture(
    scope="module",
    params=["0.6B", "1.7B"],
    ids=["0.6B", "1.7B"],
)
def runner(request, stt_0_6b, stt_1_7b) -> Qwen3ONNXRunner:
    return {"0.6B": stt_0_6b, "1.7B": stt_1_7b}[request.param]._runner
```

把所有 `1024` 换成 `runner.audio_feature_dim`。`test_decoder_kv_dims_match_spike` 保留断言 `kv_heads == 8` / `head_dim == 128`(两 variant 一致,已验证)。

`test_inspect_decoder_raises_when_no_cache_inputs` 改写:不再 monkeypatch 共享 runner.decoder(会污染 session 内其他 case),直接用 `__new__` 跳 `__init__`,造一个无 ONNX session 的 runner stub:

```python
def test_inspect_decoder_raises_when_no_cache_inputs():
    runner = Qwen3ONNXRunner.__new__(Qwen3ONNXRunner)
    fake_decoder = MagicMock()
    fake_decoder.get_inputs.return_value = []
    runner.decoder = fake_decoder
    with pytest.raises(RuntimeError, match="cache_key_"):
        runner._inspect_decoder()
```

### 步骤 8：`tests/test_qwen3_asr.py` 加 1.7B end-to-end

`patched_downloader` fixture 删除。`stt` fixture 改成 parametrized 间接指向 `stt_0_6b` / `stt_1_7b`,继续复用 conftest 那两个 session-scoped 实例:

```python
@pytest.fixture(
    scope="module",
    params=["0.6B", "1.7B"],
    ids=["0.6B", "1.7B"],
)
def stt(request, stt_0_6b, stt_1_7b) -> Qwen3ASRSTT:
    return {"0.6B": stt_0_6b, "1.7B": stt_1_7b}[request.param]
```

`test_transcribe_zh_wav` 的两条断言（"先帝" / "益州"）对两份模型都应该成立；exact-string 匹配那条只对 0.6B 保留（不同 variant 的精确输出可能差一两个字）。

`test_load_falls_back_when_runner_construction_fails` / `test_load_second_runner_failure_propagates` 删除 —— corruption fallback 路径已经从 `load()` 里去掉。

### 步骤 9：`tests/test_qwen3_stream_smoke.py` 1.7B smoke

按现有 0.6B 的 smoke 测试 parametrize variant；逻辑不变，只是 `Qwen3ASRSTT(variant=...)` 走两轮(也复用 `stt_0_6b` / `stt_1_7b`)。

### 步骤 10：回归保护单测

新增 `tests/test_qwen3_stream.py::test_init_stream_state_uses_runner_audio_feature_dim`：

```python
def test_init_stream_state_passes_correct_audio_feature_dim():
    """Regression: dummy_af must match runner.audio_feature_dim, not hardcoded 1024.

    Reproduces the 1.7B init failure where encoder hidden = 2048 but
    _stream.py used a (1, 1, 1024) dummy tensor.
    """
    runner = FakeRunner(preset_tokens=[42], audio_feature_dim=2048)
    tokenizer = FakeTokenizer(...)
    init_stream_state(runner, tokenizer)
    # Assert the prefill call's audio_features last dim was 2048
    first_call = runner.decoder_calls[0]
    assert first_call["af_dim"] == 2048
```

为此 `FakeRunner` 要：
- 接受 `audio_feature_dim` 构造参数（默认 1024 保持现有用例不变）
- `decoder_calls` 记录 `audio_features.shape[-1]`
- `encode_audio` 输出形状 `(1, n, audio_feature_dim)`

### 步骤 11 / 12：清理

- `_onnx_runner.py` 顶部 docstring 把 `(1, n_audio_tokens, 896)` / `(1, n_audio_tokens, 1024)` 改成"维度由 ONNX 决定"。
- `test_qwen3_stream.py::FakeRunner` 的 1024 默认不动，但所有内部生成 audio_features 的地方用 `self.audio_feature_dim`，让步骤 7 的新 case 能注入 2048。

## 验证清单

执行完代码后必须依次跑：

1. `uv run pytest --no-cov -rs -q` —— 本机两份 cache 都已就绪，skip 数应该 38 → 0；不应再有 qwen3 相关的 skip。
2. `uv run pytest tests/test_qwen3_runner.py tests/test_qwen3_asr.py tests/test_qwen3_stream_smoke.py -v` —— 0.6B + 1.7B 两份参数化 case 都过。
3. `uv run pytest tests/test_qwen3_stream.py::test_init_stream_state_passes_correct_audio_feature_dim -v` —— 回归保护用例过。
4. `uv run ruff check .` —— 无新增 lint 错误。
5. **手测**：`uv run daobidao`，启动后从设置页把模型切到 1.7B，等切换完成后按住热键说一段话，松手验证文字正常粘贴出来；再切回 0.6B 验证回退正常。

## 风险与回滚

- **风险 1**：CI cache miss 时第一次要下 ~3.5 GB（0.6B + 1.7B），workflow 会变慢一次。`actions/cache` 命中后续都秒过；cache key bump 到 v2 是有意触发一次冷启动。如果 GH runner 到 modelscope 网络太慢导致超时,可以临时把 1.7B 那条 fixture 加 `pytest.importorskip` 之类的逃生口,但目前不预先做。
- **风险 2**：1.7B 的 zh.wav transcript 可能跟 0.6B 不同——已经规划：只断言关键词出现，不做 exact match。
- **风险 3**：`Qwen3ONNXRunner` 加 `audio_feature_dim` 字段如果 `audio_features` 输入的 `shape[-1]` 在某些 ONNX 导出下是符号 dim 而不是整数，会 raise。已 inspect 两份现有 ONNX 都是整数（896→1024 / 1024→2048），但 fallback 抛 `RuntimeError` + 明确错误信息保证不静默走错。
- **回滚**：所有改动都局限在 `_onnx_runner.py` / `_stream.py` / `tests/` / `.github/workflows/build.yml`，没有动模型协议、没有动配置 schema、没有动外部接口。git revert 单 commit 即可还原。

## 不在本轮范围

- 优化 `_stream.py` 算法（rollback 数、prefix cache 逻辑等）。
- 让 1.7B 离线 `transcribe()` 路径也加单测（已在路径里且不出 bug，可放到下一轮）。
- 重构 conftest 的 fixture 命名（`qwen3_0_6b_model_dir` / `qwen3_1_7b_model_dir` 是当前的命名风格，改名是另一件事）。
