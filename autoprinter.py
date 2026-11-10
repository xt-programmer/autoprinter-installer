#!/usr/bin/env python3
"""Supervised, USB-presence-gated Klipper maintenance. Python 3.9+, Linux/systemd.

Audit is the default. No service is installed for this program and it never reboots.
See README.md before enabling apply or DFU flashing.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile

VERSION = "0.2.1"
GIB = 1024 ** 3
MIB = 1024 ** 2
REPOS = {
    "klipper": "https://github.com/Klipper3d/klipper.git",
    "moonraker": "https://github.com/Arksine/moonraker.git",
    "KlipperScreen": "https://github.com/KlipperScreen/KlipperScreen.git",
    "cartographer": "https://github.com/Cartographer3D/cartographer3d-plugin.git",
}
SERVICES = ("moonraker", "klipper", "KlipperScreen")
BASE_PACKAGES = ["git", "python3-venv", "python3-dev", "build-essential", "libffi-dev",
                 "libssl-dev", "libjpeg-dev", "zlib1g-dev", "libsodium-dev", "liblmdb-dev",
                 "libopenjp2-7", "curl", "nginx", "rfkill", "iproute2", "kbd"]
SCREEN_PACKAGES = ["xinit", "xinput", "x11-xserver-utils", "xserver-xorg-core",
                   "xserver-xorg-input-libinput", "xserver-xorg-legacy", "libcairo2-dev",
                   "pkg-config", "gir1.2-gtk-3.0", "python3-gi", "python3-gi-cairo",
                   "librsvg2-common", "libdbus-glib-1-dev", "dbus-x11"]
FLASH_PACKAGES = ["gcc-arm-none-eabi", "binutils-arm-none-eabi", "libnewlib-arm-none-eabi",
                  "libusb-1.0-0-dev", "libncurses-dev", "dfu-util"]
OPTIONAL_SERVICES = {"bluetooth", "cups", "cups-browsed", "ModemManager"}


class Refuse(RuntimeError):
    pass


class Removed(Refuse):
    pass


def read(path, default=""):
    try:
        return Path(path).read_text(errors="replace").strip().replace("\x00", "")
    except OSError:
        return default


def sha256(path, guard=None):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            if guard:
                guard.check()
            h.update(chunk)
    return h.hexdigest()


def require_digest(value, length=64):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
        raise Refuse("Expected a lowercase %s-character immutable digest, got %r" % (length, value))
    return value


def contained(root, path):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def usb_devices(root=Path("/sys/bus/usb/devices")):
    found = []
    for p in sorted(root.glob("*")):
        if not (p / "idVendor").is_file():
            continue
        found.append({"path": str(p), "port": p.name, "vid": read(p / "idVendor").lower(),
                      "pid": read(p / "idProduct").lower(), "serial": read(p / "serial"),
                      "product": read(p / "product"), "manufacturer": read(p / "manufacturer"),
                      "busnum": read(p / "busnum"), "devnum": read(p / "devnum")})
    return found


def select_usb(devices, selector):
    # A port is mandatory: a VID:PID alone is never a safe flash/trigger selector.
    if not isinstance(selector, dict) or not all(selector.get(k) for k in ("port", "vid", "pid")):
        raise Refuse("USB selector requires physical port, vid and pid; serial is strongly recommended")
    if not re.fullmatch(r"\d+-\d+(?:\.\d+)*", selector["port"]):
        raise Refuse("USB port must be a sysfs physical port such as 1-1.3")
    for k in ("vid", "pid"):
        if not re.fullmatch("[0-9a-f]{4}", selector[k]):
            raise Refuse("USB VID/PID must be four lowercase hexadecimal digits")
    matches = [d for d in devices if all(d.get(k) == v for k, v in selector.items())]
    if len(matches) != 1:
        raise Refuse("Expected exactly one USB match for %r; found %s" % (selector, len(matches)))
    return matches[0]


class USBGuard:
    """Latch removal, including unplug/replug of the same device (devnum changes)."""
    def __init__(self, device, poll=0.1):
        self.device = device
        self.poll = poll
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.reason = "USB trigger removed or re-enumerated"
        self.thread = None

    def present(self):
        p = Path(self.device["path"])
        return all(read(p / key) == self.device[field]
                   for key, field in (("idVendor", "vid"), ("idProduct", "pid"),
                                      ("serial", "serial"), ("busnum", "busnum"),
                                      ("devnum", "devnum")))

    def check(self):
        if not self.present():
            self.cancelled.set()
        if self.cancelled.is_set():
            raise Removed(self.reason)

    def start(self):
        self.check()
        def watch():
            while not self.closed.wait(self.poll):
                if not self.present():
                    self.cancelled.set()
                    return
        self.thread = threading.Thread(target=watch, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.closed.set()
        if self.thread:
            self.thread.join(timeout=1)


def kill_group(proc):
    for sig, delay in ((signal.SIGINT, 2), (signal.SIGTERM, 1), (signal.SIGKILL, 1)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            proc.poll()  # reap the direct child even if descendants are still alive
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=1)


class Runner:
    def __init__(self, guard, emit=print):
        self.guard, self.emit = guard, emit

    def run(self, args, *, cwd=None, user=None, timeout=1800, check=True, env=None):
        self.guard.check()
        args = [str(a) for a in args]
        # runuser avoids executing build systems, pip and git hooks as root.
        if user:
            args = ["runuser", "-u", user, "--"] + args
        clean_env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
                     "DEBIAN_FRONTEND": "noninteractive", "GIT_TERMINAL_PROMPT": "0",
                     "NEEDRESTART_MODE": "l", "UCF_FORCE_CONFFOLD": "1",
                     "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONNOUSERSITE": "1",
                     "HOME": "/root"}
        if env:
            clean_env.update(env)
        self.emit("$ " + shlex.join(args))
        # A temporary regular file avoids pipe deadlocks from long apt/compiler output.
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(args, cwd=cwd, env=clean_env, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            started = time.monotonic()
            position = 0
            tail = ""
            try:
                while True:
                    self.guard.check()
                    # pread avoids racing the child's shared file offset.
                    data = os.pread(output.fileno(), 65536, position)
                    if data:
                        position += len(data)
                        text = data.decode(errors="replace")
                        tail = (tail + text)[-2 * MIB:]
                        self.emit(text.rstrip())
                    if position > 128 * MIB:
                        raise Refuse("Command output exceeded 128 MiB; refusing to fill the filesystem")
                    if proc.poll() is not None and not data:
                        break
                    if time.monotonic() - started > timeout:
                        raise Refuse("Command timed out: " + shlex.join(args))
                    self.guard.cancelled.wait(0.1)
                self.guard.check()
            except BaseException:
                kill_group(proc)
                raise
            if check and proc.returncode:
                raise Refuse("Command failed (%s): %s\n%s" % (proc.returncode, shlex.join(args), tail[-4000:]))
            return proc.returncode, tail.strip()


def url_json(url, key=None):
    headers = {"User-Agent": "AutoPrinter/" + VERSION}
    if key:
        headers["X-Api-Key"] = key
    req = urllib.request.Request(url, headers=headers)
    # Do not use the host's HTTP proxy for loopback safety queries.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=3) as response:
        data = response.read(4 * MIB + 1)
    if len(data) > 4 * MIB:
        raise Refuse("API response exceeds limit")
    result = json.loads(data)
    if "error" in result:
        raise Refuse("Moonraker API returned an error")
    return result.get("result", result)


def api_base(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost", "::1") or u.path not in ("", "/") or u.query or u.fragment or u.username:
        raise Refuse("Moonraker URL must be a local http://127.0.0.1:7125 endpoint")
    return url.rstrip("/")


def printer_snapshot(base, key=None):
    base = api_base(base)
    info = url_json(base + "/printer/info", key)
    if info.get("state") != "ready":
        raise Refuse("Klipper state is not ready: " + str(info.get("state")))
    objects = url_json(base + "/printer/objects/list", key).get("objects", [])
    required = {"webhooks", "print_stats", "pause_resume", "virtual_sdcard", "idle_timeout",
                "heaters", "motion_report", "toolhead", "configfile", "mcu"}
    if not required.issubset(set(objects)):
        raise Refuse("Missing printer safety objects: " + str(sorted(required - set(objects))))
    # Query all objects, then verify EVERY configured heater, not just bed/extruder.
    query = {name: None for name in required | {x for x in objects if x.startswith("mcu ")}}
    first = url_json(base + "/printer/objects/query?" + "&".join(urllib.parse.quote(x, safe="") for x in query), key)
    status = first.get("status", {})
    heaters = status.get("heaters", {}).get("available_heaters")
    if not isinstance(heaters, list) or not heaters:
        raise Refuse("No complete configured heater inventory")
    names = sorted(set(query) | set(heaters))
    result = url_json(base + "/printer/objects/query?" + "&".join(urllib.parse.quote(x, safe="") for x in names), key)
    return result.get("status", {}), info


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def safe_idle(status, max_temp=50):
    def need(condition, message):
        if not condition:
            raise Refuse(message)
    need(status.get("webhooks", {}).get("state") == "ready", "Klipper not ready")
    need(status.get("print_stats", {}).get("state") in ("standby", "complete", "cancelled"), "Print active, paused, failed, or unknown")
    need(status.get("pause_resume", {}).get("is_paused") is False, "Printer paused or pause state unknown")
    need(status.get("virtual_sdcard", {}).get("is_active") is False, "Virtual SD active or unknown")
    need(status.get("idle_timeout", {}).get("state") in ("Idle", "Ready"), "Printer moving or activity unknown")
    need(status.get("configfile", {}).get("save_config_pending") is False, "Unsaved calibration/config changes")
    for field in ("live_velocity", "live_extruder_velocity"):
        value = status.get("motion_report", {}).get(field)
        need(finite_number(value) and abs(value) < 0.000001, "Motion active or unknown")
    heaters = status.get("heaters", {}).get("available_heaters")
    need(isinstance(heaters, list) and bool(heaters), "Heater list missing")
    for name in heaters:
        h = status.get(name, {})
        need(finite_number(h.get("target")) and h["target"] == 0, "Heater target active/unknown: " + name)
        need(finite_number(h.get("power")) and h["power"] == 0, "Heater output active/unknown: " + name)
        need(finite_number(h.get("temperature")) and 0 <= h["temperature"] <= max_temp,
             "Heater temperature unsafe/unknown: " + name)


def safe_extract_zip(archive, destination, guard, limit=200 * MIB):
    total = 0
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            guard.check()
            total += entry.file_size
            name = entry.filename
            target = destination / name
            if total > limit or "\\" in name or ".." in name.split("/") or not contained(destination, target) or Path(name).is_absolute() or ":" in name:
                raise Refuse("Unsafe/oversized zip entry: " + name)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise Refuse("Zip symlinks are not permitted")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(entry) as source, target.open("xb") as out:
                    while True:
                        guard.check()
                        chunk = source.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)


def atomic_write(path, data, guard, mode=0o600):
    guard.check()
    path = Path(path)
    if path.is_symlink():
        raise Refuse("Refusing to replace symlink: " + str(path))
    fd, temp = tempfile.mkstemp(prefix=".autoprinter-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), mode)
        guard.check()
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def check_free(paths, minimum=3 * GIB):
    for path in paths:
        p = Path(path)
        while not p.exists():
            p = p.parent
        free = shutil.disk_usage(p).free
        if free < minimum:
            raise Refuse("Insufficient free space at %s: %.1f MiB; need %.1f MiB" % (p, free / MIB, minimum / MIB))


def prune_candidates(root, days=90, now=None):
    root = Path(root)
    if root.is_symlink():
        raise Refuse("Refusing to prune a symlinked gcodes directory")
    cutoff = (time.time() if now is None else now) - days * 86400
    # Direct children only. Never traverse subdirectories, links or mounts.
    result = []
    for p in root.iterdir() if root.is_dir() else []:
        s = p.lstat()
        if stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and s.st_mtime < cutoff and p.suffix.lower() in (".gcode", ".gco", ".gc"):
            result.append(p)
    return sorted(result, key=lambda p: p.stat().st_mtime)


def inspect_host():
    os_release = {}
    for line in read("/etc/os-release").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            os_release[key] = value.strip('"')
    model = read("/proc/device-tree/model")
    compatible = read("/proc/device-tree/compatible")
    armbian = read("/etc/armbian-release")
    return {"system": platform.system(), "machine": platform.machine(), "model": model,
            "compatible": compatible, "os": os_release, "armbian": armbian,
            "free_root_mib": round(shutil.disk_usage("/").free / MIB),
            "hdmi": {str(p.parent.name): read(p) for p in Path("/sys/class/drm").glob("*-HDMI-*/status")},
            "usb": usb_devices(), "serial_by_id": [str(p) for p in Path("/dev/serial/by-id").glob("*")],
            "video": [str(p) for p in Path("/dev").glob("video*")],
            "wifi": [p.name for p in Path("/sys/class/net").glob("*") if (p / "wireless").exists()],
            "kiauh_present": Path("/home/pi/kiauh").exists(),
            "mainsailos_marker": Path("/etc/mainsailos-release").exists()}


def supported_host(host):
    model = host["model"].lower()
    allowed = ("raspberry pi 3 model b plus", "raspberry pi 3 model b+", "raspberry pi 4 model b", "raspberry pi 5 model b",
               "orange pi 3 lts", "orangepi 3 lts", "orange pi 5 pro", "orangepi 5 pro")
    if not any(model.startswith(x) for x in allowed):
        raise Refuse("Host model not in the supported list: " + host["model"])
    if host["machine"] not in ("aarch64", "armv7l", "armv8l"):
        raise Refuse("Unsupported CPU architecture")
    osinfo = host["os"]
    if osinfo.get("ID") == "debian" and osinfo.get("VERSION_ID") in ("12", "13"):
        return
    if osinfo.get("ID") == "ubuntu" and osinfo.get("VERSION_ID") in ("22.04", "24.04"):
        return
    raise Refuse("Apply supports Debian 12/13 or Ubuntu 22.04/24.04 userspace, including Armbian based on these")


class Installer:
    def __init__(self, args, config, guard):
        self.args, self.c, self.guard = args, config, guard
        self.logfile = None
        self.runner = Runner(guard, self.emit)
        self.home = Path("/home/pi")
        self.data = self.home / "printer_data"
        self.run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + str(os.getpid())
        self.state = Path("/var/lib/autoprinter") / self.run_id
        self.stage = self.home / ".autoprinter" / self.run_id
        self.report = {"version": VERSION, "run_id": self.run_id, "mode": "apply" if args.apply else "audit",
                       "status": "NOT_READY", "changes": [], "blockers": [], "warnings": [], "moves": []}
        self.report["options"] = {"flashing_capability": args.enable_flashing_capability,
                                  "wifi": "enabled" if self.wifi_enabled() else "disabled"}
        self.stopped = False
        self.before = {}
        self.snapshot = None

    def emit(self, text):
        # Limit escape sequences/control characters from device names and logs.
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", str(text))
        print(text, flush=True)
        if self.logfile and not self.guard.cancelled.is_set():
            self.logfile.write(text + "\n")
            self.logfile.flush()
        if self.args.apply and self.c.get("console_progress", True) and self.stopped:
            with contextlib.suppress(OSError):
                fd = os.open("/dev/tty1", os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
                try:
                    os.write(fd, (text[-2000:] + "\r\n").encode())
                finally:
                    os.close(fd)

    def command(self, *args, **kwargs):
        return self.runner.run(args, **kwargs)

    def change(self, text):
        self.report["changes"].append(text)
        self.emit("CHANGE: " + text)
        self.save_report()

    def save_report(self):
        if self.logfile and not self.guard.cancelled.is_set():
            atomic_write(self.state / "report.json", json.dumps(self.report, indent=2), self.guard)

    def api_key(self):
        return os.environ.get("AUTOPRINTER_MOONRAKER_KEY")

    def idle_check(self):
        status, info = printer_snapshot(self.c.get("moonraker_url", "http://127.0.0.1:7125"), self.api_key())
        self.snapshot = status
        self.report["klipper"] = info
        self.report["mcu"] = {k: v for k, v in status.items() if k == "mcu" or k.startswith("mcu ")}
        safe_idle(status)
        return status

    def systemd_state(self, service):
        _, out = self.command("systemctl", "show", service + ".service", "--property=LoadState,ActiveState,UnitFileState,FragmentPath,ExecStart", check=False, timeout=15)
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def inventory(self):
        self.guard.check()
        host = inspect_host()
        self.report["host"] = host
        self.emit(json.dumps(host, indent=2))
        self.before = {s: self.systemd_state(s) for s in SERVICES}
        self.report["services_before"] = self.before
        _, errors = self.command("journalctl", "-p", "err", "-b", "-n", "40", "--no-pager", check=False, timeout=15)
        self.report["linux_errors"] = errors
        self.report["klipper_log_errors"] = []
        log = self.data / "logs/klippy.log"
        if log.is_file():
            with log.open("rb") as f:
                f.seek(max(0, log.stat().st_size - 128 * 1024))
                lines = f.read().decode(errors="replace").splitlines()
            self.report["klipper_log_errors"] = [x for x in lines if re.search("error|shutdown|traceback|mcu.*disconnect", x, re.I)][-40:]
            self.emit("Recent Klipper error matches (may be historical):\n" + "\n".join(self.report["klipper_log_errors"]))
        _, processes = self.command("ps", "-eo", "pid,user,comm,%cpu,%mem", "--sort=-%cpu", check=False, timeout=15)
        self.report["processes"] = processes
        _, packages = self.command("dpkg", "--audit", check=False, timeout=15)
        self.report["dpkg_audit"] = packages
        try:
            self.idle_check()
            self.report["idle_check"] = "passed"
        except (Refuse, OSError, ValueError) as e:
            self.report["idle_check"] = str(e)
        required = ["printer.cfg", "mainsail.cfg"] + self.c.get("required_config", ["macros500", "settings500"])
        cart = self.c.get("cartographer", {})
        cart_present = False
        if cart.get("usb"):
            try:
                select_usb(host["usb"], cart["usb"])
                cart_present = True
            except Refuse:
                pass
        if cart_present:
            required.append("cartographer.cfg")
        self.report["cartographer_present"] = cart_present
        self.report["required_files"] = {name: (self.data / "config" / name).is_file() for name in required}
        self.emit("Required files: " + json.dumps(self.report["required_files"]))
        return host

    def ethernet_check(self):
        _, raw = self.command("ip", "-j", "route", "get", "1.1.1.1", timeout=15)
        routes = json.loads(raw)
        if len(routes) != 1:
            raise Refuse("No unique IPv4 internet route")
        interface = routes[0].get("dev", "")
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", interface):
            raise Refuse("Invalid network interface")
        p = Path("/sys/class/net") / interface
        if (p / "wireless").exists() or not (p / "device").exists() or read(p / "type") != "1" or read(p / "carrier") != "1":
            raise Refuse("Default internet route is not through a connected physical Ethernet interface")
        # Force the actual internet probe onto that Ethernet interface.
        self.command("curl", "--interface", interface, "--proto", "=https", "--tlsv1.2", "--fail", "--silent", "--show-error",
                     "--connect-timeout", "8", "--max-time", "20", "--output", "/dev/null", "https://github.com", timeout=25)
        self.report["ethernet"] = interface

    def validate_layout(self):
        import pwd
        account = pwd.getpwnam("pi")
        if account.pw_dir != str(self.home) or account.pw_uid == 0:
            raise Refuse("Expected non-root pi user with home /home/pi")
        for p in (self.home, self.data, self.data / "config", self.home / ".autoprinter", Path("/var/lib/autoprinter")):
            if p.is_symlink():
                raise Refuse("Custom/symlinked layout requires manual integration: " + str(p))
        # No speculative edits to KIAUH multi-instance installations.
        _, units = self.command("systemctl", "list-unit-files", "--no-legend", "--no-pager", timeout=15)
        for line in units.splitlines():
            name = line.split()[0] if line.split() else ""
            if re.match(r"(klipper|moonraker)[-@].*\.service$", name):
                raise Refuse("Multi-instance/host-MCU service detected: " + name)
        if any(s.get("ActiveState") in ("activating", "deactivating", "reloading") for s in self.before.values()):
            raise Refuse("Service transition in progress")
        existing = any(s.get("LoadState") == "loaded" for s in self.before.values())
        if existing and not self.c.get("adopt_standard_layout"):
            raise Refuse("Set adopt_standard_layout only after reviewing standard single-printer paths and units")
        if existing:
            for service in ("klipper", "moonraker"):
                if self.before[service].get("LoadState") != "loaded":
                    continue
                _, unit = self.command("systemctl", "cat", service, timeout=15)
                if "User=pi" not in unit or str(self.home) not in unit:
                    raise Refuse("Unrecognized service layout: " + service)
            # Unit files are preserved on adoption; only the standard source/venv paths change.
            _, klipper_unit = self.command("systemctl", "cat", "klipper", check=False, timeout=15)
            if self.before["klipper"].get("LoadState") == "loaded":
                expected = ["/home/pi/klipper", "/home/pi/klippy-env", "/home/pi/printer_data"]
                if not all(x in klipper_unit for x in expected):
                    raise Refuse("Klipper service is not the supported standard layout")
            _, moon_unit = self.command("systemctl", "cat", "moonraker", check=False, timeout=15)
            if self.before["moonraker"].get("LoadState") == "loaded":
                # Modern Moonraker frequently puts args in its env file.
                env_path = self.data / "systemd/moonraker.env"
                combined = moon_unit + "\n" + read(env_path)
                if not all(x in combined for x in ("/home/pi/moonraker", "/home/pi/moonraker-env", "/home/pi/printer_data")):
                    raise Refuse("Moonraker service/env is not the supported standard layout")
            if self.before["KlipperScreen"].get("LoadState") == "loaded":
                _, screen_unit = self.command("systemctl", "cat", "KlipperScreen", timeout=15)
                if not all(x in screen_unit for x in ("User=pi", "/home/pi/KlipperScreen", "/home/pi/.KlipperScreen-env")):
                    raise Refuse("KlipperScreen uses a custom source/environment path")
        self.report["existing_installation"] = existing
        # Detect alternative processes not owned by the approved service.
        for process in Path("/proc").glob("[0-9]*/cmdline"):
            raw = read(process)
            if any(x in raw for x in ("klippy.py", "moonraker.py", "moonraker.components")):
                if str(self.home) not in raw:
                    raise Refuse("Nonstandard printer process: " + str(process.parent))

    def validate_manifest(self):
        if self.c.get("schema") != 1:
            raise Refuse("Unknown manifest schema")
        for key in ("adopt_standard_layout", "allow_package_install", "console_progress", "disable_wifi"):
            if type(self.c.get(key)) is not bool:
                raise Refuse(key + " must be a JSON boolean")
        required = self.c.get("required_config", [])
        if not isinstance(required, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", x) for x in required):
            raise Refuse("required_config must be an array of simple filenames")
        for name in ("klipper", "moonraker", "KlipperScreen"):
            require_digest(self.c.get("revisions", {}).get(name), 40)
        disabled = self.c.get("disable_services", [])
        if not isinstance(disabled, list) or not set(disabled).issubset(OPTIONAL_SERVICES):
            raise Refuse("disable_services may only contain " + str(sorted(OPTIONAL_SERVICES)))
        if not self.c.get("allow_package_install"):
            raise Refuse("Set allow_package_install after reviewing host package provisioning")
        if self.report["dpkg_audit"]:
            raise Refuse("dpkg is interrupted/broken; supervised package recovery required")
        m = self.c.get("mainsail", {})
        require_digest(m.get("sha256"))
        u = urllib.parse.urlsplit(m.get("url", ""))
        if u.scheme != "https" or u.netloc != "github.com" or not u.path.startswith("/mainsail-crew/mainsail/releases/download/") or not u.path.endswith("/mainsail.zip"):
            raise Refuse("Mainsail asset must be an official immutable versioned GitHub release URL")
        if "/latest/" in u.path:
            raise Refuse("Pin a versioned Mainsail asset")
        if self.report["cartographer_present"] and self.c.get("cartographer", {}).get("install_plugin"):
            require_digest(self.c.get("revisions", {}).get("cartographer"), 40)
            for path in (self.data / "config").rglob("*.cfg"):
                if "[scanner]" in read(path):
                    raise Refuse("Legacy Cartographer scanner config requires a supervised migration")
        self.validate_configs()
        if self.before["KlipperScreen"].get("LoadState") != "loaded":
            _, display = self.command("systemctl", "is-active", "display-manager", check=False, timeout=15)
            if display == "active":
                raise Refuse("Active desktop display manager; integrate KlipperScreen manually")
        if self.args.flash and not self.args.enable_flashing_capability:
            raise Refuse("--flash additionally requires --enable-flashing-capability")
        if self.args.flash:
            self.flash_profile()  # Validate before stopping any services or installing packages.

    def bundle_file(self, relative, digest):
        require_digest(digest)
        if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
            raise Refuse("Invalid relative bundle filename")
        root = Path(self.args.config).resolve().parent
        path = root / relative
        if not contained(root, path) or not path.is_file() or path.is_symlink() or sha256(path, self.guard) != digest:
            raise Refuse("Missing, unsafe or checksum-mismatched bundle file: " + str(relative))
        return path

    def validate_configs(self):
        supplied = self.c.get("config_files", {})
        if not isinstance(supplied, dict):
            raise Refuse("config_files must be an object")
        for name, spec in supplied.items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith("."):
                raise Refuse("Config destination must be a simple filename")
            self.bundle_file(spec["file"], spec["sha256"])
            target = self.data / "config" / name
            if target.exists() and sha256(target) != spec["sha256"]:
                raise Refuse("Existing config differs; merge it manually before apply: " + name)
        for name in self.report["required_files"]:
            if not (self.data / "config" / name).is_file() and name not in supplied:
                raise Refuse("Required machine config not supplied: " + name)

    def flash_profile(self):
        p = self.c.get("octopus", {})
        if not p.get("verified_by") or not p.get("recovery_instructions"):
            raise Refuse("Flashing requires an operator-verified Octopus profile and recovery instructions")
        if p.get("board") not in ("octopus-1.0", "octopus-1.1", "octopus-pro-1.0", "octopus-pro-1.1"):
            raise Refuse("Specify exact board revision")
        chip = p.get("mcu")
        if chip not in ("stm32f446xx", "stm32f429xx", "stm32h723xx"):
            raise Refuse("V0.1 DFU supports explicitly verified STM32F446/F429/H723 profiles only")
        device = select_usb(usb_devices(), p.get("dfu_usb"))
        if (device["vid"], device["pid"]) != ("0483", "df11") or not device["serial"]:
            raise Refuse("Requires unique STM32 ROM DFU serial and physical port; Katapult is unsupported")
        if not p["dfu_usb"].get("serial") or device["port"] == self.guard.device["port"]:
            raise Refuse("DFU target must have explicit serial and be separate from trigger")
        config = self.bundle_file(p.get("build_config", ""), p.get("build_config_sha256"))
        text = config.read_text()
        boot = p.get("bootloader_kib")
        if boot not in (32, 64, 128):
            raise Refuse("No zero-offset or unverified bootloader profiles")
        required = ['CONFIG_MCU="' + chip + '"', "CONFIG_MACH_STM32=y", "CONFIG_USBSERIAL=y",
                    "CONFIG_STM32_FLASH_START_%d=y" % (boot * 1024)]
        for value in required:
            if value not in text.splitlines():
                raise Refuse("MCU .config missing expected setting: " + value)
        clock = p.get("clock_config")
        if not isinstance(clock, str) or not re.fullmatch(r"CONFIG_STM32_CLOCK_REF_[A-Z0-9_]+=y", clock) or clock not in text.splitlines():
            raise Refuse("Require exact verified clock_config setting from .config")
        address = 0x08000000 + boot * 1024
        if p.get("flash_address") != "0x%08x" % address:
            raise Refuse("Flash address does not match bootloader offset")
        if not p.get("normal_usb", {}).get("serial"):
            raise Refuse("Expected post-flash normal USB serial required")
        if p["normal_usb"].get("port") != device["port"]:
            raise Refuse("Normal/DFU selectors must reference the same physical USB port")
        return p, device, config

    def setup_state(self):
        self.guard.check()
        self.state.mkdir(parents=True, mode=0o700)
        self.logfile = (self.state / "run.log").open("a", buffering=1)
        self.stage.mkdir(parents=True, mode=0o755)
        self.command("chown", "pi:pi", self.stage)
        self.save_report()

    def backup(self):
        self.guard.check()
        sources = [self.data / "config", self.data / "systemd", self.data / "database", self.home / "mainsail-config", Path("/etc/nginx"), Path("/etc/X11/Xwrapper.config")]
        sources += [Path("/etc/systemd/system") / (s + ".service") for s in SERVICES]
        sources += [Path("/etc/systemd/system") / (s + ".service.d") for s in SERVICES]
        total = 0
        # Reject configs with links outside the data tree: tar keeps symlinks as links.
        for source in sources:
            for p in [source] + (list(source.rglob("*")) if source.is_dir() and not source.is_symlink() else []):
                self.guard.check()
                if p.is_symlink() and contained(self.data / "config", p.parent) and not any(contained(root, p) for root in (self.data / "config", self.home / "mainsail-config")):
                    raise Refuse("Config symlink outside config tree requires manual integration: " + str(p))
                if p.is_file():
                    total += p.stat().st_size
        check_free([self.state], 3 * GIB + total)
        temp = self.state / "backup.tar.partial"
        class GuardedInput:
            def __init__(self, source, guard):
                self.source, self.guard = source, guard
            def read(self, size):
                self.guard.check()
                return self.source.read(size)
        with tarfile.open(temp, "w", dereference=False) as tar:
            for source in sources:
                if source.exists() or source.is_symlink():
                    for p in [source] + (list(source.rglob("*")) if source.is_dir() and not source.is_symlink() else []):
                        self.guard.check()
                        info = tar.gettarinfo(str(p), arcname=str(p).lstrip("/"))
                        if info.isreg():
                            with p.open("rb") as source_file:
                                tar.addfile(info, GuardedInput(source_file, self.guard))
                        else:
                            tar.addfile(info)
        self.guard.check()
        with temp.open("rb") as f:
            os.fsync(f.fileno())
        temp.rename(self.state / "backup.tar")
        self.report["backup_sha256"] = sha256(self.state / "backup.tar", self.guard)
        atomic_write(self.state / "manifest.json", json.dumps(self.c, indent=2), self.guard)
        self.change("Saved stopped-stack config/database/unit/web/display backup (not MCU flash contents)")

    def stop_stack(self, recovery=False):
        if not recovery:
            self.idle_check()
        for service in SERVICES:
            if self.before[service].get("LoadState") == "loaded":
                self.command("systemctl", "stop", service, timeout=45)
                state = self.systemd_state(service)
                if state.get("ActiveState") not in ("inactive", "failed"):
                    raise Refuse("Failed to stop " + service)
        # Detect any other Klipper host processes before altering files/firmware.
        for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
            if "klippy.py" in read(cmdline):
                raise Refuse("Klipper process remains active; refusing changes")
        self.stopped = True
        if self.c.get("console_progress", True) and shutil.which("chvt") and Path("/dev/tty1").exists():
            self.command("chvt", "1", check=False, timeout=5)
        self.change("Stopped approved printer services for supervised maintenance")

    def install_packages(self, host):
        if not self.c.get("allow_package_install"):
            raise Refuse("allow_package_install must be true to provision host dependencies")
        if self.report["dpkg_audit"]:
            raise Refuse("dpkg reports an interrupted/broken package state; supervised package recovery required")
        packages = BASE_PACKAGES + SCREEN_PACKAGES
        if host["os"].get("ID") == "debian" and host["os"].get("VERSION_ID") == "13":
            packages += ["libgirepository-2.0-dev"]
        else:
            packages += ["libgirepository1.0-dev"]
        if self.args.enable_flashing_capability:
            packages += FLASH_PACKAGES
        check_free(["/", "/var", self.home])
        self.command("apt-get", "-o", "DPkg::Lock::Timeout=20", "update", timeout=600)
        # No dist-upgrade, autoremove, purge, forced lock removal or blind dpkg repair.
        self.command("apt-get", "-s", "--no-remove", "install", *packages, timeout=120)
        check_free(["/", "/var", self.home])
        self.command("apt-get", "-o", "DPkg::Lock::Timeout=20", "-o", "Dpkg::Options::=--force-confold",
                     "--yes", "--no-remove", "--no-install-recommends", "install", *packages, timeout=2400)
        self.change("Installed/updated required host packages; OS release/kernel upgrade not performed")

    def checkout(self, name):
        revision = require_digest(self.c["revisions"][name], 40)
        target = self.stage / name
        self.command("git", "-c", "core.hooksPath=/dev/null", "clone", "--no-checkout", "--filter=blob:none", REPOS[name], target, user="pi", timeout=600)
        self.command("git", "-C", target, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", revision, user="pi", timeout=600)
        _, actual = self.command("git", "-C", target, "rev-parse", "HEAD", user="pi", timeout=15)
        if actual != revision:
            raise Refuse("Revision verification failed: " + name)
        self.report.setdefault("revisions", {})[name] = actual
        return target

    def venv(self, directory, requirements, system=False):
        self.command("python3", "-m", "venv", *( ["--system-site-packages"] if system else []), directory, user="pi", timeout=180)
        self.command(directory / "bin/python", "-m", "pip", "install", "-r", requirements, user="pi", cwd=requirements.parent, timeout=1800)
        self.command(directory / "bin/python", "-m", "pip", "check", user="pi", timeout=60)

    def stage_stack(self):
        paths = {}
        for name, venv, req in (("klipper", "klippy-env", "scripts/klippy-requirements.txt"),
                                ("moonraker", "moonraker-env", "scripts/moonraker-requirements.txt"),
                                ("KlipperScreen", ".KlipperScreen-env", "scripts/KlipperScreen-requirements.txt")):
            check_free([self.home, "/var"])
            source = self.checkout(name)
            envdir = self.stage / venv
            self.venv(envdir, source / req, system=name == "KlipperScreen")
            paths[name] = source
            paths[venv] = envdir
        if self.report["cartographer_present"] and self.c.get("cartographer", {}).get("install_plugin"):
            source = self.checkout("cartographer")
            self.command(paths["klippy-env"] / "bin/python", "-m", "pip", "install", source, user="pi", timeout=1200)
            self.command(paths["klippy-env"] / "bin/python", "-m", "pip", "install", "numpy>=1.16", user="pi", timeout=1200)
            self.command(paths["klippy-env"] / "bin/python", "-m", "pip", "check", user="pi", timeout=60)
            extras = paths["klipper"] / "klippy/plugins"
            if not extras.is_dir():
                extras = paths["klipper"] / "klippy/extras"
            atomic_write(extras / "cartographer.py", "from cartographer.extra import *\n", self.guard, 0o644)
            self.command("chown", "pi:pi", extras / "cartographer.py")
            paths["cartographer"] = source
        self.download_mainsail()
        self.change("Staged pinned host sources and new Python environments; old files retained")
        return paths

    def download_mainsail(self):
        m = self.c["mainsail"]
        archive = self.stage / "mainsail.zip"
        self.command("curl", "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2", "--fail", "--location",
                     "--max-time", "300", "--max-filesize", str(100 * MIB), "--output", archive, m["url"], user="pi", timeout=310)
        if sha256(archive, self.guard) != m["sha256"]:
            raise Refuse("Mainsail checksum mismatch")
        dest = self.stage / "mainsail"
        dest.mkdir()
        safe_extract_zip(archive, dest, self.guard)
        if not (dest / "index.html").is_file():
            raise Refuse("Mainsail zip has no root index.html")
        self.command("chown", "-R", "pi:pi", dest)

    def flash(self, klipper):
        if not self.args.enable_flashing_capability or not self.args.flash:
            raise Refuse("Firmware write requires both --enable-flashing-capability and --flash")
        p, device, config = self.flash_profile()
        build_config = klipper / ".config"
        atomic_write(build_config, config.read_bytes(), self.guard, 0o644)
        self.command("chown", "pi:pi", build_config)
        self.command("make", "olddefconfig", cwd=klipper, user="pi", timeout=120)
        # Kconfig normalization must not silently change the critical profile values.
        normalized = read(build_config).splitlines()
        original = config.read_text().splitlines()
        critical = [x for x in original if x.startswith(("CONFIG_MCU=", "CONFIG_STM32_FLASH_START_", "CONFIG_STM32_CLOCK_REF_", "CONFIG_USBSERIAL=")) and not x.startswith("#")]
        if not all(x in normalized for x in critical):
            raise Refuse("Kconfig changed a critical MCU profile setting")
        self.command("make", "-j2", cwd=klipper, user="pi", timeout=1200)
        firmware = klipper / "out/klipper.bin"
        if not firmware.is_file() or not 1024 < firmware.stat().st_size < 2 * MIB:
            raise Refuse("Invalid firmware image")
        # Read target identity and alternate memory descriptors; never use VID:PID alone.
        _, listing = self.command("dfu-util", "--list", timeout=15)
        serial = device["serial"]
        selected = [x for x in listing.splitlines() if ('serial="' + serial + '"') in x and "alt=0" in x]
        if len(selected) != 1 or "Internal Flash" not in selected[0]:
            raise Refuse("DFU internal-flash alternate not uniquely verified")
        self.flash_profile()  # Re-match physical port/serial immediately before write.
        atomic_write(self.state / "FIRMWARE_WRITE_STARTED", json.dumps({"profile": p, "sha256": sha256(firmware)}, indent=2), self.guard)
        self.report["firmware_write_started"] = True
        self.save_report()
        # No mass erase. This operation is inherently non-transactional.
        self.command("dfu-util", "--device", "0483:df11", "--serial", serial, "--alt", "0",
                     "--dfuse-address", p["flash_address"] + ":leave", "--download", firmware, timeout=180)
        self.change("Wrote Octopus firmware from pinned Klipper source; awaiting normal enumeration")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            self.guard.check()
            try:
                select_usb(usb_devices(), p["normal_usb"])
                return
            except Refuse:
                self.guard.cancelled.wait(0.2)
        raise Refuse("MCU did not enumerate normally; check BOOT jumper/reset and recovery instructions")

    def activate_path(self, destination, source):
        self.guard.check()
        destination = Path(destination)
        # Persist an intent before changing paths so power loss leaves a recovery trail.
        retained = destination.with_name(destination.name + ".before-" + self.run_id)
        move = {"destination": str(destination), "retained": str(retained), "new_source": str(source), "done": False}
        self.report["moves"].append(move)
        self.save_report()
        if destination.exists() or destination.is_symlink():
            if retained.exists() or retained.is_symlink():
                raise Refuse("Retention path already exists")
            destination.rename(retained)
        self.guard.check()
        destination.symlink_to(source, target_is_directory=True)
        move["done"] = True
        self.save_report()

    def user_directory(self, path):
        self.guard.check()
        path.mkdir(parents=True, exist_ok=True)
        self.command("chown", "pi:pi", path)

    def unit(self, name, content):
        if self.before.get(name, {}).get("LoadState") == "loaded":
            return  # Preserve adopted units and their options/drop-ins.
        path = Path("/etc/systemd/system") / (name + ".service")
        if path.exists() or path.is_symlink():
            raise Refuse("Unexpected existing unit: " + str(path))
        atomic_write(path, content, self.guard, 0o644)
        self.command("systemctl", "enable", name, timeout=30)
        self.change("Installed service " + name)

    def activate_stack(self, paths):
        for sub in ("config", "logs", "comms", "gcodes", "systemd"):
            self.user_directory(self.data / sub)
        for name, spec in self.c.get("config_files", {}).items():
            destination = self.data / "config" / name
            if not destination.exists():
                source = self.bundle_file(spec["file"], spec["sha256"])
                atomic_write(destination, source.read_bytes(), self.guard, 0o644)
                self.command("chown", "pi:pi", destination)
                self.change("Installed missing operator-supplied config " + name)
        for name, path in paths.items():
            self.activate_path(self.home / name, path)
        self.activate_path(self.home / "mainsail", self.stage / "mainsail")
        common = "\nRestart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n"
        self.unit("klipper", "[Unit]\nDescription=Klipper host\nAfter=network.target\n[Service]\nType=simple\nUser=pi\nSupplementaryGroups=dialout\nExecStart=/home/pi/klippy-env/bin/python /home/pi/klipper/klippy/klippy.py /home/pi/printer_data/config/printer.cfg -l /home/pi/printer_data/logs/klippy.log -a /home/pi/printer_data/comms/klippy.sock" + common)
        # Using the source module preserves a conventional source-based Moonraker layout.
        self.unit("moonraker", "[Unit]\nDescription=Moonraker\nAfter=network.target klipper.service\n[Service]\nType=simple\nUser=pi\nWorkingDirectory=/home/pi/moonraker\nExecStart=/home/pi/moonraker-env/bin/python -m moonraker -d /home/pi/printer_data" + common)
        if not (self.data / "config/moonraker.conf").exists():
            atomic_write(self.data / "config/moonraker.conf", "[server]\nhost: 0.0.0.0\nport: 7125\nklippy_uds_address: /home/pi/printer_data/comms/klippy.sock\n\n[authorization]\ntrusted_clients:\n  127.0.0.1\n  ::1\n\n[file_manager]\nenable_object_processing: True\n\n[history]\n\n[announcements]\n", self.guard, 0o644)
            self.command("chown", "pi:pi", self.data / "config/moonraker.conf")
            self.change("Installed minimal Moonraker configuration; remote clients require authorization")
        if self.before["KlipperScreen"].get("LoadState") != "loaded":
            # No display-manager/network-manager takeover, boot configuration changes or reboot.
            _, active = self.command("systemctl", "is-active", "display-manager", check=False, timeout=15)
            if active == "active":
                raise Refuse("Active desktop display manager; integrate KlipperScreen manually")
            wrapper = Path("/etc/X11/Xwrapper.config")
            if wrapper.exists():
                settings = [line.strip() for line in read(wrapper).splitlines() if line.strip() and not line.lstrip().startswith("#")]
                standard = {"allowed_users=console", "allowed_users=anybody", "needs_root_rights=auto", "needs_root_rights=yes", "needs_root_rights=no"}
                if not set(settings).issubset(standard):
                    raise Refuse("Existing custom Xwrapper.config; integrate manually")
            wrapper.parent.mkdir(exist_ok=True)
            atomic_write(wrapper, "allowed_users=anybody\nneeds_root_rights=yes\n", self.guard, 0o644)
            self.command("groupadd", "--force", "klipperscreen")
            self.command("usermod", "-a", "-G", "tty,video,render,klipperscreen", "pi")
            screen = "[Unit]\nDescription=KlipperScreen\nAfter=systemd-user-sessions.service systemd-logind.service moonraker.service\nWants=dbus.socket\nConditionPathExists=/dev/tty0\n[Service]\nType=simple\nUser=pi\nSupplementaryGroups=klipperscreen\nWorkingDirectory=/home/pi/KlipperScreen\nEnvironment=\"KS_XCLIENT=/home/pi/.KlipperScreen-env/bin/python /home/pi/KlipperScreen/screen.py\" BACKEND=X\nExecStart=/home/pi/KlipperScreen/scripts/KlipperScreen-start.sh\nPAMName=login\nTTYPath=/dev/tty7\nTTYReset=yes\nTTYVHangup=yes\nTTYVTDisallocate=yes\n"
            self.unit("KlipperScreen", screen + common)
        self.install_nginx()
        self.command("systemctl", "daemon-reload", timeout=30)
        self.change("Activated staged host files; retained prior source trees and environments")

    def install_nginx(self):
        # A separate port avoids replacing an existing MainsailOS/other site's configuration.
        port = self.c.get("mainsail_port", 8080)
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535 or port == 7125:
            raise Refuse("mainsail_port must be an unprivileged port other than Moonraker's")
        target = Path("/etc/nginx/conf.d/autoprinter-mainsail.conf")
        content = ("# Managed by AutoPrinter; prior version is in backup.tar\nserver {\n listen " + str(port) + ";\n server_name _;\n root /home/pi/mainsail;\n index index.html;\n client_max_body_size 0;\n location / { try_files $uri $uri/ /index.html; }\n location /websocket {\n  proxy_pass http://127.0.0.1:7125/websocket;\n  proxy_http_version 1.1;\n  proxy_set_header Upgrade $http_upgrade;\n  proxy_set_header Connection \"upgrade\";\n  proxy_read_timeout 86400;\n }\n location ~ ^/(printer|api|access|machine|server)(/|$) {\n  proxy_pass http://127.0.0.1:7125;\n  proxy_set_header Host $host;\n  proxy_set_header X-Real-IP $remote_addr;\n  proxy_set_header X-Forwarded-For $remote_addr;\n }\n}\n")
        if target.exists() and not read(target).startswith("# Managed by AutoPrinter"):
            raise Refuse("Existing unrecognized nginx file")
        # Every path to the assets must be world-traversable, without changing home permissions.
        for p in (self.home, self.home / ".autoprinter", self.stage):
            if p.stat().st_mode & 0o001 == 0:
                raise Refuse("nginx cannot traverse " + str(p) + "; adjust explicitly or use an existing web server")
        atomic_write(target, content, self.guard, 0o644)
        self.command("nginx", "-t", timeout=15)
        self.command("systemctl", "enable", "nginx", timeout=30)
        self.command("systemctl", "reload-or-restart", "nginx", timeout=30)
        self.change("Installed isolated Mainsail web endpoint on port " + str(port))

    def wifi_enabled(self):
        return self.args.enable_wifi or self.c.get("disable_wifi", True) is False

    def configure_wifi(self):
        self.ethernet_check()
        enabled = self.wifi_enabled()
        self.command("rfkill", "unblock" if enabled else "block", "wifi", timeout=15)
        if enabled and shutil.which("nmcli") and self.systemd_state("NetworkManager").get("ActiveState") == "active":
            self.command("nmcli", "radio", "wifi", "on", timeout=15)
        found = False
        for p in Path("/sys/class/net").glob("*"):
            if (p / "wireless").exists():
                found = True
                self.command("ip", "link", "set", "dev", p.name, "up" if enabled else "down", timeout=15)
        if enabled and not found:
            self.report["warnings"].append("Wi-Fi requested but no wireless interface detected; no SSID/profile was configured")
        self.change("Enabled Wi-Fi radio/interfaces using existing profiles" if enabled else "Blocked Wi-Fi radios and brought wireless interfaces down")

    def verify_wifi(self, devices, interfaces):
        enabled = self.wifi_enabled()
        radios = [d for d in devices if d.get("type") in ("wlan", "wifi")]
        for d in radios:
            soft = str(d.get("soft", "")).lower()
            hard = str(d.get("hard", "")).lower()
            if enabled:
                if soft not in ("unblocked", "false", "0") or hard in ("blocked", "true", "1"):
                    raise Refuse("Requested Wi-Fi radio is still blocked")
            elif soft not in ("blocked", "true", "1"):
                raise Refuse("Wi-Fi radio remains unblocked")
        for interface in interfaces:
            if enabled and interface.get("admin_up") is not True:
                raise Refuse("Requested Wi-Fi interface is not administratively up")
            if not enabled and interface["operstate"] not in ("down", "notpresent"):
                raise Refuse("Wi-Fi interface remains enabled")
        if enabled and not interfaces:
            self.report["warnings"].append("Wi-Fi enabled policy selected, but no wireless interface is available")
        self.report["wifi"] = {"policy": "enabled" if enabled else "disabled", "interfaces": interfaces,
                               "radios": radios, "association_required": False}

    def trim_services(self):
        for service in self.c.get("disable_services", []):
            self.guard.check()
            state = self.systemd_state(service)
            self.report.setdefault("optional_services_before", {})[service] = state
            self.save_report()
            if state.get("LoadState") == "loaded":
                self.command("systemctl", "disable", "--now", service, timeout=45)
                self.change("Disabled explicitly selected optional service " + service)

    def ready_led(self):
        led = self.c.get("ready_led")
        if not led:
            return
        name, section = led.get("name", ""), led.get("section", "")
        if not led.get("verified_by") or not re.fullmatch(r"[A-Za-z0-9_]+", name) or section not in ("neopixel", "dotstar"):
            raise Refuse("LED indication requires a verified existing LED section/name")
        settings = self.snapshot.get("configfile", {}).get("settings", {})
        configured = settings.get(section + " " + name, {})
        if not configured or any(k.lower() == "gcode_macro set_led" for k in settings):
            raise Refuse("Verified LED absent or SET_LED overridden by a macro")
        pin = configured.get("pin") if section != "dotstar" else configured.get("data_pin")
        if not pin or pin != led.get("pin"):
            raise Refuse("LED pin does not match the verified profile")
        self.guard.check()
        url = api_base(self.c.get("moonraker_url", "http://127.0.0.1:7125")) + "/printer/gcode/script"
        command = "SET_LED LED=%s RED=0 GREEN=1 BLUE=0 WHITE=0 TRANSMIT=1 SYNC=0" % name
        headers = {"Content-Type": "application/json"}
        if self.api_key():
            headers["X-Api-Key"] = self.api_key()
        request = urllib.request.Request(url, data=json.dumps({"script": command}).encode(), headers=headers, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=3) as response:
            result = json.loads(response.read(MIB))
        if "error" in result:
            raise Refuse("LED command failed")
        self.guard.check()
        self.change("Set verified status LED green")

    def verify_ready(self):
        check_free(["/", "/var", self.home])
        self.ethernet_check()
        for service in ("klipper", "moonraker", "KlipperScreen"):
            if self.systemd_state(service).get("ActiveState") != "active":
                raise Refuse("Required service is not active: " + service)
        deadline = time.monotonic() + 90
        last = ""
        while time.monotonic() < deadline:
            self.guard.check()
            try:
                status = self.idle_check()
                if status.get("configfile", {}).get("warnings"):
                    raise Refuse("Klipper reports configuration warnings")
                if not status.get("mcu", {}).get("mcu_version"):
                    raise Refuse("Main MCU has not reported its firmware version")
                normal = self.c.get("octopus", {}).get("normal_usb")
                if not normal:
                    raise Refuse("Set Octopus normal_usb selector before claiming readiness")
                select_usb(usb_devices(), normal)
                chip = self.c.get("octopus", {}).get("mcu")
                reported_chip = status.get("mcu", {}).get("mcu_constants", {}).get("MCU")
                if chip and chip != reported_chip:
                    raise Refuse("Main MCU chip does not match the verified profile")
                serial = status.get("configfile", {}).get("settings", {}).get("mcu", {}).get("serial")
                if not serial or not serial.startswith("/dev/"):
                    raise Refuse("Main MCU must use an explicit USB serial device")
                tty = Path(serial).resolve().name
                usb_ancestor = None
                for ancestor in (Path("/sys/class/tty") / tty / "device").resolve().parents:
                    if (ancestor / "idVendor").is_file():
                        usb_ancestor = ancestor.name
                        break
                if usb_ancestor != normal["port"]:
                    raise Refuse("Configured main MCU serial is not on the verified Octopus USB port")
                if self.report["cartographer_present"] and "mcu cartographer" not in status:
                    raise Refuse("Detected Cartographer is not connected as mcu cartographer")
                # A successful ready handshake validates protocol compatibility, not calibration.
                break
            except (Refuse, OSError, ValueError) as e:
                last = str(e)
                self.guard.cancelled.wait(2)
        else:
            raise Refuse("Printer did not become safely ready: " + last)
        connected = [p for p in Path("/sys/class/drm").glob("*-HDMI-*/status") if read(p) == "connected"]
        if len(connected) != 1:
            raise Refuse("Expected exactly one connected HDMI output; cannot verify display")
        if not self.args.display_confirmed:
            raise Refuse("HDMI/GUI requires supervised visual verification; rerun with --display-confirmed after checking it")
        if not self.args.machine_config_confirmed:
            raise Refuse("Operator must validate machine-specific config/calibration; use --machine-config-confirmed")
        _, radios = self.command("rfkill", "--json", timeout=15)
        devices = json.loads(radios).get("rfkilldevices", [])
        interfaces = []
        for p in Path("/sys/class/net").glob("*"):
            if (p / "wireless").exists():
                flags = read(p / "flags")
                admin_up = bool(int(flags, 16) & 1) if re.fullmatch(r"0x[0-9a-fA-F]+", flags) else None
                interfaces.append({"name": p.name, "operstate": read(p / "operstate"), "admin_up": admin_up})
        self.verify_wifi(devices, interfaces)
        missing = [name for name in self.report["required_files"] if not (self.data / "config" / name).is_file()]
        if missing:
            raise Refuse("Required files missing: " + str(missing))
        self.command("curl", "--fail", "--silent", "--show-error", "--max-time", "5", "--output", "/dev/null",
                     "http://127.0.0.1:%d/" % self.c.get("mainsail_port", 8080), timeout=10)
        self.report["status"] = "SOFTWARE_READY"
        self.report["warnings"].append("No motion/heating/endstop test performed. SOFTWARE_READY is not a physical safety certification.")

    def apply(self, host):
        if os.geteuid() != 0:
            raise Refuse("Apply requires sudo/root")
        if not self.args.maintenance_confirmed:
            raise Refuse("Apply requires --maintenance-confirmed: supervised machine, external job sources prevented, actuators safely isolated")
        supported_host(host)
        self.validate_layout()
        self.validate_manifest()
        if self.args.allow_prune:
            if self.args.recovery_isolated:
                raise Refuse("Pruning is disabled during offline/recovery installation")
            self.prune()
        check_free(["/", "/var", self.home])
        self.ethernet_check()
        recovery = self.args.recovery_isolated
        if recovery:
            if self.snapshot is not None:
                safe_idle(self.snapshot)  # An isolation assertion never overrides known unsafe live state.
            if not self.args.flash and self.report["existing_installation"]:
                raise Refuse("Recovery override for an existing installation requires a DFU flash operation")
            if not self.args.actuator_power_disconnected:
                raise Refuse("Recovery/fresh install requires --actuator-power-disconnected; USB logic power only during maintenance")
        else:
            self.idle_check()
            self.guard.cancelled.wait(2)
            self.idle_check()
        # No file/package changes occurred before all of the above checks.
        self.setup_state()
        self.stop_stack(recovery=recovery)
        self.backup()
        self.install_packages(host)
        paths = self.stage_stack()
        if self.args.flash:
            self.flash(paths["klipper"])
        self.activate_stack(paths)
        self.configure_wifi()
        self.trim_services()
        if recovery:
            self.report["status"] = "INSTALLED_NEEDS_POWER_AND_VALIDATION"
            self.emit("Installation complete with actuator power isolated. Restore power under supervision, then run --verify. Services remain stopped.")
            return
        if not self.args.display_confirmed or not self.args.machine_config_confirmed:
            self.report["status"] = "INSTALLED_NEEDS_VALIDATION"
            self.emit("Host installed. Printer services remain stopped. Supervise startup, validate the display/machine config, then use --verify with confirmation flags.")
            return
        self.command("systemctl", "start", "klipper", "moonraker", timeout=60)
        self.command("systemctl", "start", "KlipperScreen", timeout=45)
        self.verify_ready()
        self.ready_led()
        self.return_display()

    def quiesce_on_failure(self):
        if not self.stopped:
            return
        for service in SERVICES:
            self.guard.check()
            if self.systemd_state(service).get("LoadState") == "loaded":
                self.command("systemctl", "stop", service, timeout=45)
        self.emit("Printer services stopped after maintenance failure. Review report before restarting.")

    def return_display(self):
        if self.stopped and shutil.which("chvt"):
            self.command("chvt", "7", check=False, timeout=5)

    def verify(self):
        if os.geteuid() != 0 or not self.args.maintenance_confirmed:
            raise Refuse("--verify requires root and --maintenance-confirmed")
        # Verification deliberately does not start printer services or issue any G-code.
        self.verify_ready()

    def prune(self):
        if not self.args.apply or not self.args.allow_prune or not self.args.maintenance_confirmed:
            raise Refuse("Pruning needs --apply --allow-prune --maintenance-confirmed")
        if os.geteuid() != 0:
            raise Refuse("Pruning requires root")
        # A broken/offline printer is never an excuse to erase jobs blindly.
        self.idle_check()
        days = self.c.get("prune_older_days", 90)
        if not isinstance(days, int) or days < 30:
            raise Refuse("Retain at least 30 days of G-code")
        root = self.data / "gcodes"
        for p in prune_candidates(root, days):
            self.guard.check()
            if shutil.disk_usage(root).free >= 500 * MIB:
                break
            # Recheck immediately before every deletion; don't delete renamed/replaced links.
            self.idle_check()
            if p in prune_candidates(root, days):
                self.guard.check()
                p.unlink()
                self.report["changes"].append("Permanently deleted old G-code with explicit consent: " + str(p))
                self.emit(self.report["changes"][-1])
        check_free(["/", "/var", self.home])


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--config", default=str(Path(__file__).with_name("manifest.json")))
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true", help="perform supervised host maintenance")
    modes.add_argument("--verify", action="store_true", help="read-only post-install readiness checks")
    p.add_argument("--inventory", action="store_true", help="print Linux hardware inventory only; no USB selector needed")
    p.add_argument("--usb-port", help="override trigger physical port; keeps configured VID/PID/serial")
    p.add_argument("--usb-vid", help="override trigger USB vendor ID")
    p.add_argument("--usb-pid", help="override trigger USB product ID")
    p.add_argument("--usb-serial", help="override trigger serial, including an explicit empty string")
    p.add_argument("--enable-flashing-capability", action="store_true", help="permit guarded flashing and install STM32 build/DFU tools during apply; does not flash")
    p.add_argument("--enable-wifi", action="store_true", help="enable Wi-Fi radio/interfaces during apply using existing profiles; Ethernet remains required")
    p.add_argument("--maintenance-confirmed", action="store_true")
    p.add_argument("--recovery-isolated", action="store_true", help="offline/fresh/DFU recovery; leaves printer services stopped")
    p.add_argument("--actuator-power-disconnected", action="store_true")
    p.add_argument("--flash", action="store_true", help="flash verified Octopus already in STM32 ROM DFU")
    p.add_argument("--allow-prune", action="store_true", help="irreversibly delete old G-code only below 500 MiB")
    p.add_argument("--display-confirmed", action="store_true")
    p.add_argument("--machine-config-confirmed", action="store_true")
    p.add_argument("--report", help="save report to this explicit path (even in audit)")
    return p.parse_args(argv)


def main(argv=None):
    args = arguments(argv)
    if platform.system() != "Linux":
        print("REFUSED: run on the target Linux Pi; this program does not modify Windows.", file=sys.stderr)
        return 2
    if args.inventory:
        if args.apply or args.verify or args.flash or args.allow_prune:
            print("REFUSED: --inventory cannot be combined with mutations/verification", file=sys.stderr)
            return 2
        print(json.dumps(inspect_host(), indent=2))
        return 0
    if (args.flash or args.allow_prune or args.recovery_isolated) and not args.apply:
        print("REFUSED: flash, prune and recovery require --apply", file=sys.stderr)
        return 2
    if args.flash and not args.enable_flashing_capability:
        print("REFUSED: --flash requires --enable-flashing-capability", file=sys.stderr)
        return 2
    guard = None
    installer = None
    lock = None
    try:
        config = json.loads(Path(args.config).read_text())
        if not isinstance(config, dict):
            raise Refuse("Manifest must be a JSON object")
        selector = dict(config.get("trigger_usb", {}))
        for field in ("port", "vid", "pid", "serial"):
            value = getattr(args, "usb_" + field)
            if value is not None:
                selector[field] = value
        trigger = select_usb(usb_devices(), selector)
        inherited_session = os.environ.get("AUTOPRINTER_TRIGGER_SESSION")
        if inherited_session:
            expected = json.loads(inherited_session)
            if any(trigger.get(k) != v for k, v in expected.items()):
                raise Removed("USB trigger changed between download and installer startup")
        guard = USBGuard(trigger).start()
        def interrupted(signum, frame):
            guard.reason = "Interrupted by signal %s" % signum
            guard.cancelled.set()
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        if args.apply or args.verify:
            import fcntl
            # flock is released by process exit, never erase somebody else's lock.
            lock_fd = os.open("/run/autoprinter.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            lock = os.fdopen(lock_fd, "a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refuse("Another AutoPrinter session holds the lock")
        installer = Installer(args, config, guard)
        host = installer.inventory()
        if args.apply:
            installer.apply(host)
        elif args.verify:
            installer.verify()
        else:
            installer.report["status"] = "AUDIT_ONLY"
            installer.emit("AUDIT ONLY: printer/services/configuration were not modified. Review the inventory and configure immutable revisions and board profiles.")
        installer.save_report()
        guard.check()
        installer.emit("RESULT: " + installer.report["status"])
        installer.emit("OK TO REMOVE USB: this maintenance process has finished. This message alone does not mean the printer is ready.")
        return 0 if installer.report["status"] in ("SOFTWARE_READY", "AUDIT_ONLY") else 3
    except Removed as e:
        if installer:
            installer.report["status"] = "ABORTED_USB_OR_SIGNAL"
            installer.report["blockers"].append(str(e))
            installer.emit("ABORTED: " + str(e) + ". No restart, rollback, reboot or further maintenance will be attempted. Inspect package/firmware state before reuse.")
        else:
            print("ABORTED: " + str(e), file=sys.stderr)
        return 130
    except (Refuse, OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile, subprocess.SubprocessError) as e:
        if installer:
            installer.report["status"] = "NOT_READY"
            installer.report["blockers"].append(str(e))
            installer.emit("NOT READY: " + str(e))
            if args.apply and installer.stopped:
                with contextlib.suppress(Refuse, OSError):
                    installer.quiesce_on_failure()
            installer.emit("Retained paths and backup location are in the report. Supervised recovery is required.")
            with contextlib.suppress(Refuse, OSError):
                installer.save_report()
        else:
            print("REFUSED: " + str(e), file=sys.stderr)
        return 2
    finally:
        if installer:
            if args.report:
                # On USB removal, honour 'no more work': do not write an optional report.
                if guard and not guard.cancelled.is_set():
                    with contextlib.suppress(OSError, Refuse):
                        atomic_write(Path(args.report).resolve(), json.dumps(installer.report, indent=2), guard)
            if installer.logfile:
                installer.logfile.close()
        if guard:
            guard.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    sys.exit(main())
