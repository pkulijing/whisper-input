# 启动时清理已有实例

## 现状

调试 / 日常使用时偶尔会遇到：

- 上次 daobidao 没退出干净留了僵尸进程
- 双击 .app 启动 / 命令行 `uv run daobidao` 误启了第二个
- 系统休眠唤醒后发现热键不响应,猜测是上一个会话的实例还活着

这时候**新启动的实例 `SettingsServer.start()` 直接抛 `OSError: [Errno 98] Address already in use`**(端口 51230 被老实例占着,见 [`settings_server.py:353`](../../src/daobidao/settings_server.py#L353))。用户看到的是程序静默崩溃,只能 `ps | grep daobidao` 手动 kill 老的再重启,体验很差。

## 期望

启动序列里加一个前置步骤,自动检测并清理老实例,**用户感知到的就是"双击启动 = 重启"**。具体语义:

- 能启动 → 可能没老实例(端口空闲)或者干掉了老实例
- 启动失败 → 端口被某个**不是 daobidao** 的进程占着,需要用户手动处理

附加:给"两个 shell 各起一个调试用"的开发者场景留一个 `--allow-multiple` flag 跳过整套检测。

## 不做

- **不做僵尸状态精细判断**:`/health` 端点能不能响应、热键监听是否还活着这些都不查。MVP 阶段"端口被占且能拿到 PID = 干掉它"。
- **不做 PID 文件 / fcntl 锁**:这些都是把决策权再推一层,本质问题不变。端口已经是天然独占资源,直接用。
- **不做 macOS-only / Linux-only 特殊路径**:协议层用 HTTP 而不是 lsof / psutil,跨平台一致。
- **不引入新依赖**:stdlib 的 `socket` + `urllib` + `os.kill` 够用。**不加 psutil**。
