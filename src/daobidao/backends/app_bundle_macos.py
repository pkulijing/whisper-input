"""macOS .app bundle 管理 — 解决 TCC 权限归属问题。

通过生成一个包含原生 launcher 的 .app bundle，让 macOS TCC 系统
将权限归属于 "Daobidao" 而非 "python3.12"。

launcher（预编译的 universal binary，随 wheel 分发）在自身进程内
dlopen(libpython) 运行 daobidao，因此 TCC 看到的进程二进制
始终是我们的 launcher，不是 Python。
"""

import importlib.resources
import os
import stat
import subprocess
import sys

from daobidao.logger import get_logger

logger = get_logger(__name__)

# .app bundle 安装位置
APP_NAME = "Daobidao"
APP_BUNDLE_NAME = f"{APP_NAME}.app"
APP_INSTALL_DIR = os.path.expanduser("~/Applications")
APP_BUNDLE_PATH = os.path.join(APP_INSTALL_DIR, APP_BUNDLE_NAME)

# launcher 配置
CONFIG_DIR = os.path.expanduser("~/.config/daobidao")
VENV_PATH_FILE = os.path.join(CONFIG_DIR, "venv-path")

# bundle 标识
BUNDLE_ID = "com.daobidao.app"

# 环境变量：标识当前进程由 bundle launcher 启动
BUNDLE_ENV_KEY = "_DAOBIDAO_BUNDLE"


def get_app_bundle_path() -> str:
    """返回 .app bundle 路径。"""
    return APP_BUNDLE_PATH


def is_app_bundle_installed() -> bool:
    """检查 .app bundle 是否已安装且完好。"""
    exe = os.path.join(APP_BUNDLE_PATH, "Contents", "MacOS", "daobidao")
    plist = os.path.join(APP_BUNDLE_PATH, "Contents", "Info.plist")
    return os.path.isfile(exe) and os.path.isfile(plist)


def is_app_bundle_outdated() -> bool:
    """检查已安装的 .app bundle 版本是否与当前包版本不一致。"""
    import plistlib

    from daobidao.version import __version__

    plist_path = os.path.join(APP_BUNDLE_PATH, "Contents", "Info.plist")
    try:
        with open(plist_path, "rb") as f:
            info = plistlib.load(f)
        return info.get("CFBundleVersion") != __version__
    except (FileNotFoundError, plistlib.InvalidFileException):
        return True


def is_launched_from_bundle() -> bool:
    """判断当前进程是否从 .app bundle 启动。"""
    return os.environ.get(BUNDLE_ENV_KEY) == "1"


