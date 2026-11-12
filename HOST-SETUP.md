# Development prototype: fresh host setup

This older prototype is retained for development history. It uses port 8080
and staged source paths. The live bench was subsequently normalized using
official KIAUH templates to real native directories and Mainsail on port 80.
Use `peripherals.py` for accessories on that existing installation. Do not
use this prototype as the documented KIAUH installation path.

`host_setup.py` is a separate fresh-host path for the inspected OrangePi 3 LTS
aarch64 running Armbian/Ubuntu 26.04. It is not a production bypass for the normal
USB-gated printer maintenance mode. It prepares Klipper, Moonraker and Mainsail
without configuring or contacting an MCU.

It refuses an existing printing service, source/environment paths, printer
configuration or nginx service, broken package state, unsupported bench image,
missing explicit opt-ins and insufficient free space. Run it from a supervised
SSH shell as `pi`, with an inspected local manifest containing exact Klipper /
Moonraker source commits and the official versioned Mainsail ZIP URL/SHA-256:

```sh
sudo python3 host_setup.py --config /path/to/host-manifest.json \
  --ssh-session-pid "$$" --supervised-host-setup \
  --experimental-ubuntu-26-04 --allow-wifi-transport
```

The controlling SSH shell's UID/environment and process start time are checked.
Session termination or a signal cancels the current command group at its next
checkpoint. Blocking kernel I/O and TCP disconnect detection are not hard
real-time; this mode provides no USB-removal guarantee because it is explicitly
an SSH host-preparation path. No startup rule or unattended resume is installed.

Behavior:

- Install only the host build/Python/web dependencies; no whole-system upgrade,
  kernel update, MCU build/flash tooling, KlipperScreen, radio toggle or network
  manager installation. APT can update shared runtime dependencies as needed.
- Stage exact upstream source revisions and separate Python environments, run
  dependency checks and command-line import checks, and verify the Mainsail ZIP.
- Preserve existing user-home permissions. Static Mainsail assets are served
  from a new versioned public directory under `/var/www/autoprinter-mainsail`.
- Create a standard Klipper unit **disabled and stopped**, conditional on the
  future presence of an operator-supplied `printer.cfg`. No fabricated printer
  config, heater/motor pins, calibrations, MCU reset or G-code is involved.
- Start Moonraker and nginx, with Mainsail on port **8080**. Moonraker trusts
  loopback and the specific controlling SSH client's address; no broad subnet
  trust or new Wi-Fi credentials are created.
- Save the manifest, checksummed pre-change backup, log and change ledger under
  `/var/lib/autoprinter/<run-id>/`. An SSH failure or interrupted APT operation
  needs inspection before rerunning. A failed source staging attempt is retained
  under `/home/pi/.autoprinter/<run-id>/`.

A successful result is `HOST_INSTALLED_NOT_PRINTER_READY`. That verifies the web
host/API and the intentionally inactive Klipper service; it does not verify
printer firmware, motion, heating or display hardware. The next stage needs the
real machine configuration and a detected/identified MCU.
