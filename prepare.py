#!/usr/bin/env python3
"""Resolve official upstream revisions on a preparation computer, not on a printer.

This selects latest sources, not a certified compatible release set. Review and bench
test the resulting manifest before applying it to a printer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.request

PROJECTS = {"klipper": ("Klipper3d/klipper", "master"),
            "moonraker": ("Arksine/moonraker", "master"),
            "KlipperScreen": ("KlipperScreen/KlipperScreen", "master"),
            "cartographer": ("Cartographer3D/cartographer3d-plugin", "main")}


def request(url):
    return urllib.request.Request(url, headers={"User-Agent": "AutoPrinter-prepare/0.2", "Accept": "application/vnd.github+json"})


def api(path):
    with urllib.request.urlopen(request("https://api.github.com/" + path), timeout=20) as response:
        data = response.read(8 * 1024 * 1024)
    return json.loads(data)


def prepare(config):
    for name, (repo, branch) in PROJECTS.items():
        commit = api("repos/%s/commits/%s" % (repo, branch))["sha"]
        if not re.fullmatch("[0-9a-f]{40}", commit):
            raise ValueError("Invalid commit response")
        config.setdefault("revisions", {})[name] = commit
        print("Pinned %s: %s" % (name, commit))
    release = api("repos/mainsail-crew/mainsail/releases/latest")
    assets = [a for a in release["assets"] if a["name"] == "mainsail.zip"]
    if len(assets) != 1:
        raise ValueError("No unique official mainsail.zip release asset")
    asset = assets[0]
    url = asset["browser_download_url"]
    if not url.startswith("https://github.com/mainsail-crew/mainsail/releases/download/"):
        raise ValueError("Unexpected release asset URL")
    if asset["size"] > 100 * 1024 * 1024:
        raise ValueError("Release asset too large")
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request(url), timeout=30) as response:
        for chunk in iter(lambda: response.read(65536), b""):
            total += len(chunk)
            if total > 100 * 1024 * 1024:
                raise ValueError("Release asset too large")
            digest.update(chunk)
    if total != asset["size"]:
        raise ValueError("Release asset size mismatch")
    expected = asset.get("digest")
    if expected and expected != "sha256:" + digest.hexdigest():
        raise ValueError("GitHub asset digest mismatch")
    config["mainsail"] = {"url": url, "sha256": digest.hexdigest()}
    print("Pinned Mainsail %s: %s" % (release["tag_name"], digest.hexdigest()))
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("manifest.json")))
    parser.add_argument("--output", default="manifest.prepared.json")
    parser.add_argument("--resolve-latest", action="store_true", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        parser.error("Output already exists; use a new filename to preserve the earlier manifest")
    config = prepare(json.loads(Path(args.config).read_text()))
    fd, temp = tempfile.mkstemp(dir=output.parent, prefix=".prepared-")
    try:
        with os.fdopen(fd, "w", newline="\n") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, output)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    print("Saved " + str(output))
    print("Still required: trigger identity, verified board/config files, layout/package approval and bench validation. No printer was modified.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        sys.exit("Preparation failed: " + str(error))
