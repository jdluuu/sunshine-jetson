#!/usr/bin/env python3
"""Check a packaged AppImage's application list on a JetPack 5 Xorg desktop."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET


def free_port():
    for base in range(53989, 62000, 1000):
        sockets = []
        try:
            for kind, offsets in ((socket.SOCK_STREAM, (-5, 0, 1, 21)),
                                  (socket.SOCK_DGRAM, (9, 10, 11))):
                for offset in offsets:
                    sock = socket.socket(socket.AF_INET, kind)
                    sockets.append(sock)
                    sock.bind(("0.0.0.0", base + offset))
            return base
        except OSError:
            pass
        finally:
            for sock in sockets:
                sock.close()
    raise RuntimeError("No free test port range")


def stop(process):
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def check_case(image, root, env, context, cert, mode, apps, expected):
    config = root / mode
    config.mkdir(mode=0o700)
    path = config / "apps.json"
    original = None
    if apps is not None:
        original = (json.dumps(apps, ensure_ascii=False) + "\r\n").encode()
        path.write_bytes(original)
    state = {"root": {"uniqueid": str(uuid.uuid4()), "devices": [{
        "uniqueid": "appimage-smoke", "certs": [cert.read_text()],
    }]}}
    state_path = config / "sunshine_state.json"
    state_path.write_text(json.dumps(state))
    port = free_port()
    log_path = config / "console.log"
    args = [str(image)]
    if mode == "legacy-extract":
        args.append("--appimage-extract-and-run")
    args.extend(["--config-dir", str(config), "--port", str(port)])
    with log_path.open("w") as log:
        process = subprocess.Popen(args, env=env, cwd=root, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        for _ in range(150):
            text = log_path.read_text(errors="replace")
            if process.poll() is not None:
                raise RuntimeError("AppImage exited:\n" + text[-3000:])
            if "Found encoder jetson: [nvv4l2h264enc]" in text and "Configuration UI available" in text:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Startup timeout:\n" + text[-3000:])
        assert "No such node" not in text, text
        url = "https://127.0.0.1:" + str(port - 5) + "/applist?uniqueid=appimage-smoke"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
        for attempt in range(20):
            try:
                with opener.open(url, timeout=3) as response:
                    tree = ET.fromstring(response.read())
                break
            except OSError:
                if attempt == 19:
                    raise
                time.sleep(0.1)
        assert tree.attrib.get("status_code") == "200", ET.tostring(tree)
        names = [app.findtext("AppTitle") for app in tree.findall("App")]
        assert names == expected, names
        updated = json.loads(path.read_text())
        assert "env" in updated, updated
        backups = list(config.glob("apps.json.before-env-fix-*.bak"))
        if apps is not None and "env" not in apps:
            assert updated == dict(apps, env={})
            assert len(backups) == 1 and backups[0].read_bytes() == original
        elif apps is not None:
            assert path.read_bytes() == original
            assert not backups
        else:
            assert not backups
        assert json.loads(state_path.read_text())["root"] == state["root"]
        return {"mode": mode, "applist_status": 200, "apps": names,
                "hardware_encoder": "nvv4l2h264enc", "pairing_preserved": True}
    finally:
        stop(process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    image = args.image.resolve(strict=True)
    env = os.environ.copy()
    if not env.get("DISPLAY"):
        raise RuntimeError("Run in an Xorg desktop environment")
    with tempfile.TemporaryDirectory(prefix="sunshine appimage regression ") as temporary:
        root = Path(temporary)
        portable = root / "Renamed Sunshine NX.AppImage"
        shutil.copy2(image, portable)
        cert, key = root / "client.pem", root / "client.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-days", "1", "-subj", "/CN=AppImage-Smoke-Test",
                        "-keyout", str(key), "-out", str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key.chmod(0o600)
        context = ssl._create_unverified_context()
        context.load_cert_chain(cert, key)
        cases = [
            ("fresh", None, ["Desktop"]),
            ("legacy-extract", {"apps": [
                {"name": "Desktop", "image-path": "desktop.png"},
                {"name": "My custom app", "cmd": "true"},
            ]}, ["Desktop", "My custom app"]),
            ("existing", {"env": {"CUSTOM_TEST": "retained"}, "apps": [
                {"name": "My desktop", "image-path": "desktop.png"},
            ]}, ["My desktop"]),
        ]
        results = []
        for mode, apps, expected in cases:
            result = check_case(portable, root, env, context, cert, mode, apps, expected)
            results.append(result)
            print("PASS", json.dumps(result), flush=True)
    evidence = {"image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "fresh_and_legacy_applist": results,
                "renamed_image_in_directory_with_spaces": True,
                "fuse_and_extract_modes": True,
                "moonlight_stream_test": "not performed by this application-list test"}
    args.output.write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