def _build_info_plist() -> str:
    """生成 Info.plist 内容。"""
    from daobidao.version import __version__

    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>{APP_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>{APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>{BUNDLE_ID}</string>
    <key>CFBundleVersion</key>
    <string>{__version__}</string>
    <key>CFBundleShortVersionString</key>
    <string>{__version__}</string>
    <key>CFBundleExecutable</key>
    <string>daobidao</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>叨逼叨 需要使用麦克风来进行语音识别。</string>
</dict>
</plist>
"""


def _get_prebuilt_assets():
    """获取预编译的 launcher binary 和 icns 图标。

    返回 (launcher_ref, icns_ref)，均为 importlib.resources
    的 Traversable 对象。
    """
    macos_assets = importlib.resources.files("daobidao.assets").joinpath(
        "macos"
    )
    launcher = macos_assets.joinpath("daobidao-launcher")
    icns = macos_assets.joinpath("AppIcon.icns")
    return launcher, icns


def install_app_bundle() -> str:
    """生成/更新 .app bundle。返回安装路径。

    使用随 wheel 分发的预编译 universal binary，
    不需要 Xcode Command Line Tools。
    """
    from daobidao.i18n import t

    logger.info("install_start", message=t("install.start"))

    # 1. 获取预编译资源
    launcher_ref, icns_ref = _get_prebuilt_assets()

    # 2. 创建 .app 目录结构
    contents = os.path.join(APP_BUNDLE_PATH, "Contents")
    macos_dir = os.path.join(contents, "MacOS")
    resources_dir = os.path.join(contents, "Resources")

    for d in (macos_dir, resources_dir):
        os.makedirs(d, exist_ok=True)

    # 3. 复制预编译 launcher
    exe_path = os.path.join(macos_dir, "daobidao")
    logger.info("install_launcher", message=t("install.launcher"))
    with open(exe_path, "wb") as out:
        out.write(launcher_ref.read_bytes())
    os.chmod(
        exe_path,
        stat.S_IRWXU
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH,
    )  # 755

    # 4. 写入 Info.plist
    plist_path = os.path.join(contents, "Info.plist")
    with open(plist_path, "w", encoding="utf-8") as f:
        f.write(_build_info_plist())

    # 5. 复制预生成图标
    logger.info("install_icon", message=t("install.icon"))
    icns_path = os.path.join(resources_dir, "AppIcon.icns")
    with open(icns_path, "wb") as out:
        out.write(icns_ref.read_bytes())

    # 6. Ad-hoc 签名
    logger.info("install_sign", message=t("install.sign"))
    subprocess.run(
        ["codesign", "--force", "--sign", "-", "--deep", APP_BUNDLE_PATH],
        capture_output=True,
    )

    # 7. 清掉旧 TCC 授权,避免"僵尸条目"问题。
    #    TCC 按 (bundle_id, cdhash) 索引授权记录,CI 每次 release 重新
    #    编译 launcher → cdhash 漂移。旧条目仍会显示在系统设置列表里
    #    并看似"已打开",但实际对新 cdhash 不生效,用户只能手动删除。
    #    这里主动清掉,让用户下次启动看到规范的新授权弹窗。
    #    首次安装时无旧条目,tccutil 也返回 0,不影响。
    logger.info("install_tcc_reset", bundle_id=BUNDLE_ID)
    subprocess.run(
        ["tccutil", "reset", "Accessibility", BUNDLE_ID],
        capture_output=True,
    )

    # 8. 保存 venv 路径
    _save_venv_path()

    logger.info(
        "install_done",
        path=APP_BUNDLE_PATH,
        message=t("install.done", path=APP_BUNDLE_PATH),
    )
    return APP_BUNDLE_PATH


def _save_venv_path() -> None:
    """将当前 venv 的 sys.prefix 保存到配置文件。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(VENV_PATH_FILE, "w", encoding="utf-8") as f:
        f.write(sys.prefix + "\n")


def update_venv_path() -> None:
    """更新已保存的 venv 路径（用于 uv tool upgrade 后）。"""
    if os.path.isfile(VENV_PATH_FILE):
        _save_venv_path()


def launch_via_bundle(extra_args: list[str] | None = None) -> None:
    """通过 open -a 启动 .app bundle，当前进程退出。"""
    cmd = ["/usr/bin/open", "-a", APP_BUNDLE_PATH]
    if extra_args:
        cmd += ["--args", *extra_args]
    subprocess.Popen(cmd)
    sys.exit(0)


def restart_via_bundle() -> None:
    """在 bundle 模式下重启应用。

    直接 execv 到 launcher binary，绕开 LaunchServices。
    这样避免 open -a 因 LaunchServices 缓存过时返回 -600 的问题。
    binary 在 .app/Contents/MacOS/ 下且 cdhash 不变，TCC 身份仍然归属
    Daobidao bundle。
    """
    launcher = os.path.join(APP_BUNDLE_PATH, "Contents", "MacOS", "daobidao")
    os.execv(launcher, [launcher])


def _confirm(prompt: str) -> bool:
    """交互式确认（y/N）。"""
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


def uninstall_cleanup() -> None:
    """卸载前清理，供 `daobidao --uninstall` 调用。

    自动清理：.app bundle、LaunchAgent、TCC 授权、venv-path。
    交互确认：配置文件、模型缓存。
    """
    import shutil

    from daobidao.backends.autostart_macos import (
        AUTOSTART_FILE,
        AUTOSTART_LABEL,
    )
    from daobidao.i18n import t

    # ── 自动清理部分 ──────────────────────────────────

    if os.path.exists(AUTOSTART_FILE):
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{AUTOSTART_LABEL}"],
            capture_output=True,
        )
        os.remove(AUTOSTART_FILE)
        print(
            f"[uninstall] "
            f"{t('uninstall.removed_launchagent', path=AUTOSTART_FILE)}"
        )
    else:
        print(f"[uninstall] {t('uninstall.no_launchagent')}")

    # 2. 重置 TCC 授权（按 bundle ID）
    for service in ("Accessibility", "ListenEvent"):
        subprocess.run(
            ["tccutil", "reset", service, BUNDLE_ID],
            capture_output=True,
        )
    print(f"[uninstall] {t('uninstall.reset_tcc', bundle_id=BUNDLE_ID)}")

    # 3. 删除 .app bundle
    if os.path.isdir(APP_BUNDLE_PATH):
        shutil.rmtree(APP_BUNDLE_PATH)
        print(f"[uninstall] {t('uninstall.removed_app', path=APP_BUNDLE_PATH)}")
    else:
        print(f"[uninstall] {t('uninstall.no_app')}")

    # 4. 删除 venv-path 配置
    if os.path.isfile(VENV_PATH_FILE):
        os.remove(VENV_PATH_FILE)

    # ── 交互确认部分 ──────────────────────────────────

    # 5. 配置文件
    from daobidao.config_manager import CONFIG_DIR

    if os.path.isdir(CONFIG_DIR):
        if _confirm(t("uninstall.confirm_config", path=CONFIG_DIR)):
            shutil.rmtree(CONFIG_DIR)
            print(
                f"[uninstall] {t('uninstall.removed_config', path=CONFIG_DIR)}"
            )
        else:
            print(f"[uninstall] {t('uninstall.keep_config')}")
    else:
        print(f"[uninstall] {t('uninstall.no_config', path=CONFIG_DIR)}")

    # 6. 模型缓存（modelscope 新旧版本路径都检查）
    # 同时清 Qwen3-ASR(当前)和 SenseVoiceSmall(≤0.7.3 老用户)两条线,
    # 确保 --uninstall 不遗留历史缓存。
    model_dirs = []
    for base, org in (
        ("~/.cache/modelscope/hub/models/zengshuishui", "zengshuishui"),
        ("~/.cache/modelscope/hub/zengshuishui", "zengshuishui"),
        ("~/.cache/modelscope/hub/models/iic", "iic"),
        ("~/.cache/modelscope/hub/iic", "iic"),
    ):
        base_path = os.path.expanduser(base)
        names = (
            ("Qwen3-ASR-onnx",)
            if org == "zengshuishui"
            else ("SenseVoiceSmall-onnx", "SenseVoiceSmall")
        )
        for name in names:
            d = os.path.join(base_path, name)
            if os.path.isdir(d):
                model_dirs.append(d)
    if model_dirs:
        # 计算总大小
        total = 0
        for d in model_dirs:
            for root, _dirs, files in os.walk(d):
                total += sum(
                    os.path.getsize(os.path.join(root, f)) for f in files
                )
        size_mb = total / (1024 * 1024)
        if _confirm(t("uninstall.confirm_model", size_mb=f"{size_mb:.0f}")):
            for d in model_dirs:
                shutil.rmtree(d)
            print(f"[uninstall] {t('uninstall.removed_model')}")
        else:
            print(f"[uninstall] {t('uninstall.keep_model')}")
    else:
        print(f"[uninstall] {t('uninstall.no_model')}")

    print()
    print(t("uninstall.done"))
