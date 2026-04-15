"""Whisper Input 启动窗口 — 三阶段引导。

由 .app 内置的 python-build-standalone 直接执行（stdlib only）。
读取 WHISPER_INPUT_APP_DIR 环境变量定位 bundle 资源。

三阶段：
  Stage A — uv sync 装依赖到 user venv（~20MB,按 pyproject hash 决定是否跳过）
  Stage B — stt.downloader 从 ModelScope 下载 SenseVoice ONNX（~231MB,5 个文件,已缓存自然秒过）
  Stage C — 起 main.py，读日志直到 "[sensevoice] 模型加载完成" 后退出窗口

整个过程只有一个 tkinter 窗口，stage 间无缝切换。
Stage C 的 main.py 用 start_new_session 启动，setup_window 退出后继续运行。
"""

import contextlib
import hashlib
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

# .app/Contents/Resources/  —— 由 trampoline 注入
APP_DIR = Path(os.environ.get("WHISPER_INPUT_APP_DIR", ""))
APP_SRC = APP_DIR / "app"
UV_BIN = APP_DIR / "uv"
# 用嵌套 .app 里的 python 做 venv 基础解释器，让 TCC 权限归属给 Whisper Input。
# venv python 会 symlink 到这个路径，uv venv 创建时也会记录这个路径作为 base。
BUNDLED_PYTHON = (
    APP_DIR / "Whisper Input.app" / "Contents" / "MacOS" / "whisper-input"
)

USER_DATA_DIR = Path.home() / "Library/Application Support/Whisper Input"
USER_VENV = USER_DATA_DIR / ".venv"
USER_VENV_PYTHON = USER_VENV / "bin" / "python"
DEPS_SENTINEL = USER_DATA_DIR / ".deps_sha256"

LOG_FILE = Path.home() / "Library/Logs/WhisperInput.log"

MODEL_LOADED_MARKER = "[sensevoice] 模型加载完成"
MODEL_LOADING_MARKER = "[main] 预加载 SenseVoice 模型"


def log(msg: str) -> None:
    print(f"[setup] {msg}", flush=True)


def compute_deps_hash() -> str:
    h = hashlib.sha256()
    for name in ("pyproject.toml", "uv.lock"):
        p = APP_SRC / name
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()


