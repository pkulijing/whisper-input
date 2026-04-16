# i18n 国际化实现计划

## Context

whisper-input 的所有用户界面文案（设置页、托盘菜单、命令行输出、macOS 权限弹窗、argparse 帮助文本）目前全部硬编码为中文。目标是支持中文 (zh)、英文 (en)、法语 (fr) 三种语言，同时创建双语 README。

对应 BACKLOG.md 中「国际化（i18n）」条目，在 `i18n` worktree（分支 `i18n`）上开发。文档轮次编号：`18-国际化i18n`。

---

## 架构决策

### 1. 统一的 JSON locale 文件
- 路径：`src/whisper_input/assets/locales/{zh,en,fr}.json`
- 扁平 key（dot 分隔前缀），如 `"settings.title"`, `"tray.quit"`, `"main.recording_start"`
- Python 后端和前端 JS **共享同一组 JSON 文件**

### 2. Python 侧 `i18n.py` 模块
- 提供 `load_locales()`, `set_language(lang)`, `get_language()`, `t(key, **kwargs)`, `get_all_locales()`
- Fallback 链：当前语言 → zh → key 本身
- 在 `main()` 中 config 加载后立即初始化

### 3. 设置页前端 i18n（方案 A）
- 所有 locale 数据通过 `string.Template` 嵌入 HTML（`$locale_data`），零额外请求
- HTML 元素加 `data-i18n="key"` 属性
- ~50 行 JS `applyLocale(lang)` 遍历替换 textContent
- 语言切换即时生效，不刷新页面
- 切换后通过现有 `POST /api/config` 保存 `ui.language`

### 4. 热键下拉标签
- Python 侧 `SUPPORTED_KEYS` 改为只传 key code 列表（不含中文标签）
- 前端 JS 从 locale 数据查找 `hotkey.{KEY_CODE}` 作为显示名

### 5. 语言热切换（无需重启）
- CLI 日志：`t()` 每次调用实时查表，天然支持
- 托盘菜单：pystray `MenuItem` text 参数传 callable（`lambda _: t("tray.settings")`），每次展开菜单自动求值
- 托盘 tooltip：`on_status_change` 回调中用 `t()`，状态变化时自动更新
- 设置页切换语言 → `POST /api/config` → `on_config_changed` 回调中调 `set_language()` 即时生效

### 6. Linux 托盘 tooltip
- pystray Linux 用 latin-1 编码 WM_NAME，中文会乱码
- 添加 `_safe_tooltip()` 函数：如果文本无法 latin-1 编码则 fallback 到英文

### 7. `string.Template` 安全
- 改用 `safe_substitute()` 替代 `substitute()`，避免 locale 中的 `$USER` 等被误解析

---

## 新建文件

### `src/whisper_input/i18n.py` (~60 行)
核心翻译模块：
```python
# load_locales(): 通过 importlib.resources 加载三个 JSON
# set_language(lang): 设置当前语言
# get_language() -> str
# t(key, **kwargs) -> str: 查 current -> zh -> key 本身，再 .format(**kwargs)
# get_all_locales() -> dict: 返回全部 locale 数据（供嵌入 HTML）
```

### `src/whisper_input/assets/locales/zh.json` (~90 keys)
Key 分组：
- `settings.*` (30): 设置页 HTML + JS 消息
- `hotkey.*` (11): 热键下拉标签
- `tray.*` (6): 托盘菜单和状态
- `main.*` (20): CLI 启动/运行时日志
- `cli.*` (5): argparse 帮助文本
- `perm.*` (10): macOS 权限弹窗
- `sensevoice.*` (3), `recorder.*` (2), `server.*` (1), `stt.*` (1)

### `src/whisper_input/assets/locales/en.json`
同 key，英文翻译。

### `src/whisper_input/assets/locales/fr.json`
同 key，法文翻译。

### `src/whisper_input/assets/locales/__init__.py`
空文件，确保 `importlib.resources` 能访问子目录。

### `README.zh-CN.md`
现 README.md 的中文内容移入此文件。

### `docs/18-国际化i18n/` 
- `PROMPT.md`, `PLAN.md`, `SUMMARY.md`

---

## 修改文件

### `src/whisper_input/config_manager.py`
- `DEFAULT_CONFIG` 添加 `"ui": {"language": "zh"}`
- `_generate_yaml()` 添加 `ui:` 段落（含注释）

### `src/whisper_input/__main__.py` (改动最大)
- `main()` 中 config 加载后调用 `load_locales()` + `set_language()`
- 替换 ~20 处 `print("...中文...")` 为 `print(f"[main] {t('main.xxx')}")`
- 替换 argparse description/help 为 `t()` 调用
- `"识别中..."` (line 116) → `t("main.processing")`

