# 远程桌面场景探索总结

## 开发项背景

使用 Mac 上的 ToDesk 远程控制 Linux 机器时，whisper-input 的热键完全无法识别，需要定位原因并评估可行性。

## 探索过程与发现

### 1. 热键问题定位

**现象：** 通过 ToDesk 远程按键时，whisper-input 无法检测到任何热键事件。

**根因：** 当前热键监听使用 evdev 直接读取物理键盘设备。ToDesk 通过 X11 的 XTest 扩展注入按键事件，完全绕过了 evdev 层。

**验证方法：**
- `xev -event keyboard`：X11 层面**能**看到 ToDesk 的按键事件（Mac Command → Linux `Control_R`，keycode 105）
- 列举所有 evdev 设备：**没有**发现 ToDesk 创建的虚拟键盘设备

**结论：** 热键问题可以通过将监听方式从 evdev 切换到 X11（Xlib）来解决。X11 的 keysym 同样能区分左右修饰键（`Control_R` vs `Control_L`），不会丢失功能。

### 2. 麦克风问题

**现象：** Linux 端 `sounddevice` 列出的录音设备全部为本机声卡，没有 ToDesk 虚拟麦克风设备。

**结论：** ToDesk 在被控端为 Linux 时不支持麦克风重定向，Mac 端的麦克风声音无法传到 Linux 端。

### 3. 最终结论

虽然热键问题有解，但麦克风无法重定向是硬伤，语音输入在远程桌面场景下**不可用**。改造暂时搁置。

## 局限性

- 仅测试了 ToDesk，未测试其他远程桌面方案
- ToDesk 后续版本可能增加 Linux 被控端的麦克风重定向支持

## 后续 TODO

- 如果切换到支持音频重定向的远程方案（如 RDP/VNC + PulseAudio 重定向），可重新推进 X11 热键监听改造
- X11 热键监听的改造方案：保留 evdev 用于本地，增加 Xlib 监听作为 fallback，覆盖远程桌面场景
