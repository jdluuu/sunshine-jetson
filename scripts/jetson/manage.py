#!/usr/bin/env python3
"""User-session launcher for the Sunshine Jetson fork (2026-10-20).

SPDX-License-Identifier: GPL-3.0-only
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid


def displays(text):
    """XRandR output order includes disconnected connectors, as Sunshine does."""
    result = []
    for line in text.splitlines():
        match = re.match(r"^(\S+) (connected|disconnected|unknown connection)\b(.*)", line)
        if match:
            name, state, rest = match.groups()
            result.append({
                "index": len(result), "name": name,
                "active": state == "connected" and bool(
                    re.search(r"\b\d+x\d+[+-]\d+[+-]\d+", rest)),
                "primary": "primary" in rest.split(),
            })
    return result


def choose_output(text, requested=None):
    available = [item for item in displays(text) if item["active"]]
    if requested is not None:
        available = [item for item in available if requested in
                     (item["name"], str(item["index"]))]
    if not available:
        raise RuntimeError("没有可用的 X11 显示器；请登录 Xorg 桌面并连接显示器/显示器模拟器。")
    return str(next((item for item in available if item["primary"]), available[0])["index"])


def update_config(text, values):
    names = "|".join(re.escape(key) for key in values)
    kept = [line for line in text.splitlines()
            if not re.match(r"^\s*(?:" + names + r")\s*=", line)]
    return "\n".join(kept + [key + " = " + str(value) for key, value in values.items()]) + "\n"


def atomic_text(path, content, mode=0o600):
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(content)
    temporary.chmod(mode)
    temporary.replace(path)


def atomic_link(path, target):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def own_pid(root):
    try:
        pid = int((root / "run.pid").read_text())
        exe = Path("/proc", str(pid), "exe").resolve()
        if (root / "releases").resolve() in exe.parents:
            return pid
    except (OSError, ValueError):
        pass
    return None


def stop(root):
    pid = own_pid(root)
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if own_pid(root) is None:
                return
            time.sleep(0.1)
        raise RuntimeError("Sunshine 尚未退出，未强制结束进程。")


def run(root):
    lock = (root / "run.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    # Keep this descriptor across exec so a second launcher cannot start.
    os.set_inheritable(lock.fileno(), True)
    existing = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", "sunshine"],
                              stdout=subprocess.DEVNULL)
    if existing.returncode == 0:
        raise RuntimeError("已有其他 Sunshine 正在运行。请先关闭旧实例，再执行 start。")
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/" + str(os.getuid()))
    env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                   "unix:path=" + env["XDG_RUNTIME_DIR"] + "/bus")
    if env.get("XDG_SESSION_TYPE") == "wayland":
        raise RuntimeError("本版需要 Xorg 桌面；请在登录界面选择 Xorg 会话。")
    query = subprocess.run(["xrandr", "--query"], env=env, capture_output=True, text=True)
    if query.returncode:
        raise RuntimeError("无法访问 X11 桌面。请从桌面终端启动，或设置正确的 DISPLAY/XAUTHORITY。")
    config_dir = root / "config"
    config_dir.mkdir(mode=0o700, exist_ok=True)
    preferences = json.loads((root / "preferences.json").read_text())
    config = config_dir / "sunshine.conf"
    text = config.read_text() if config.exists() else ""
    atomic_text(config, update_config(text, {
        "encoder": "jetson", "capture": "x11", "hevc_mode": 1,
        "output_name": choose_output(query.stdout, preferences.get("output")),
    }))
    if not os.access("/dev/uinput", os.W_OK):
        raise RuntimeError("当前用户无法写入 /dev/uinput；请重新运行安装脚本设置输入权限。")
    env["SUNSHINE_JETSON_CONFIG_DIR"] = str(config_dir)
    atomic_text(root / "run.pid", str(os.getpid()))
    binary = (root / "current/bin/sunshine").resolve(strict=True)
    os.execve(str(binary), [str(binary), str(config)], env)


def start(root):
    if own_pid(root):
        print("Sunshine Jetson 已在运行。")
        return
    log_path = root / "console.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen([sys.executable, str(root / "manage.py"), "run"],
                                   stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(150):
        if process.poll() is not None:
            raise RuntimeError("启动失败：\n" + log_path.read_text(errors="replace")[-3000:])
        text = log_path.read_text(errors="replace")
        if ("Found encoder jetson: [nvv4l2h264enc]" in text
                and "Configuration UI available" in text):
            print("已启动硬件编码。管理页面：https://localhost:47990")
            return
        time.sleep(0.2)
    stop(root)
    raise RuntimeError("启动超时，已停止新实例。请查看 " + str(log_path))


def rollback(root):
    previous = root / "previous"
    if not previous.is_symlink() or not previous.resolve().is_dir():
        raise RuntimeError("这是首次安装，没有上一版；可执行 disable 停止自动启动。")
    stop(root)
    old_target = previous.resolve()
    new_target = (root / "current").resolve()
    atomic_link(root / "current", old_target)
    atomic_link(previous, new_target)
    print("已回退程序版本；配置和配对信息保留。运行 start 启动。")


def main():
    parser = argparse.ArgumentParser(description="Sunshine Jetson 启动、停止与版本回退")
    parser.add_argument("action", choices=["run", "start", "stop", "status", "rollback", "disable"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.action == "run":
        run(root)
    elif args.action == "start":
        start(root)
    elif args.action == "stop":
        stop(root)
    elif args.action == "rollback":
        rollback(root)
    elif args.action == "disable":
        stop(root)
        autostart = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        desktop = autostart / "autostart/sunshine-jetson.desktop"
        if desktop.exists() and str(root) in desktop.read_text():
            desktop.unlink()
        print("已停止并关闭登录自动启动，程序与配置仍保留。")
    else:
        pid = own_pid(root)
        print("运行中，PID " + str(pid) if pid else "未运行")
        print("日志：" + str(root / "console.log"))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as error:
        sys.exit(str(error))