### `src/whisper_input/settings_server.py`
- `SUPPORTED_KEYS` → `SUPPORTED_KEY_CODES`（仅 key code 列表，去掉中文标签）
- `_get_settings_html()` 改用 `safe_substitute()`，新增注入：
  - `$locale_data` = `json.dumps(get_all_locales())`
  - `$current_language` = `get_language()`
  - `$hotkey_codes` = `json.dumps(SUPPORTED_KEY_CODES)`
- `"无效的 JSON"` → `t("server.invalid_json")`

### `src/whisper_input/assets/settings.html`
- HTML：所有可翻译元素加 `data-i18n="key"` 属性
- HTML：在「系统」card 中新增「界面语言」设置行（select 下拉）
- JS：新增 `const LOCALES = $locale_data;`, `let currentLang = '$current_language';`
- JS：新增 `applyLocale(lang)` 函数（~30 行）
- JS：`$hotkey_options` → `$hotkey_codes`，`populateSelect` 改为从 locale 查标签
- JS：所有中文字符串字面量改为 `LOCALES[currentLang]['key']` 查找
- JS：语言切换事件 → `applyLocale()` + `saveSetting('ui.language', lang)` + toast 提示

### `src/whisper_input/backends/tray_macos.py`
- `_STATUS_TIPS` 字典改为 `_status_tip(status)` 函数，调用 `t(f"tray.{status}")`
- 菜单 `"设置..."` → `t("tray.settings")`, `"退出"` → `t("tray.quit")`

### `src/whisper_input/backends/tray_linux.py`
- 同 tray_macos 的改法
- 新增 `_safe_tooltip(key)` 处理 latin-1 编码限制

### `src/whisper_input/backends/hotkey_macos.py`
- `check_macos_permissions()` 中所有 osascript 对话框文案 → `t()` 调用
- print 消息 → `t()` 调用

### `src/whisper_input/backends/hotkey_linux.py`
- print 消息和 ValueError 文案 → `t()` 调用

### `src/whisper_input/stt/sense_voice.py`
- 3 处 print → `t()` 调用

### `src/whisper_input/recorder.py`
- 2 处 print → `t()` 调用

### `src/whisper_input/stt/__init__.py`
- `"未知的 STT 引擎"` → `t()` 调用

### `README.md`
- 全部改为英文内容，顶部加 `[English](README.md) | [中文](README.zh-CN.md)` 切换链接

---

## 实施顺序

### Phase 1: 基础设施
1. 创建 `i18n.py`
2. 创建三个 locale JSON 文件
3. `config_manager.py` 添加 `ui.language`
4. 编写 `tests/test_i18n.py`

### Phase 2: Python 后端替换
5. `__main__.py` — 初始化 i18n + 替换所有字符串
6. `settings_server.py` — 重构 SUPPORTED_KEYS + 注入 locale 数据
7. `tray_macos.py` + `tray_linux.py` — 菜单和 tooltip
8. `hotkey_macos.py` + `hotkey_linux.py` — 日志和权限弹窗
9. `stt/sense_voice.py` + `recorder.py` + `stt/__init__.py` — 日志

### Phase 3: 前端 i18n
10. `settings.html` — data-i18n 标注 + 语言切换器 + JS i18n 运行时

### Phase 4: README
11. 英文 README.md + 中文 README.zh-CN.md

### Phase 5: 验证
12. 运行 `uv run pytest` 确保所有测试通过
13. `uv run whisper-input` 手动测试：
    - 启动日志语言正确
    - 设置页三语切换正常、即时生效
    - 热键下拉标签随语言切换
    - 托盘菜单文案正确
    - `--help` 输出语言正确
14. `uv run ruff check .` 确保代码风格

---

## 风险 & 注意

- **循环导入**：`i18n.py` 只依赖 `importlib.resources`，不导入其他 whisper_input 模块，安全
- **模块级 `t()` 调用**：`_STATUS_TIPS` 等模块级常量不能在 import 时调用 `t()`（此时 locale 未初始化），必须改为函数/惰性求值
- **`$` 转义**：locale JSON 中含 `$USER` 的值，`safe_substitute()` 会原样保留，不受影响
- **HTML 注入安全**：`json.dumps()` 会转义 `</script>` 中的 `/`，嵌入 `<script>` 块安全
- **法语翻译质量**：机器翻译，技术术语可能不地道，后续可找母语者 review
- **维护负担**：改 UI 文案需同步三个 JSON，PR 流程中需注意
