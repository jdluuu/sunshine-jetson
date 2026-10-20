#!/usr/bin/env python3
"""Orin NX / JetPack 5 AppImage launcher. SPDX-License-Identifier: GPL-3.0-only"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from install import configure_input, platform_check, runtime_check
from manage import atomic_text, choose_output, update_config


def ensure_apps(path):
    """Initialize apps or repair the missing env field in early AppImages."""
    if not path.exists():
        atomic_text(path, json.dumps({
            "env": {}, "apps": [{"name": "Desktop", "image-path": "desktop.png"}],
        }, indent=2) + "\n")
        return
    original = path.read_bytes()
    try:
        data = json.loads(original)
    except ValueError as error:
        raise RuntimeError("应用配置不是有效 JSON，请检查：" + str(path)) from error
    if not isinstance(data, dict):
        raise RuntimeError("应用配置缺少 apps 列表，请检查：" + str(path))
    if "env" in data:
        return
    if not isinstance(data.get("apps"), list):
        raise RuntimeError("应用配置缺少 apps 列表，请检查：" + str(path))
    # Sunshine 0.20 requires this node even when no custom variables are set.
    # Preserve custom apps, pairing, and the exact pre-migration file.
    with tempfile.NamedTemporaryFile(
            mode="wb", prefix=path.name + ".before-env-fix-",
            suffix=".bak", dir=path.parent, delete=False) as backup:
        backup.write(original)
    data["env"] = {}
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Sunshine Jetson AppImage：Orin NX / JetPack 5 / Xorg")
    parser.add_argument("--version", action="version",
                        version="Sunshine Jetson v0.20.0-jetson.2 (Sunshine 0.20.0 / JetPack 5)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="检查设备、插件和运行库，不启动服务")
    action.add_argument("--setup-input", action="store_true", help="通过 sudo 设置一次键鼠权限")
    action.add_argument("--probe", type=Path, metavar="OUTPUT.h264", help="实际编码 90 帧并检查关键帧")
    parser.add_argument("--config-dir", type=Path, help="独立配置目录，默认为 ~/.config/sunshine-jetson-appimage")
    parser.add_argument("--output", help="显示器名称或编号，默认活动主屏")
    parser.add_argument("--port", type=int, default=47989, help="基础端口，默认 47989")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65514:
        parser.error("--port 必须在 1024 至 65514 之间")
    platform_check()
    appdir = Path(__file__).resolve().parents[3]
    binary = appdir / "usr/bin/sunshine"
    env = os.environ.copy()
    # Limit bundled libraries to Sunshine children; never preload them into Python/sudo.
    env["LD_LIBRARY_PATH"] = str(appdir / "usr/lib") + (
        ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    for executable in (binary, appdir / "usr/bin/jetson-probe"):
        result = subprocess.run(["ldd", str(executable)], env=env, capture_output=True, text=True)
        if result.returncode or "not found" in result.stdout:
            raise RuntimeError("缺少系统运行库：\n" + result.stdout + result.stderr)
    runtime_check()
    if args.setup_input:
        configure_input()
        print("键鼠权限已设置。之后直接运行 AppImage 即可。")
        return
    if args.probe:
        target = args.probe.expanduser().resolve()
        if target.exists():
            raise RuntimeError("测试输出已存在，拒绝覆盖：" + str(target))
        subprocess.run([str(appdir / "usr/bin/jetson-probe"), str(target)], env=env, check=True)
        return
    if args.check:
        print("设备、编码插件和动态库检查通过。")
        print("键鼠权限：" + ("可用" if os.access("/dev/uinput", os.W_OK)
                              else "未配置，请执行本 AppImage --setup-input"))
        return
    if not os.access("/dev/uinput", os.W_OK):
        raise RuntimeError("无法写入 /dev/uinput；请先执行本 AppImage --setup-input。")
    if not env.get("DISPLAY") or env.get("XDG_SESSION_TYPE") == "wayland":
        raise RuntimeError("请从已登录的 Xorg 桌面终端启动；通过 SSH 时需设置 DISPLAY/XAUTHORITY。")
    if args.port == 47989 and subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-x", "sunshine"], stdout=subprocess.DEVNULL).returncode == 0:
        raise RuntimeError("已有 Sunshine 在运行，请先关闭旧实例。")
    query = subprocess.check_output(["xrandr", "--query"], text=True)
    display = choose_output(query, args.output)
    config_dir = (args.config_dir or Path(os.environ.get(
        "XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "sunshine-jetson-appimage").expanduser().resolve()
    if config_dir == Path.home().resolve():
        raise RuntimeError("请指定专用配置目录，不能直接使用主目录。")
    config_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    config_dir.chmod(0o700)
    lock = (config_dir / "appimage.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("此 AppImage 配置目录已有实例正在运行。") from None
    os.set_inheritable(lock.fileno(), True)
    config = config_dir / "sunshine.conf"
    text = config.read_text() if config.exists() else ""
    atomic_text(config, update_config(text, {
        "encoder": "jetson", "capture": "x11", "hevc_mode": 1,
        "output_name": display, "port": args.port,
    }))
    ensure_apps(config_dir / "apps.json")
    env["SUNSHINE_JETSON_CONFIG_DIR"] = str(config_dir)
    print("配置：" + str(config_dir), flush=True)
    print("管理页面：https://localhost:" + str(args.port + 1), flush=True)
    # SUNSHINE_CONFIGURE_APPIMAGE uses ./usr/share/sunshine relative to this directory.
    os.chdir(appdir)
    os.execve(str(binary), [str(binary), str(config)], env)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit("Sunshine Jetson AppImage：" + str(error))
