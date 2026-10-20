#!/usr/bin/env python3
"""Build and install the Orin NX / JetPack 5 fork of Sunshine.

Added 2026-10-20. SPDX-License-Identifier: GPL-3.0-only
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from manage import atomic_link, atomic_text, choose_output, ensure_apps, own_pid, start, stop

SOURCE = Path(__file__).resolve().parents[2]
SUBMODULES = [
    "third-party/Simple-Web-Server", "third-party/ffmpeg-linux-aarch64",
    "third-party/miniupnp", "third-party/moonlight-common-c",
    "third-party/nanors", "third-party/nv-codec-headers", "third-party/tray",
]
PACKAGES = """build-essential git ca-certificates pkg-config python3 python3-venv
python3-pip nodejs npm acl kmod udev x11-xserver-utils procps
libboost-filesystem-dev libboost-locale-dev libboost-log-dev
libboost-program-options-dev libboost-thread-dev libssl-dev libcurl4-openssl-dev
libcap-dev libdrm-dev libevdev-dev libnuma-dev libopus-dev libpulse-dev
libva-dev libvdpau-dev libx11-dev libx11-xcb-dev libxcb1-dev libxcb-shm0-dev
libxcb-xfixes0-dev libxcb-shape0-dev libxcb-render0-dev libxfixes-dev
libxrandr-dev libxtst-dev libxinerama-dev libxcursor-dev libxi-dev
libappindicator3-dev libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev gstreamer1.0-tools
gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad""".split()


def command(argv, **kwargs):
    print("+ " + " ".join(map(str, argv)), flush=True)
    return subprocess.run(list(map(str, argv)), check=True, **kwargs)


def platform_check():
    if os.geteuid() == 0:
        raise RuntimeError("请用桌面登录用户运行，勿用 sudo 执行整个脚本。")
    model = Path("/proc/device-tree/model")
    board = model.read_text().rstrip("\0") if model.exists() else ""
    if platform.machine() != "aarch64" or "Orin NX" not in board:
        raise RuntimeError("首版仅支持 aarch64 Orin NX；不支持没有 NVENC 的 Orin Nano。")
    release = Path("/etc/nv_tegra_release").read_text()
    os_release = Path("/etc/os-release").read_text()
    if not re.search(r"^# R35\b", release) or 'VERSION_ID="20.04"' not in os_release:
        raise RuntimeError("首版要求 JetPack 5 / L4T R35 / Ubuntu 20.04。其他版本尚未验证。")
    if not Path("/dev/nvhost-msenc").exists():
        raise RuntimeError("未发现 NVENC 设备 /dev/nvhost-msenc，请检查 JetPack 驱动。")
    print("设备：" + board + "\n" + release.splitlines()[0], flush=True)


def runtime_check():
    for name in ("nvvidconv", "nvv4l2h264enc", "h264parse", "appsrc", "appsink"):
        command(["gst-inspect-1.0", name], stdout=subprocess.DEVNULL)
    print("Jetson GStreamer 编码组件检查通过。", flush=True)


def find_cmake(build):
    # Pin a version compatible with both Ubuntu 20.04 and the older upstream CMake files.
    venv = build / "tools"
    cmake = venv / "bin/cmake"
    if not cmake.exists():
        command([sys.executable, "-m", "venv", venv])
        command([venv / "bin/python", "-m", "pip", "install", "--disable-pip-version-check",
                 "--only-binary=:all:", "cmake==3.27.9"])
    return cmake


def check_build_memory(jobs, meminfo=None):
    """Check available RAM; Jetson zram swap is not additional physical memory."""
    text = Path("/proc/meminfo").read_text() if meminfo is None else meminfo
    available = re.search(r"^MemAvailable:\s+(\d+)", text, re.M)
    if available is None:
        raise RuntimeError("无法读取可用内存，未开始编译。")
    required_gib = 1 + 2 * jobs
    if int(available.group(1)) < required_gib * 1024 * 1024:
        raise RuntimeError(
            "-j" + str(jobs) + " 编译前至少需要约 " + str(required_gib)
            + " GiB 可用内存（不计 swap/zram）；请关闭占用较大的程序或降低 --jobs。")


def configure_input():
    rule = 'KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"\n'
    rule_path = Path("/etc/udev/rules.d/70-sunshine-jetson-uinput.rules")
    if rule_path.exists() and rule_path.read_text() != rule:
        raise RuntimeError("已有不同的 " + str(rule_path) + "，请手动检查后再安装。")
    command(["sudo", "tee", rule_path], input=rule, text=True, stdout=subprocess.DEVNULL)
    command(["sudo", "modprobe", "uinput"])
    command(["sudo", "udevadm", "control", "--reload-rules"])
    command(["sudo", "udevadm", "trigger", "--action=change",
             "--subsystem-match=misc", "--sysname-match=uinput"])
    command(["sudo", "setfacl", "-m", "u:" + str(os.getuid()) + ":rw", "/dev/uinput"])


def deploy(stage, root, output, autostart):
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    current = root / "current"
    if current.exists() and not current.is_symlink():
        raise RuntimeError("安装目录 current 不是符号链接，拒绝覆盖。")
    old = current.resolve() if current.is_symlink() else None
    if old and (root / "releases").resolve() not in old.parents:
        raise RuntimeError("current 指向安装目录之外，拒绝覆盖。")
    running = own_pid(root) is not None
    config = root / "config"
    if config.exists():
        backup = root / "backups" / stage.name
        backup.parent.mkdir(exist_ok=True)
        shutil.copytree(config, backup)
    else:
        config.mkdir(mode=0o700)
    shutil.copy2(SOURCE / "scripts/jetson/manage.py", root / "manage.py")
    preferences = root / "preferences.json"
    settings = json.loads(preferences.read_text()) if preferences.exists() else {}
    if output is not None:
        settings["output"] = output
    atomic_text(preferences, json.dumps(settings))
    apps = config / "apps.json"
    ensure_apps(apps)
    # No modifications to the original Sunshine config, binary, or service.
    stop(root)
    atomic_link(current, stage)
    if old:
        atomic_link(root / "previous", old)
    other = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", "sunshine"],
                           stdout=subprocess.DEVNULL).returncode == 0
    desktop_session = bool(os.environ.get("DISPLAY")) and os.environ.get("XDG_SESSION_TYPE") != "wayland"
    try:
        if desktop_session and not other:
            start(root)
        else:
            print("程序已安装，未自动启动：请关闭旧 Sunshine，并在 Xorg 桌面终端执行 start。")
    except BaseException:
        stop(root)
        if old:
            atomic_link(current, old)
            if running:
                start(root)
        else:
            current.unlink()
        print("新实例启动失败，程序链接已回退；日志保留在安装目录。", file=sys.stderr)
        raise
    if autostart and not other:
        target = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        target = target / "autostart/sunshine-jetson.desktop"
        target.parent.mkdir(parents=True, exist_ok=True)
        launcher = str(root / "manage.py")
        # Desktop Exec uses its own quoting, not a shell; escape reserved characters.
        launcher = launcher.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
        launcher = launcher.replace("$", "\\$").replace("%", "%%")
        atomic_text(target, '[Desktop Entry]\nType=Application\nName=Sunshine Jetson\n'
                    'Exec=/usr/bin/python3 "' + launcher + '" start\n'
                    'Terminal=false\nX-GNOME-Autostart-enabled=true\n', 0o644)
    print("安装完成。配置与配对目录：" + str(config))
    print("管理命令：python3 " + str(root / "manage.py") + " {start|stop|status|rollback|disable}")
    print("首次启动后访问 https://localhost:47990 设置账号，并在 Moonlight 中重新配对。")


def main():
    parser = argparse.ArgumentParser(description="Orin NX / JetPack 5 Sunshine 一键源码安装")
    parser.add_argument("--check", action="store_true", help="只检查硬件和编码插件")
    parser.add_argument("--build-only", action="store_true", help="构建和测试，不安装、不改权限或启动服务")
    parser.add_argument("--skip-deps", action="store_true", help="依赖已准备好时跳过 apt（安装时仍会设置输入权限）")
    parser.add_argument("--no-autostart", action="store_true", help="不创建登录自动启动项")
    parser.add_argument("--output", help="显示器名称或编号；默认每次启动选择活动主显示器")
    parser.add_argument("--jobs", type=int, default=2, help="编译并发数，默认 2；内存紧张时用 1")
    parser.add_argument("--build-dir", type=Path, default=SOURCE / "build-jetson")
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local/opt/sunshine-jetson")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs 必须至少为 1")
    platform_check()
    if args.check:
        runtime_check()
        return
    root = args.prefix.expanduser().resolve()
    build = args.build_dir.expanduser().resolve()
    if Path.home().resolve() not in root.parents:
        raise RuntimeError("--prefix 必须在当前用户主目录中。")
    if root == Path.home().resolve() or any(c in str(root) for c in "\n\r;"):
        raise RuntimeError("不支持这个安装目录。")
    check_build_memory(args.jobs)
    build.mkdir(parents=True, exist_ok=True)
    lock = (build / "install.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if shutil.disk_usage(build).free < 8 * 1024**3:
        raise RuntimeError("构建目录至少需要 8 GiB 可用空间。")
    if not args.skip_deps:
        command(["sudo", "apt-get", "update"])
        command(["sudo", "apt-get", "install", "-y", "--no-install-recommends", "--no-upgrade"] + PACKAGES)
    runtime_check()
    command(["git", "-C", SOURCE, "submodule", "update", "--init", "--recursive", "--depth", "1", "--"] + SUBMODULES)
    command(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=SOURCE)
    commit = subprocess.check_output(["git", "-C", SOURCE, "rev-parse", "HEAD"], text=True).strip()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stage = (build / "stage") if args.build_only else (root / "releases" / (commit[:12] + "-" + stamp))
    cmake = find_cmake(build)
    env = os.environ.copy()
    env.update(BRANCH="master", BUILD_VERSION="0.20.0", COMMIT=commit)
    command([cmake, "-S", SOURCE, "-B", build / "cmake",
             "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_CXX_FLAGS_RELEASE=-O2 -DNDEBUG -g0",
             "-DCMAKE_C_FLAGS_RELEASE=-O2 -DNDEBUG -g0",
             "-DSUNSHINE_ENABLE_JETSON=ON", "-DSUNSHINE_ENABLE_CUDA=OFF",
             "-DSUNSHINE_ENABLE_DRM=OFF", "-DSUNSHINE_ENABLE_WAYLAND=OFF",
             "-DSUNSHINE_ENABLE_X11=ON", "-DSUNSHINE_ENABLE_TRAY=ON",
             "-DCMAKE_INSTALL_PREFIX=" + str(stage), "-DSUNSHINE_ASSETS_DIR=share/sunshine",
             "-DSUNSHINE_EXECUTABLE_PATH=" + str(stage / "bin/sunshine")], env=env)
    check_build_memory(args.jobs)
    command(["nice", "-n", "15", cmake, "--build", build / "cmake", "-j" + str(args.jobs)])
    gst_flags = subprocess.check_output(["pkg-config", "--cflags", "--libs",
                                        "gstreamer-app-1.0", "gstreamer-video-1.0"], text=True).split()
    command(["g++", "-O2", "-std=c++17", "-I" + str(SOURCE / "src"),
             SOURCE / "src/jetson_encoder.cpp", SOURCE / "tests/jetson_probe.cpp",
             "-o", build / "jetson-probe"] + gst_flags)
    command([build / "jetson-probe", build / "probe.h264"])
    binary = build / "cmake/sunshine"
    linked = subprocess.check_output(["ldd", binary], text=True)
    if "not found" in linked:
        raise RuntimeError("程序缺少运行库：\n" + linked)
    (stage / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, stage / "bin/sunshine")
    assets = stage / "share/sunshine"
    shutil.copytree(SOURCE / "src_assets/common/assets", assets, dirs_exist_ok=True)
    shutil.copytree(SOURCE / "src_assets/linux/assets", assets, dirs_exist_ok=True)
    shutil.copytree(SOURCE / "node_modules", assets / "web/node_modules", dirs_exist_ok=True)
    shutil.copy2(SOURCE / "sunshine.png", assets / "sunshine.png")
    shutil.copy2(SOURCE / "LICENSE", stage / "LICENSE")
    atomic_text(stage / "build-info.json", json.dumps({
        "source": "https://github.com/jdluuu/sunshine-jetson", "commit": commit,
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "backend": "nvvidconv + nvv4l2h264enc", "jobs": args.jobs,
    }, indent=2))
    if args.build_only:
        print("构建与硬件编码测试通过。完整程序与网页资源：" + str(stage))
        return
    # Do privileged input setup only after a successful build and hardware test.
    configure_input()
    if os.environ.get("DISPLAY") and os.environ.get("XDG_SESSION_TYPE") != "wayland":
        text = subprocess.check_output(["xrandr", "--query"], text=True)
        choose_output(text, args.output)
    deploy(stage, root, args.output, not args.no_autostart)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit("安装未完成：" + str(error))
