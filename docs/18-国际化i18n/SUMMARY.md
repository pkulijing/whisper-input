# 第 18 轮开发总结 —— 国际化（i18n）

## 开发项背景

whisper-input 的全部用户界面文案（设置页、托盘菜单、CLI 输出、macOS 权限弹窗、argparse 帮助）此前硬编码为中文。作为开源工具，缺少英文/法文界面会挡住非中文用户。

## 实现方案

### 关键设计

1. **统一 JSON locale 文件**（`assets/locales/{zh,en,fr}.json`）：扁平 dot-key 结构，Python 后端和前端 JS 共享同一组文件，约 90 个翻译 key
2. **轻量 `i18n.py` 模块**（~60 行）：`load_locales()` / `set_language()` / `t(key, **kwargs)` / `get_all_locales()`，fallback 链为 当前语言 → zh → key 本身
3. **设置页前端 i18n**：所有 locale 数据通过 `string.Template.safe_substitute()` 嵌入 HTML（零额外请求），`data-i18n` 属性标记可翻译元素，~30 行 JS `applyLocale()` 函数遍历替换 textContent，语言切换即时生效无需刷新
4. **语言完全热切换无需重启**：
   - CLI 日志：`t()` 每次调用实时查表
   - 托盘菜单：pystray `MenuItem` text 传 `lambda _: t("key")`，每次展开菜单自动求值
   - 托盘 tooltip：`on_status_change` 回调中用 `t()`
   - 设置页切换语言 → `POST /api/config` → `on_config_changed` 回调中调 `set_language()`
5. **Linux 托盘 latin-1 安全**：`_safe_tooltip()` 函数检测文本能否 latin-1 编码，不能则 fallback 到英文
6. **设置页布局重组**：从三组（基本/高级/系统）简化为两组：
   - **基本设置**：提示音、录音浮窗、托盘图标状态、界面语言 — 改了即时生效
   - **高级设置**：快捷键、页面端口、开机自启动 — 前两项改了需重启
   - 语言切换器放在基本设置里作为 select 下拉，和其他设置项风格统一

### 开发内容概括

**新建文件（8 个）：**
- `src/whisper_input/i18n.py` — 核心 i18n 模块
- `src/whisper_input/assets/locales/__init__.py` — 包标记
- `src/whisper_input/assets/locales/zh.json` — 中文翻译（~90 keys）
- `src/whisper_input/assets/locales/en.json` — 英文翻译
- `src/whisper_input/assets/locales/fr.json` — 法文翻译
- `tests/test_i18n.py` — 13 个测试用例
- `README.zh-CN.md` — 中文 README
- `docs/18-国际化i18n/` — 开发文档（PROMPT / PLAN / SUMMARY）

**修改文件（12 个）：**
- `__main__.py` — i18n 初始化 + ~20 处字符串替换 + 语言热切换回调
- `settings_server.py` — SUPPORTED_KEYS → SUPPORTED_KEY_CODES + locale 数据注入 + safe_substitute
- `settings.html` — data-i18n 属性 + 语言下拉 + JS i18n 运行时 + 设置分组重组
- `config_manager.py` — DEFAULT_CONFIG 添加 `ui.language` + YAML 生成添加 `ui:` 段
- `tray_macos.py` / `tray_linux.py` — 菜单和 tooltip 改为 t() 调用，callable text 支持热切换
- `hotkey_macos.py` / `hotkey_linux.py` — 日志和 macOS 权限弹窗 i18n
- `stt/sense_voice.py` / `recorder.py` / `stt/__init__.py` — 日志 i18n
- `README.md` — 改为英文版，顶部加语言切换链接
- 3 个测试文件适配新的断言

### 额外产物

- `tests/test_i18n.py`：13 个测试用例覆盖 locale 加载、语言切换、fallback 链、format kwargs、三语翻译 key 一致性校验

## 局限性

1. **法语翻译质量**：由 AI 生成，技术术语可能不够地道，建议找法语母语者 review
2. **维护负担**：每次改 UI 文案需同步更新三个 JSON 文件（test_i18n 会校验 key 一致性，不会漏 key，但漏翻译不会报错）
3. **argparse 帮助文本语言**：在 config 加载前用默认语言（zh），首次运行 `--help` 显示中文

## 后续 TODO

- 新增语言只需添加 `assets/locales/xx.json` + HTML select 加一个 option + `SUPPORTED_LANGUAGES` 加一项，架构天然支持
- 考虑在 CI 中加入 locale JSON 一致性检查（当前仅在 test_i18n 中覆盖）