def deps_up_to_date() -> bool:
    if not USER_VENV_PYTHON.exists():
        return False
    if not DEPS_SENTINEL.exists():
        return False
    if DEPS_SENTINEL.read_text().strip() != compute_deps_hash():
        return False
    # venv 健康检查：pyvenv.cfg 的 home 字段指向的基 python 可能因为
    # 本地开发污染、.app 被移动、多版本共存等原因失效，此时 venv 的
    # python 能启动但 stdlib 定位会崩。跑一次最小 import 验活。
    try:
        subprocess.run(
            [str(USER_VENV_PYTHON), "-c", "import importlib, sys"],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


class SetupWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Whisper Input")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.cancelled = False
        self.errored = False
        self.current_proc: subprocess.Popen | None = None
        # 主线程 UI 更新队列：worker 线程只 put 消息，不碰 tk
        self._ui_q: queue.Queue = queue.Queue()

        self._build_ui()
        self._center_window()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(800, lambda: self.root.attributes("-topmost", False))

    # ---------- UI ----------

    def _build_ui(self) -> None:
        frame = tk.Frame(self.root, padx=24, pady=20)
        frame.pack(fill="both", expand=True)

        self.title_var = tk.StringVar(value="Whisper Input 初始化")
        tk.Label(
            frame, textvariable=self.title_var,
            font=("Helvetica", 18, "bold"),
        ).pack(pady=(0, 8))

        self.desc_var = tk.StringVar(value="正在准备运行环境")
        tk.Label(
            frame, textvariable=self.desc_var,
            font=("Helvetica", 13), fg="#666",
        ).pack(pady=(0, 12))

        self.progress = ttk.Progressbar(
            frame, length=480, mode="indeterminate", maximum=100,
        )
        self.progress.pack(pady=(0, 4))
        self.progress.start(15)

        self.status_var = tk.StringVar(value="启动中...")
        tk.Label(
            frame, textvariable=self.status_var,
            font=("Helvetica", 11), fg="#999",
        ).pack(pady=(0, 8))

        log_frame = tk.Frame(frame)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_frame, height=12, width=68,
            font=("Menlo", 10),
            bg="#1e1e1e", fg="#cccccc",
            state="disabled", wrap="none",
            borderwidth=1, relief="solid",
        )
        sb = tk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _center_window(self) -> None:
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{x}+{y}")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_indeterminate(self) -> None:
        if str(self.progress.cget("mode")) != "indeterminate":
            self.progress.configure(mode="indeterminate")
        self.progress.start(15)

    def _set_determinate(self, maximum: int) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=maximum, value=0)

    def _set_progress(self, value: int) -> None:
        self.progress["value"] = value

    def _on_close(self) -> None:
        log("用户关闭窗口")
        self.cancelled = True
        if self.current_proc and self.current_proc.poll() is None:
            with contextlib.suppress(Exception):
                self.current_proc.terminate()
        self.root.destroy()

    # ---------- thread-safe UI dispatcher ----------
    # tkinter 的 Tk 对象不是真正线程安全的。worker 线程往队列里 put
    # (fn, args, kwargs)，主线程每 50ms 轮询一次并在本线程中调用。
    # 绝不要从 worker 线程直接调 self.root.after / tk 方法。

    def _ui(self, fn, *args, **kwargs) -> None:
        """从 worker 线程调度一次 UI 更新到主线程。"""
        self._ui_q.put((fn, args, kwargs))

    def _poll_ui_queue(self) -> None:
        """主线程轮询：把 worker 线程 put 的 UI 更新执行掉。"""
        try:
            while True:
                fn, args, kwargs = self._ui_q.get_nowait()
                try:
                    fn(*args, **kwargs)
                except Exception as e:
                    log(f"ui queue dispatch error: {e}")
        except queue.Empty:
            pass
        # 窗口还活着就继续轮询
        if not self.cancelled:
            with contextlib.suppress(tk.TclError):
                self.root.after(50, self._poll_ui_queue)

    # ---------- stage runner ----------

    def _run_stages(self) -> None:
        try:
            log("worker thread started")
            self._ui(self._append_log, "[worker] 已启动")
            self._ui(self._append_log, f"[worker] APP_DIR={APP_DIR}")
            self._ui(
                self._append_log, f"[worker] UV_BIN={UV_BIN}",
            )
            self._ui(
                self._append_log,
                f"[worker] BUNDLED_PYTHON={BUNDLED_PYTHON}",
            )

            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            self._ui(
                self._append_log, f"[worker] user data: {USER_DATA_DIR}",
            )

            up_to_date = deps_up_to_date()
            self._ui(
                self._append_log,
                f"[worker] deps_up_to_date={up_to_date}",
            )
            if up_to_date:
                log("依赖已是最新，跳过 stage A")
                self._ui(self._append_log, "✓ 依赖已就绪，跳过安装")
            else:
                self._ui(self._enter_stage_a)
                if not self._stage_a_run():
                    return

            self._ui(self._enter_stage_b)
            if not self._stage_b_run():
                return

            self._ui(self._enter_stage_c)
            if not self._stage_c_run():
                return

            self._ui(self._on_all_done)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            log(f"stage runner 异常: {e}\n{tb}")
            self._ui(self._on_error, f"内部错误: {e}")
            for line in tb.splitlines():
                self._ui(self._append_log, line)

    # ---------- stage A: uv sync ----------

    def _enter_stage_a(self) -> None:
        self.title_var.set("Whisper Input 初始化 (1/3)")
        self.desc_var.set("正在安装运行依赖，首次约 800MB")
        self.status_var.set("准备安装...")
        self._set_indeterminate()
        self._append_log("\n==> 阶段 1: 安装 Python 依赖 (uv sync)")

    def _stage_a_run(self) -> bool:
        env = os.environ.copy()
        env["UV_PROJECT_ENVIRONMENT"] = str(USER_VENV)
        env.pop("VIRTUAL_ENV", None)
        cmd = [
            str(UV_BIN), "sync",
            "--python", str(BUNDLED_PYTHON),
            "--no-progress", "--color=never",
        ]
        ok = self._run_pty(
            cmd, cwd=str(APP_SRC), env=env,
            line_handler=self._stage_a_line,
        )
        if ok:
            DEPS_SENTINEL.write_text(compute_deps_hash())
            self._ui(self._append_log, "✓ 依赖安装完成")
        return ok

    def _stage_a_line(self, line: str) -> None:
        if line.startswith("Resolved "):
            self._ui(self.status_var.set, "正在解析依赖图...")
        elif line.startswith("Downloading ") or line.startswith("Downloaded "):
            self._ui(self.status_var.set, "正在下载 wheel ...")
        elif line.startswith("Built ") or line.startswith("Building "):
            self._ui(self.status_var.set, "正在编译扩展...")
        elif line.startswith("Prepared "):
            self._ui(self.status_var.set, "正在准备安装...")
        elif line.startswith("Installed "):
            self._ui(self.status_var.set, "依赖安装完成")
        elif line.startswith("+ "):
            pkg = line[2:].split("==")[0].strip()
            self._ui(self.status_var.set, f"已安装 {pkg}")

    # ---------- stage B: 下载 SenseVoice ONNX 模型 ----------

    def _enter_stage_b(self) -> None:
        self.title_var.set("Whisper Input 初始化 (2/3)")
        self.desc_var.set("正在准备语音识别模型，首次约 231MB")
        self.status_var.set("检查模型缓存...")
        self._set_determinate(100)
        self._append_log("\n==> 阶段 2: 准备 SenseVoice 模型")

    def _stage_b_run(self) -> bool:
        # stt/downloader.py 是纯 stdlib 实现,可以直接在 setup_window 自己
        # 的进程里(bundled python-build-standalone)运行,不需要起 user venv
        # 子进程 —— 下载、SHA256 校验、tar.bz2 解压、manifest 落盘全程 stdlib。
        # 本地已命中就直接返回,零联网。
        sys.path.insert(0, str(APP_SRC))
        try:
            from stt.downloader import (
                ModelDownloadError,
                download_model,
            )
        except Exception as e:
            self._ui(
                self._append_log,
                f"[loader] 加载 stt.downloader 失败: {e}",
            )
            return False

        def log_cb(msg: str) -> None:
            self._ui(self._append_log, msg)

        def progress_cb(done: int, total: int) -> None:
            # 节流:只在百分比整数变化时更新 UI,避免 tk 事件队列被淹
            if total > 0:
                pct = int(done * 100 / total)
                if pct != getattr(self, "_stage_b_last_pct", -1):
                    self._stage_b_last_pct = pct  # type: ignore[attr-defined]
                    self._ui(self._set_progress, pct)
                    mb_done = done / 1024 / 1024
                    mb_total = total / 1024 / 1024
                    self._ui(
                        self.status_var.set,
                        f"下载中 {pct}% ({mb_done:.1f}/{mb_total:.1f} MB)",
                    )
            else:
                mb_done = done / 1024 / 1024
                self._ui(
                    self.status_var.set, f"下载中 {mb_done:.1f} MB",
                )

        try:
            model_dir = download_model(
                progress_cb=progress_cb, log_cb=log_cb,
            )
        except ModelDownloadError as e:
            self._ui(
                self._append_log, f"[loader] 模型下载失败:\n{e}",
            )
            return False
        except Exception as e:
            self._ui(
                self._append_log, f"[loader] 模型准备异常: {e}",
            )
            return False

        self._ui(self._set_progress, 100)
        self._ui(
            self._append_log, f"✓ 模型已就绪: {model_dir}",
        )
        return True

    # ---------- stage C: main.py + tail log ----------

    def _enter_stage_c(self) -> None:
        self.title_var.set("Whisper Input 初始化 (3/3)")
        self.desc_var.set("正在加载语音识别模型到内存")
        self.status_var.set("启动主程序...")
        self._set_indeterminate()
        self._append_log("\n==> 阶段 3: 加载模型并启动主程序")

    def _stage_c_run(self) -> bool:
        # 先记录当前日志文件大小，main.py 之后写的内容从这里开始 tail
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.touch(exist_ok=True)
        tail_start_pos = LOG_FILE.stat().st_size

        log_fd = os.open(
            str(LOG_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644,
        )
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        for var in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            env.pop(var, None)
        try:
            self.current_proc = subprocess.Popen(
                [str(USER_VENV_PYTHON), str(APP_SRC / "main.py")],
                cwd=str(APP_SRC),
                stdout=log_fd, stderr=log_fd,
                env=env,
                start_new_session=True,
            )
        finally:
            os.close(log_fd)

        log(f"main.py 已启动 (pid={self.current_proc.pid})")
        self._ui(
            self._append_log,
            f"main.py 已启动 (pid={self.current_proc.pid})",
        )

        return self._tail_log_for_marker(
            MODEL_LOADED_MARKER, timeout=180, start_pos=tail_start_pos,
        )

    def _tail_log_for_marker(
        self, marker: str, timeout: float, start_pos: int = 0,
    ) -> bool:
        start = time.monotonic()
        try:
            log_handle = open(LOG_FILE, "rb")  # noqa: SIM115
        except OSError as e:
            self._ui(self._on_error, f"无法读取日志: {e}")
            return False
        with log_handle as f:
            f.seek(start_pos)
            buf = b""
            while not self.cancelled:
                proc = self.current_proc
                if proc is None:
                    return False
                if proc.poll() is not None and not buf:
                    self._ui(
                        self._on_error,
                        "主程序在加载模型完成前退出，请查看日志",
                    )
                    return False
                if time.monotonic() - start > timeout:
                    self._ui(
                        self._on_error, "模型加载超时（180s）",
                    )
                    return False
                chunk = f.read(4096)
                if not chunk:
                    time.sleep(0.2)
                    continue
                buf += chunk
                while b"\n" in buf:
                    line_b, buf = buf.split(b"\n", 1)
                    line = line_b.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue
                    self._ui(self._append_log, line)
                    if MODEL_LOADING_MARKER in line:
                        self._ui(
                            self.status_var.set, "正在加载模型权重...",
                        )
                    if marker in line:
                        return True
        return False

    # ---------- subprocess + pty ----------

    def _run_pty(self, cmd, cwd, env, line_handler) -> bool:
        import pty
        import select

        log(f"运行: {' '.join(cmd)}")
        master, slave = pty.openpty()
        try:
            self.current_proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdout=slave, stderr=slave,
            )
        except Exception as e:
            os.close(master)
            os.close(slave)
            self._ui(self._on_error, f"启动失败: {e}")
            return False
        os.close(slave)

        ansi_re = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
        buf = ""
        try:
            while not self.cancelled:
                try:
                    r, _, _ = select.select([master], [], [], 0.2)
                except (OSError, ValueError):
                    break
                if r:
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    buf += data.decode("utf-8", errors="replace")
                    while True:
                        idx = -1
                        for sep in ("\r\n", "\n", "\r"):
                            i = buf.find(sep)
                            if i >= 0 and (idx < 0 or i < idx):
                                idx = i
                                sep_len = len(sep)
                        if idx < 0:
                            break
                        line = buf[:idx]
                        buf = buf[idx + sep_len:]
                        line = ansi_re.sub("", line).strip()
                        if not line:
                            continue
                        log(f"out: {line}")
                        self._ui(self._append_log, line)
                        try:
                            line_handler(line)
                        except Exception as e:
                            log(f"line_handler error: {e}")
                elif self.current_proc.poll() is not None:
                    # 把剩余字节读完
                    try:
                        while True:
                            data = os.read(master, 4096)
                            if not data:
                                break
                            buf += data.decode("utf-8", errors="replace")
                    except OSError:
                        pass
                    break
        finally:
            with contextlib.suppress(OSError):
                os.close(master)
            self.current_proc.wait()

        rc = self.current_proc.returncode
        if rc != 0 and not self.cancelled:
            self._ui(
                self._on_error, f"命令失败 (exit {rc})",
            )
            return False
        return True

    # ---------- terminal states ----------

    def _on_all_done(self) -> None:
        log("全部初始化完成")
        self.title_var.set("Whisper Input")
        self.desc_var.set("初始化完成，主程序已在托盘运行")
        self.status_var.set("即将关闭此窗口...")
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=100)
        self._append_log("\n✓ 全部完成，主程序进入托盘")
        self.root.after(1200, self.root.destroy)

    def _on_error(self, msg: str) -> None:
        log(f"错误: {msg}")
        self.errored = True
        self.title_var.set("Whisper Input — 出错")
        self.desc_var.set(msg)
        self.status_var.set(f"日志: {LOG_FILE}")
        with contextlib.suppress(Exception):
            self.progress.stop()
        self._append_log(f"\n✗ {msg}")

    def run(self) -> bool:
        # 先启动 UI 队列轮询（主线程），之后再起 worker 线程
        self.root.after(50, self._poll_ui_queue)

        def kick():
            threading.Thread(target=self._run_stages, daemon=True).start()
        self.root.after(500, kick)
        self.root.mainloop()
        return not (self.cancelled or self.errored)


def main() -> None:
    log("启动 setup_window")
    log(f"APP_DIR={APP_DIR}")
    log(f"USER_VENV={USER_VENV}")
    if not APP_DIR or not APP_SRC.exists():
        print(
            f"[setup] APP_SRC not found: {APP_SRC} "
            f"(WHISPER_INPUT_APP_DIR={APP_DIR})",
            file=sys.stderr,
        )
        sys.exit(1)
    win = SetupWindow()
    ok = win.run()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
