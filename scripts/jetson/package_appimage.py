#!/usr/bin/env python3
"""Package an already built, relocatable aarch64 Sunshine. GPL-3.0-only."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile

SOURCE = Path(__file__).resolve().parents[2]
# Keep the OS, display/audio stack and JetPack GStreamer ABI together on the host.
HOST = re.compile(
    r"^(?:lib(?:c|m|pthread|dl|rt|resolv|nsl|crypt|gcc_s|stdc\+\+)\.so"
    r"|lib(?:gst|glib-|gobject-|gmodule-|gthread-|gio-|orc-|ffi\.|pcre\.|pulse|dbus-|systemd\.|udev\.)"
    r"|lib(?:X|xcb|drm|GL|EGL|GLES|nvidia|nv)|ld-linux)"
)


def dependencies(binary):
    output = subprocess.check_output(["ldd", str(binary)], text=True)
    if "not found" in output:
        raise RuntimeError("Unresolved dependencies:\n" + output)
    return {name: Path(path) for name, path in re.findall(r"^\s*(\S+) => (/\S+) \(", output, re.M)}


def package(binary, probe, assets, runtime, runtime_sha256, runtime_license, output, build_jobs):
    if platform.machine() != "aarch64":
        raise RuntimeError("Package on the target aarch64 JetPack 5 system.")
    if output.exists():
        raise RuntimeError("Output already exists: " + str(output))
    data = binary.read_bytes()
    if b"./usr/share/sunshine" not in data:
        raise RuntimeError("Build with SUNSHINE_CONFIGURE_APPIMAGE=ON and CMAKE_INSTALL_PREFIX=/usr.")
    runtime_data = runtime.read_bytes()
    if hashlib.sha256(runtime_data).hexdigest() != runtime_sha256:
        raise RuntimeError("AppImage runtime SHA256 mismatch")
    if runtime_data[:4] != b"\x7fELF" or runtime_data[8:11] != b"AI\x02" or runtime_data[18:20] != b"\xb7\x00":
        raise RuntimeError("Expected an official aarch64 type-2 runtime")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sunshine-appimage-", dir=output.parent) as temporary:
        appdir = Path(temporary) / "Sunshine.AppDir"
        for part in ("usr/bin", "usr/lib", "usr/share/sunshine", "usr/share/jetson", "usr/share/licenses"):
            (appdir / part).mkdir(parents=True)
        shutil.copy2(binary, appdir / "usr/bin/sunshine")
        shutil.copy2(probe, appdir / "usr/bin/jetson-probe")
        shutil.copytree(assets, appdir / "usr/share/sunshine", dirs_exist_ok=True)
        for name in ("appimage_run.py", "install.py", "manage.py"):
            shutil.copy2(SOURCE / "scripts/jetson" / name, appdir / "usr/share/jetson" / name)
        shutil.copy2(SOURCE / "LICENSE", appdir / "usr/share/licenses/Sunshine-GPL-3.0")
        shutil.copy2(SOURCE / "third-party/ffmpeg-linux-aarch64/LICENSE",
                     appdir / "usr/share/licenses/FFmpeg-bundle-LICENSE")
        shutil.copy2(runtime_license, appdir / "usr/share/licenses/AppImage-runtime-LICENSE")
        shutil.copy2(SOURCE / "sunshine.png", appdir / "sunshine.png")
        (appdir / ".DirIcon").symlink_to("sunshine.png")
        (appdir / "sunshine.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Sunshine Jetson\nExec=AppRun\n"
            "Icon=sunshine\nTerminal=true\nCategories=Network;RemoteAccess;\n"
            "X-AppImage-Arch=aarch64\n")
        (appdir / "AppRun").write_text(
            '#!/bin/sh\nset -eu\nAPPDIR_PATH=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            'exec /usr/bin/python3 -B "$APPDIR_PATH/usr/share/jetson/appimage_run.py" "$@"\n')
        (appdir / "AppRun").chmod(0o755)
        manifest = {"source": "https://github.com/jdluuu/sunshine-jetson",
                    "package_version": "v0.20.0-jetson.2",
                    "encoder_source_commit": "b469cc5b856363eb3783770f57373e22743ba55d",
                    "target": "Orin NX / L4T R35 / Ubuntu 20.04 / Xorg",
                    "build_jobs": build_jobs,
                    "binary_sha256": hashlib.sha256(data).hexdigest(),
                    "runtime_source": "https://github.com/AppImage/type2-runtime",
                    "runtime_sha256": runtime_sha256, "bundled": {}, "host": [],
                    "launcher_sources": {
                        name: hashlib.sha256((SOURCE / "scripts/jetson" / name).read_bytes()).hexdigest()
                        for name in ("appimage_run.py", "install.py", "manage.py", "package_appimage.py")
                    }}
        linked = dependencies(binary)
        linked.update(dependencies(probe))
        for name, path in sorted(linked.items()):
            if HOST.match(name):
                manifest["host"].append(name)
                continue
            shutil.copy2(path.resolve(), appdir / "usr/lib" / name)
            owner = None
            for candidate in (str(path), str(path.resolve()), str(path).replace("/lib/", "/usr/lib/", 1)):
                result = subprocess.run(["dpkg-query", "-S", candidate], text=True, capture_output=True)
                if result.returncode == 0:
                    owner = result.stdout.split(": ")[0].split(":")[0]
                    break
            if owner is None:
                raise RuntimeError("Cannot identify library package: " + str(path))
            copyright_path = Path("/usr/share/doc") / owner / "copyright"
            shutil.copy2(copyright_path, appdir / "usr/share/licenses" / (owner + ".copyright"))
            version = subprocess.check_output(
                ["dpkg-query", "-W", "-f=${Version}", owner], text=True)
            manifest["bundled"][name] = {"package": owner, "version": version,
                                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        # Include packaging/launcher sources and build provenance in the distributable.
        shutil.copy2(Path(__file__), appdir / "usr/share/jetson/package_appimage.py")
        (appdir / "usr/share/jetson/build-info.json").write_text(json.dumps(manifest, indent=2))
        squashfs = Path(temporary) / "payload.squashfs"
        subprocess.run(["mksquashfs", str(appdir), str(squashfs), "-noappend", "-all-root",
                        "-comp", "gzip", "-b", "1048576", "-processors", "4", "-no-progress"], check=True)
        with output.open("xb") as stream:
            stream.write(runtime_data)
            with squashfs.open("rb") as payload:
                shutil.copyfileobj(payload, stream)
        output.chmod(0o755)
        output.with_suffix(".build-info.json").write_text(json.dumps(manifest, indent=2))
        output.with_suffix(".sha256").write_text(
            hashlib.sha256(output.read_bytes()).hexdigest() + "  " + output.name + "\n")
    print("Created: " + str(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("binary", "probe", "assets", "runtime", "runtime-license", "output"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--build-jobs", type=int, default=2, help="Actual compiler parallelism for provenance")
    args = parser.parse_args()
    if args.build_jobs < 1:
        parser.error("--build-jobs must be positive")
    package(args.binary.resolve(), args.probe.resolve(), args.assets.resolve(),
            args.runtime.resolve(), args.runtime_sha256, args.runtime_license.resolve(),
            args.output.resolve(), args.build_jobs)


if __name__ == "__main__":
    main()
