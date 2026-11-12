#!/usr/bin/env python3
"""Read-only USB detection and persistent positive evidence. No installers or MCU I/O.

`record` is suitable for a short udev callback; it only writes local observations.
The maintenance installer consumes the history on its next explicit invocation.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile
import time

SCHEMA = 1
DEFAULT_STATE = Path("/var/lib/autoprinter/device-history.json")
CARTOGRAPHER = re.compile(r"\bcartographer(?:3d)?(?:\b|_)", re.I)


def read(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


def usb_parent(path):
    node = Path(path).resolve()
    return next((p for p in (node, *node.parents) if (p / "idVendor").is_file()), None)


def current(usb_root=Path("/sys/bus/usb/devices"), video_root=Path("/sys/class/video4linux"),
            by_id=Path("/dev/v4l/by-id"), serial_root=Path("/dev/serial/by-id")):
    devices, webcams, probes = [], [], []
    for p in sorted(usb_root.glob("*")):
        if not (p / "idVendor").is_file() or p.name.startswith("usb"):
            continue
        d = {"port": p.name, "vid": read(p / "idVendor"), "pid": read(p / "idProduct"),
             "serial": read(p / "serial"), "manufacturer": read(p / "manufacturer"), "product": read(p / "product")}
        devices.append(d)
        if any(read(i / "bInterfaceClass").lower() == "0e" for i in p.glob(p.name + ":*")):
            webcams.append(dict(d, evidence="USB video interface class 0e", nodes=[]))
        if CARTOGRAPHER.search(d["manufacturer"] + " " + d["product"]):
            probes.append(dict(d, evidence="Named Cartographer USB manufacturer/product"))
    for video in sorted(video_root.glob("*")):
        parent = usb_parent(video / "device")
        if not parent:
            continue  # SoC video decoders are not USB webcams.
        for camera in webcams:
            if camera["port"] != parent.name or read(video / "index") not in ("0", ""):
                continue  # Exclude metadata/secondary nodes, never assume /dev/video0.
            node = "/dev/" + video.name
            stable = [str(p) for p in by_id.glob("*-video-index0") if p.resolve().name == video.name]
            camera["nodes"].append({"device": stable[0] if stable else node, "node": node, "name": read(video / "name")})
    for serial in serial_root.glob("*"):
        if CARTOGRAPHER.search(serial.name) and not any(d.get("serial_path") == str(serial) for d in probes):
            probes.append({"serial_path": str(serial), "evidence": "Named Cartographer serial-by-id device"})
    return {"usb": devices, "usb_webcam": webcams, "cartographer": probes}


def kernel_evidence(text):
    result = {"usb_webcam": [], "cartographer": []}
    for line in text.splitlines():
        if re.search(r"Found\s+UVC\s+.+?\s+device\b", line, re.I):
            result["usb_webcam"].append({"evidence": line[-400:], "source": "kernel journal"})
        if re.search(r"\busb\s+[0-9.-]+:\s*(?:Product|Manufacturer):", line, re.I) and CARTOGRAPHER.search(line):
            result["cartographer"].append({"evidence": line[-400:], "source": "kernel journal"})
    return result


def load(path=DEFAULT_STATE):
    if not path.exists():
        return {"schema": SCHEMA, "devices": {}}
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Unsafe or oversized peripheral history")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("devices"), dict):
        raise ValueError("Invalid peripheral history schema; preserve and inspect the file")
    for kind, entry in data['devices'].items():
        if kind not in ('usb_webcam', 'cartographer') or not isinstance(entry, dict) or not isinstance(entry.get('seen'), bool) or not isinstance(entry.get('observations'), list):
            raise ValueError('Invalid peripheral history entry; preserve and inspect the file')
    return data


def merge(state, snapshot, historic=None, now=None):
    if state.get("schema") != SCHEMA:
        raise ValueError("Unknown peripheral history schema")
    now = now or dt.datetime.now(dt.timezone.utc).isoformat()
    result = json.loads(json.dumps(state))
    for kind in ("usb_webcam", "cartographer"):
        evidence = list(snapshot.get(kind, [])) + list((historic or {}).get(kind, []))
        if not evidence:
            continue  # Positive detections latch; unplugging never forgets a device.
        entry = result["devices"].setdefault(kind, {"seen": True, "first_seen": now, "observations": []})
        entry.update(seen=True, last_seen=now)
        known = {json.dumps(x, sort_keys=True) for x in entry["observations"]}
        for observation in evidence:
            key = json.dumps(observation, sort_keys=True)
            if key not in known:
                entry["observations"].append(observation)
                known.add(key)
        entry["observations"] = entry["observations"][-64:]
    result["last_scan"] = now
    return result


def wanted(state, snapshot):
    return {kind: bool(snapshot.get(kind)) or state.get("devices", {}).get(kind, {}).get("seen") is True
            for kind in ("usb_webcam", "cartographer")}


def record(snapshot=None, historic=None, path=DEFAULT_STATE):
    import fcntl
    path = Path(path)
    if path.parent.is_symlink():
        raise ValueError("History directory cannot be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        deadline = time.monotonic() + 1
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError("Peripheral history is busy")
                time.sleep(.01)
        data = merge(load(path), snapshot if snapshot is not None else current(), historic)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".history-")
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
        return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("scan", "record"), default="scan", nargs="?")
    parser.add_argument("--event", help="udev DEVPATH; sysfs device events only")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    if args.event:
        target = (Path("/sys") / args.event.lstrip("/")).resolve()
        try:
            target.relative_to("/sys/devices")
        except ValueError:
            parser.error("udev event must identify a device below /sys/devices")
    snapshot = current()
    state = record(snapshot, path=args.state) if args.mode == "record" else load(args.state)
    if not args.event:
        print(json.dumps({"current": snapshot, "history": state, "wanted": wanted(state, snapshot)}, indent=2))


if __name__ == "__main__":
    main()
