#!/bin/sh
# Run as `sudo sh /path/to/auto-installer-v0.2.1/run.sh [arguments]`.
# No installation, sudo password entry or background daemon in this launcher.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v python3 >/dev/null 2>&1 || {
    echo 'Python 3 is required; provision it before connecting the maintenance trigger.' >&2
    exit 2
}
exec python3 "$SCRIPT_DIR/autoprinter.py" "$@"
