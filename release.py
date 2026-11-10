#!/usr/bin/env python3
"""Package a reviewed installer and generate a single-command HTTPS launcher.

Run before gh release create. Writes artifacts outside the source directory.
No GitHub credentials, API calls, publication or printer operations occur here.
"""
import argparse
import ast
import hashlib
from pathlib import Path
import re
import shlex
import zipfile


def write_lf(path, text):
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def build(root, output, repository, tag):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Repository must be owner/name")
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Use a stable version tag such as v0.2.0")
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("Output directory must be outside the source tree")
    output.mkdir(parents=True, exist_ok=True)
    name = "auto-installer-" + tag.removesuffix(".0")
    # A public release contains generic code/template configuration only.
    # Machine configs can instead be passed locally with --config on the Pi.
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or path.is_symlink() or any(p in (".git", "__pycache__") for p in relative.parts):
            continue
        if path.suffix == ".pyc" or path.name in ("install.sh", "SHA256SUMS.txt"):
            continue
        files.append(path)
    archive = output / (name + ".zip")
    if archive.exists():
        raise ValueError("Refusing to overwrite an existing release ZIP")
    checksums = []
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for path in files:
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            if path.suffix == ".py":
                ast.parse(data.decode("utf-8"), filename=relative, feature_version=(3, 9))
            if path.suffix == ".sh" and b"\r" in data:
                raise ValueError("Shell files require LF line endings")
            entry = zipfile.ZipInfo(name + "/" + relative, date_time=(2026, 11, 10, 12, 0, 0))
            entry.create_system = 3
            entry.external_attr = (0o100755 if path.suffix == ".sh" else 0o100644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(entry, data)
            checksums.append(hashlib.sha256(data).hexdigest() + "  " + relative + "\n")
        zipped.writestr(name + "/SHA256SUMS.txt", "".join(checksums))
    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip() is not None:
            raise ValueError("ZIP integrity failure")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    url = "https://github.com/%s/releases/download/%s/%s" % (repository, tag, archive.name)
    bootstrap = (root / "bootstrap.py").read_text(encoding="utf-8")
    delimiter = "AUTOPRINTER_BOOTSTRAP_020"
    if delimiter in bootstrap.splitlines():
        raise ValueError("Heredoc delimiter collision")
    launcher = ("#!/bin/sh\n# Auto-generated for " + repository + " " + tag + "; payload pinned below.\n"
                "set -eu\ncommand -v python3 >/dev/null 2>&1 || { echo 'Python 3.9+ is required' >&2; exit 2; }\n"
                "exec python3 - --url " + shlex.quote(url) + " --sha256 " + digest + " \"$@\" <<'" + delimiter + "'\n" + bootstrap + "\n" + delimiter + "\n")
    write_lf(output / "install.sh", launcher)
    write_lf(output / (archive.name + ".sha256"), digest + "  " + archive.name + "\n")
    raw = "https://raw.githubusercontent.com/%s/%s/install.sh" % (repository, tag)
    command = "curl --proto '=https' --proto-redir '=https' -fsSL " + shlex.quote(raw) + " | sudo sh -s --"
    write_lf(output / "single-command.txt", command + "\n")
    return {"archive": str(archive), "sha256": digest, "command": command, "launcher": str(output / "install.sh")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", default="v0.2.0")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    import json
    print(json.dumps(build(Path(__file__).parent, args.output, args.repository, args.tag), indent=2))
