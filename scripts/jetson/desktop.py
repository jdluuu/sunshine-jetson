"""Install the AppImage as a desktop application and switch login autostart."""
import getpass
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import uuid

from manage import atomic_text

NAME = "sunshine-jetson-appimage.desktop"


def desktop_value(text):
    return str(text).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def desktop_arg(text):
    # Exec quoting is decoded once as a desktop value, then as an argument.
    text = str(text).replace("%", "%%")
    text = "".join("\\" + char if char in '\\\"`$' else char for char in text)
    return '"' + desktop_value(text) + '"'


def old_services():
    units = ("sunshine.service", "sunshine-jetson.service",
             "sunshine@" + getpass.getuser() + ".service")
    result = []
    for user in (True, False):
        prefix = ["systemctl"] + (["--user"] if user else [])
        for unit in units:
            probe = subprocess.run(prefix + ["show", unit, "--property=LoadState", "--value"],
                                   capture_output=True, text=True)
            if probe.stdout.strip() == "loaded":
                result.append((user, unit))
            elif probe.stdout.strip() not in ("not-found", "masked") and probe.returncode:
                raise RuntimeError("无法检查 Sunshine 服务：" + probe.stderr.strip())
    return result


def install_autostart(appdir, config, output=None, port=47989):
    image = Path(os.environ.get("APPIMAGE", "")).expanduser().resolve()
    if not image.is_file():
        raise RuntimeError("请直接运行 AppImage 的 --install-autostart 命令。")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    target = data_home / "sunshine-jetson-appimage"
    autostart = config_home / "autostart"
    applications = data_home / "applications"
    for path in (target, autostart, applications):
        path.mkdir(parents=True, exist_ok=True)
    installed = target / "Sunshine-Jetson.AppImage"
    if image != installed.resolve():
        temporary = installed.with_name(installed.name + ".new")
        shutil.copy2(image, temporary)
        temporary.chmod(0o755)
        temporary.replace(installed)
    icon = target / "sunshine.png"
    shutil.copy2(appdir / "sunshine.png", icon)
    args = [str(installed), "--config-dir", str(config), "--desktop"]
    if output is not None:
        args += ["--output", output]
    if port != 47989:
        args += ["--port", str(port)]
    entry = ("[Desktop Entry]\nType=Application\nName=Sunshine Jetson\n"
             "Exec=/usr/bin/env " + " ".join(desktop_arg(arg) for arg in args) + "\n"
             "Icon=" + desktop_value(icon) + "\nTerminal=false\n"
             "Categories=Network;RemoteAccess;\nX-GNOME-Autostart-enabled=true\n"
             "X-GNOME-Autostart-Delay=3\n")
    # Prepare the replacement before disabling the previous startup mechanism.
    prepared = target / "autostart.desktop.new"
    atomic_text(prepared, entry, 0o644)
    services = old_services()
    for user, unit in services:
        command = ["systemctl"] + (["--user"] if user else [])
        if not user:
            command.insert(0, "sudo")
        subprocess.run(command + ["disable", "--now", unit], check=True)
    directories = [Path(p) / "autostart" for p in
                   os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":") if p]
    directories.append(autostart)
    old_names = set()
    for directory in directories:
        for path in directory.glob("*.desktop"):
            if path.name == NAME:
                continue
            text = path.read_text(errors="replace")
            commands = [line.lower() for line in text.splitlines() if line.startswith("Exec=")]
            if "sunshine" in path.name.lower() or any("sunshine" in line for line in commands):
                old_names.add(path.name)
    backups = target / "autostart-backups" / uuid.uuid4().hex
    for name in sorted(old_names):
        previous = autostart / name
        disabled = "[Desktop Entry]\nType=Application\nName=Sunshine\nHidden=true\n"
        if previous.exists() and previous.read_text(errors="replace") == disabled:
            continue
        if previous.exists():
            backups.mkdir(parents=True, exist_ok=True)
            shutil.copy2(previous, backups / name)
        atomic_text(previous, disabled, 0o644)
    atomic_text(applications / NAME, entry, 0o644)
    atomic_text(autostart / NAME, entry, 0o644)
    prepared.unlink()
    print("已停用旧 Sunshine 自启动，改为登录桌面后启动本 AppImage。")
    print("程序：" + str(installed))
    print("立即启动：nohup " + shlex.join(args) + " >/dev/null 2>&1 &")
