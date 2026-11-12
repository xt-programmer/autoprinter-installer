#!/bin/sh
# Auto-generated for xt-programmer/autoprinter-installer v0.4.0; payload pinned below.
set -eu
command -v python3 >/dev/null 2>&1 || { echo 'Python 3.9+ is required' >&2; exit 2; }
exec python3 - --url https://github.com/xt-programmer/autoprinter-installer/releases/download/v0.4.0/auto-installer-v0.4.zip --sha256 3a929b45896566151e1d1b2c3b903d0b2640cf3ecb5b05a902b0f40956c6255a "$@" <<'AUTOPRINTER_BOOTSTRAP_020'
#!/usr/bin/env python3
"""Download and run a SHA-256-pinned AutoPrinter release.

No arguments: hardware inventory only. Maintenance requires an explicit USB port.
The generated install.sh embeds this file, so no local bootstrap is required.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile


class HTTPSRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise RuntimeError("Refusing non-HTTPS redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def usb_identity(port, vid=None, pid=None, serial=None, root=Path("/sys/bus/usb/devices")):
    if not re.fullmatch(r"\d+-\d+(?:\.\d+)*", port):
        raise RuntimeError("Invalid USB physical port; use a port from --inventory")
    device = root / port
    def value(key):
        try:
            return (device / key).read_text().strip()
        except OSError:
            return ""
    identity = {key: value(key) for key in ("idVendor", "idProduct", "serial", "busnum", "devnum")}
    if not all(re.fullmatch("[0-9a-f]{4}", identity[k]) for k in ("idVendor", "idProduct")) or not identity["busnum"] or not identity["devnum"]:
        raise RuntimeError("Selected USB trigger port is not present")
    for expected, key in ((vid, "idVendor"), (pid, "idProduct"), (serial, "serial")):
        if expected is not None and expected != identity[key]:
            raise RuntimeError("Selected USB trigger identity does not match " + key)
    return device, identity


class Presence:
    def __init__(self, device, expected):
        self.device, self.expected = device, expected
        self.removed, self.closed = threading.Event(), threading.Event()
        self.thread = None

    def check(self):
        def value(key):
            try:
                return (self.device / key).read_text().strip()
            except OSError:
                return ""
        if self.removed.is_set() or any(value(k) != v for k, v in self.expected.items()):
            self.removed.set()
            raise RuntimeError("USB trigger removed/re-enumerated; aborted")

    def start(self):
        self.check()
        def watch():
            while not self.closed.wait(.1):
                try:
                    self.check()
                except RuntimeError:
                    return
        self.thread = threading.Thread(target=watch, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=1)


def unpack(archive, dest, check):
    dest.mkdir()
    total = 0
    with zipfile.ZipFile(archive) as zipped:
        for entry in zipped.infolist():
            check()
            total += entry.file_size
            name = entry.filename
            path = dest / name
            if total > 100 * 1024 * 1024 or "\\" in name or ".." in name.split("/") or ":" in name or Path(name).is_absolute() or stat.S_ISLNK(entry.external_attr >> 16):
                raise RuntimeError("Unsafe zip entry")
            try:
                path.resolve().relative_to(dest.resolve())
            except ValueError:
                raise RuntimeError("Zip path traversal")
            if entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(entry) as source, path.open("xb") as output:
                while True:
                    check()
                    chunk = source.read(65536)
                    if not chunk:
                        break
                    output.write(chunk)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--peripherals", action="store_true", help="existing KIAUH accessory plan/setup; no MCU operations")
    parser.add_argument("--configs", action="store_true", help="only versioned macros/settings plan/setup; no services/packages/MCU changes")
    parser.add_argument("--usb-port", help="explicit physical trigger port from inventory")
    parser.add_argument("--usb-vid", help="optional expected vendor ID")
    parser.add_argument("--usb-pid", help="optional expected product ID")
    parser.add_argument("--usb-serial", help="optional expected serial; use an empty string if absent")
    args, forwarded = parser.parse_known_args(argv)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    if any(x.split("=", 1)[0] in ("--usb-port", "--usb-vid", "--usb-pid", "--usb-serial", "--url", "--sha256") for x in forwarded):
        parser.error("Do not repeat download/USB options after the -- separator")
    if sys.platform != "linux":
        parser.error("Run this launcher on the Linux Pi")
    if not re.fullmatch("[0-9a-f]{64}", args.sha256):
        parser.error("Expected exact trusted SHA-256 checksum")
    if urllib.parse.urlsplit(args.url).scheme != "https":
        parser.error("Only HTTPS download URLs are supported")
    if args.peripherals and args.configs: parser.error('Choose --peripherals or --configs, not both')
    special_mode=args.peripherals or args.configs
    if not forwarded and not special_mode:
        forwarded = ["--inventory"] if not args.usb_port else []
    peripheral_audit = special_mode and '--apply' not in forwarded
    peripheral_ssh = special_mode and "--supervised-ssh" in forwarded
    if not args.usb_port and not (peripheral_audit or peripheral_ssh) and (forwarded != ["--inventory"] or any(x is not None for x in (args.usb_vid, args.usb_pid, args.usb_serial))):
        parser.error("Maintenance/audit needs --usb-port. First run with --inventory to choose the trigger port")
    if "--inventory" in forwarded and any(x in forwarded for x in ("--apply", "--verify", "--flash", "--allow-prune", "--recovery-isolated")):
        parser.error("Inventory cannot be combined with maintenance")
    presence = None
    identity = None
    if args.usb_port:
        device, identity = usb_identity(args.usb_port, args.usb_vid, args.usb_pid, args.usb_serial)
        presence = Presence(device, identity).start()
    check = presence.check if presence else lambda: None
    try:
        if shutil.disk_usage(tempfile.gettempdir()).free < 100 * 1024 * 1024:
            raise RuntimeError("Less than 100 MiB temporary space; use a locally provisioned copy")
        work = Path(tempfile.mkdtemp(prefix="autoprinter-download-"))
        print("Download workspace: " + str(work), flush=True)
        archive = work / "bundle.zip"
        digest = hashlib.sha256()
        total = 0
        started = time.monotonic()
        opener = urllib.request.build_opener(HTTPSRedirects())
        with opener.open(args.url, timeout=3) as response, archive.open("xb") as output:
            if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise RuntimeError("Refusing non-HTTPS redirect")
            while True:
                check()
                chunk = response.read(65536)
                check()
                if not chunk:
                    break
                total += len(chunk)
                if total > 40 * 1024 * 1024 or time.monotonic() - started > 300:
                    raise RuntimeError("Bundle download too large or timed out")
                output.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != args.sha256:
            raise RuntimeError("Bundle checksum mismatch; nothing executed")
        dest = work / "bundle"
        unpack(archive, dest, check)
        entrypoint = "config_sync.py" if args.configs else "peripherals.py" if args.peripherals else "autoprinter.py"
        scripts = list(dest.glob("*/"+entrypoint)) + list(dest.glob(entrypoint))
        if len(scripts) != 1:
            raise RuntimeError("Bundle must have one AutoPrinter entry point at root or one directory deep")
        command = [sys.executable, str(scripts[0])]
        if identity:
            session = {"port": args.usb_port, "vid": identity["idVendor"], "pid": identity["idProduct"],
                       "serial": identity["serial"], "busnum": identity["busnum"], "devnum": identity["devnum"]}
            os.environ["AUTOPRINTER_TRIGGER_SESSION"] = json.dumps(session)
            for field in ("port", "vid", "pid", "serial"):
                command += ["--usb-" + field, session[field]]
        command += forwarded
        check()
        os.execv(sys.executable, command)
    finally:
        if presence:
            presence.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile) as error:
        sys.exit("BOOTSTRAP REFUSED: " + str(error))

AUTOPRINTER_BOOTSTRAP_020
