# AutoPrinter v0.2 — supervised Pi / Klipper maintenance

First implementation from the supplied `info.txt` and `activity.txt`. This is a
bench-test release. It has **not been run on a physical Pi or printer**. The
included manifest deliberately blocks installation/flashing until the missing
machine settings, configuration files and release pins have been supplied.

Download and run inventory directly on the Linux Pi:

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.2.0/install.sh | sudo sh -s --
```

Use `--usb-port YOUR_PORT` for a guarded audit, and add
`--enable-flashing-capability` / `--enable-wifi` as desired. Installation needs
`--apply --maintenance-confirmed --config /path/to/machine.json`. Actual firmware
writing additionally needs `--flash` and the verified board profile. See the
single-command section below for complete examples.

## What is included

| File | Purpose |
| --- | --- |
| `autoprinter.py` | Linux inventory, audit, install/update/repair and readiness checks |
| `manifest.json` | Operator-reviewed machine settings; intentionally incomplete |
| `prepare.py` | Resolve current official upstream commits and hash a Mainsail release on a preparation computer |
| `bootstrap.py` | Embedded HTTPS + SHA-256 downloader used by the one-command launcher |
| `release.py` | Generate ZIP, pinned install.sh, checksum and single-command text |
| `run.sh` | Local USB-storage launcher |
| `profiles/` | Place your verified printer files and MCU build config here |
| `tests/test_safety.py` | Automated safety-policy tests; no printer required |
| `REQUIREMENTS.md` | Coverage of every item in your two input files |
| `RECOVERY.md` | Failure and manual recovery instructions |
| `SOURCES.md` | Official upstream references used for implementation |

## Operating limits

Supported apply path: a single printer, user `pi`, home `/home/pi`,
`~/printer_data`, standard Klipper/Moonraker/KlipperScreen source and environment
paths, systemd and APT. Existing source trees/environments are retained next to
their old paths; replacement environments remain at their original staging
paths so their absolute Python entry points remain valid.

The host allowlist covers Raspberry Pi 3B+, 4B and 5 and Orange Pi 3 LTS and 5 Pro,
with Debian 12/13 or Ubuntu 22.04/24.04 userspace, including corresponding Armbian
images, and Python 3.9+. This is a policy allowlist, **not a tested hardware/OS
compatibility matrix**. Other layouts, multiple instances and older OS releases
need integration work. No distro upgrade, kernel/bootloader update, partition
repair, power cycling or automatic reboot occurs.

The Pi must already have `python3`, `curl`, `ip`, `ps`, `runuser`, APT, systemd and
their normal base utilities. The installer provisions the printing stack's
additional dependencies only after its safety checks. It does not install
KIAUH; it recognizes and can adopt supported layouts created by KIAUH/MainsailOS.

USB presence is polled every 100 ms and checked before commands and mutation
steps. Removal or re-enumeration latches cancellation, and the active process
group receives SIGINT, then TERM/KILL if needed. No automatic restart, rollback,
resume or reboot follows removal. **Linux scheduling, blocking I/O, APT maintainer
scripts and already-submitted systemd jobs prevent a hard instantaneous-stop
guarantee. An interrupted firmware write can leave the MCU requiring ROM DFU
recovery.** Keep power and the trigger connected until the final result. Thirty
minutes alone is not evidence of completion. This software cannot replace a
physical emergency stop or actuator power isolation.

## First use: inventory and audit

1. Extract the download and copy the whole folder to a USB storage drive or the
   Pi. An ordinary ATtiny85/Digispark HID board is not a storage drive; see the
   trigger section below.
2. With the printer supervised, run on the target Pi:

   ```sh
   python3 /path/to/auto-installer-v0.2/autoprinter.py --inventory
   ```

3. Record the trigger's exact `port`, `vid`, `pid` and `serial` from `usb` in the
   output and fill `trigger_usb` in `manifest.json`. If it has no serial, retain
   the empty string. Physical port is still mandatory; never select by product
   name or VID:PID alone. The USB stick can itself be the trigger, or a separate
   ATtiny can be used. Changing the hub/port requires updating the selector.
4. Run the default audit. Root is useful for journal/service visibility but the
   audit does not change printer services, packages, configs, Wi-Fi or firmware:

   ```sh
   sudo sh /path/to/auto-installer-v0.2/run.sh \
     --report /tmp/autoprinter-audit.json
   ```

`--inventory` is a deliberately read-only provisioning exception: it does not
require a trigger. All other modes require the configured trigger throughout.
Audit reports facts, missing files, logs, hardware and safety state; it does not
claim readiness or make a complete dependency/upgrade plan. An explicit
`--report` writes only the chosen report file. Do not choose an existing config
file as the report destination.

## Complete the manifest before apply

- Obtain the known working `printer.cfg`, `mainsail.cfg`, `macros500`, and
  `settings500`. Those last two names are literal; change `required_config` if
  your files actually end in `.cfg`. A detected, explicitly mapped Cartographer
  also requires `cartographer.cfg`.
- Put missing files in `profiles/` and add filename / relative source path /
  lowercase SHA-256 entries under `config_files`; see `profiles/README.md`.
  Existing files are preserved. If a supplied file differs from an existing
  file, apply refuses; merge manually first. Config pin assignments, includes,
  homing directions, PID settings, probe offsets and calibration are never
  fabricated. Startup macros/delayed G-code must be reviewed by the operator.
- Set `allow_package_install: true` after reviewing the package lists in the
  script. Set `adopt_standard_layout: true` only for an inspected standard
  existing installation. The installer preserves recognized existing units;
  it installs its own units only when absent. Missing/unusual partial units can
  require manual repair. Standard `mainsail-config` config symlinks are backed
  up; arbitrary external config symlinks are refused.
- Pin exact 40-character source commits under `revisions`, plus a versioned
  official Mainsail release URL and SHA-256. Do not use `main`, `master`, `HEAD`
  or an unversioned `latest` asset as installation pins. To resolve today's
  sources on a preparation computer, from the extracted folder run:

  ```sh
  python3 prepare.py --resolve-latest --output manifest.prepared.json
  ```

  On Windows use `python` instead of `python3` if necessary. This fetches current
  upstream commits and hashes the release download; it does **not certify that
  the newest components form a compatible set**. Inspect and bench-test them,
  then copy the reviewed pins into the machine's `manifest.json`. Reuse the
  tested manifest for repeat deployments. APT/Python transitive dependencies
  still come from their repositories; this is not a fully offline, hash-locked
  supply chain. `pip check` and live MCU handshakes provide runtime checks.
- Set `octopus.normal_usb` using its normal-mode physical port, VID, PID and
  serial. This is required for readiness. `octopus.mcu` may be filled from the
  actual verified MCU and is checked against reported Klipper constants.
- If Cartographer is physically present, map its exact `cartographer.usb`
  selector and set `install_plugin: true` to install the pinned current plugin.
  No mapping/detection means no optional plugin installation. Legacy `[scanner]`
  configs require a manual migration. Probe firmware flashing and calibration
  are outside v0.2. A detected probe must connect as `mcu cartographer` before
  readiness passes. Webcam devices are inventoried; camera streaming software
  is not installed or changed.
- Wi-Fi is blocked and brought down by default, or enabled with `--enable-wifi`, only after Ethernet is verified. Ethernet
  configuration, static addresses, DHCP, SSH settings and passwords are not
  rewritten. The radio block is for the current boot; systemd-rfkill can retain
  it. Persistent hardware Wi-Fi disable/boot overlays are not configured.
- Optional `disable_services` accepts only `bluetooth`, `cups`, `cups-browsed`,
  and `ModemManager`. The default is empty. Unknown processes are listed for
  review; they are never guessed to be unnecessary and killed.

Mainsail is served on the configurable port **8080** so an existing port-80 site
can be preserved. The installer creates only its nginx file and checks nginx
configuration. A busy port causes failure. Your home directory must already be
traversable by nginx; permissions are not silently weakened. New Moonraker
configuration trusts loopback clients only; set up remote authentication using
Moonraker's documented procedure for your site. Existing Moonraker config is
preserved, so its service/API paths must agree with the standard layout.

KlipperScreen uses the upstream X11 start script and a dedicated tty7 service on
fresh installs. An active desktop display manager is refused. The X11 wrapper
setting needed for this dedicated setup is backed up. No NetworkManager
takeover, forced HDMI mode, display rotation or board-specific boot setting is
attempted. Exactly one connected DRM HDMI output and a visual operator check
are required for readiness.

## Maintenance on a working, idle printer

Prevent external job submission and supervise the machine before using
`--maintenance-confirmed`. The script checks idle state twice, every configured
heater (target/output zero and temperature at most 50 C), motion, SD activity,
paused/failed states and pending saved calibration. Unknown/offline states
refuse mutation. Stop job sources and safely isolate actuators as appropriate;
an API poll cannot prevent every possible race with another operator.

```sh
sudo sh /path/to/auto-installer-v0.2/run.sh --apply --maintenance-confirmed
```

This installs/updates the host to the reviewed manifest, backs up configuration
and the stopped Moonraker database, stages new environments, retains old files,
applies the selected Wi-Fi policy and leaves printer services stopped for validation. It also repairs
missing supported service files and recreates Python environments. It does not
blindly reset repositories, erase config, force-remove APT locks, or run a global
OS upgrade. Before the first software installation at least **3 GiB** must be
free on each relevant filesystem; staging and backups recheck free space.
Large backups/installations can require substantially more.

For unattended software restart within an already supervised bench session,
add both `--display-confirmed --machine-config-confirmed` only if those claims
are valid for the selected release/config. Otherwise start and inspect the
stack yourself after reviewing the report:

```sh
sudo systemctl start klipper moonraker KlipperScreen
# Inspect HDMI display, loaded configuration, temperatures, wiring and calibration.
sudo sh /path/to/auto-installer-v0.2/run.sh --verify --maintenance-confirmed \
  --display-confirmed --machine-config-confirmed --report /tmp/autoprinter-verify.json
```

Verification does not start services, flash firmware, change Wi-Fi, or send any
G-code. It can succeed only when the services/API are already healthy and the
required state/USB/network/display/file checks pass. `SOFTWARE_READY` means
these checks and the operator confirmations passed; no physical motion,
endstop, heater response or extrusion test has been performed.

## Fresh Linux / offline MCU / ROM DFU recovery

For a fresh install, or an existing installation with an Octopus already in ROM
DFU, an API idle check is unavailable. Disconnect motor/heater actuator power
physically, keep the MCU on verified USB logic power, and inspect the machine.
The explicit isolated recovery path is:

```sh
sudo sh /path/to/auto-installer-v0.2/run.sh --apply --maintenance-confirmed \
  --recovery-isolated --actuator-power-disconnected
```

On an existing installation this override additionally requires `--flash` and
a verified DFU profile. A fresh install can omit flash when the MCU already has
compatible firmware. The script leaves all printer services stopped and returns
`INSTALLED_NEEDS_POWER_AND_VALIDATION`. Reconnect actuator power only under
supervision, then use the startup/verification steps above.

Flashing is **opt-in** with both `--enable-flashing-capability --flash`. The Octopus must already be in STM32 ROM
DFU, with a unique serial and physical port. The manifest requires the exact
board revision, an inspected `stm32f446xx`, `stm32f429xx` or `stm32h723xx` MCU,
operator identity, known working full Klipper `.config` and its SHA-256, exact
clock setting, nonzero bootloader offset, matching flash address, expected
normal-mode USB identity and recovery instructions. The script checks the build
settings again after `make olddefconfig`, builds from the same pinned Klipper
source as the host, and uses serial-selected `dfu-util` internal flash alternate
0. It never chooses a chip/boot offset from generic `0483:df11`, forces ROM DFU
entry, mass-erases flash, or flashes Cartographer/ATtiny. Katapult, CAN, SD-card
flashing and readback of old MCU firmware are outside v0.2.

## Low disk space, logs and recovery

Default behavior is to refuse changes below 3 GiB; at an almost-full rootfs it
prints the failure without requiring a persistent run directory. With explicit
`--allow-prune` added to apply, and a live safely idle printer, it can delete
direct-child G-code files older than `prune_older_days` (minimum 30, default 90)
only while their filesystem has less than 500 MiB free. It skips subdirectories,
symlinks and hardlinks. This deletion is permanent and not backed up. Reaching
500 MiB does not satisfy the 3 GiB installation gate; remove other files
yourself and retry. Logs/caches are never automatically erased. Recovery-mode
pruning is refused. Files on another filesystem cannot free rootfs space.

Apply writes a run log, JSON change/recovery ledger, manifest snapshot and
checksummed uncompressed backup under `/var/lib/autoprinter/<run-id>/`.
Staged trees remain under `/home/pi/.autoprinter/<run-id>/`; prior installed
paths are retained as `<name>.before-<run-id>`. Nothing automatically purges
these recovery copies. Configuration, database and service backups are local
to the same storage, so export them and keep a full SD/eMMC image for storage
failure redundancy. APT package changes and MCU writes cannot be transactionally
rolled back by this script. See `RECOVERY.md` before reuse after any failure.

Exit codes: `0` audit/software-ready, `2` refused/failed, `3` installed but needs
supervised validation, `130` cancelled/USB removed. The final
`OK TO REMOVE USB` means the process finished, not that the printer is ready.
On removal the script deliberately does not write a final JSON report; the
persisted ledger may show the last completed stage. Read the console/log and
firmware marker and inspect actual machine state.

## Single Linux command / ATtiny trigger

Public source: https://github.com/xt-programmer/autoprinter-installer

Download and run read-only hardware inventory on the target Pi:

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.2.0/install.sh | sudo sh -s --
```

Choose the trigger's physical USB port from that inventory. For a USB-gated
audit with the requested capabilities selected (no printer changes):

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.2.0/install.sh | sudo sh -s -- --usb-port 1-1.3 --enable-flashing-capability --enable-wifi
```

Replace `1-1.3` with the actual trigger port. VID/PID/serial are captured from
that explicitly selected port before download and kept fixed throughout the
session; optional `--usb-vid`, `--usb-pid`, `--usb-serial` additionally enforce
expected values. Ambiguous product-name/VID-only autodetection is never used.

For actual installation/maintenance, supply your completed local machine
manifest and add `--apply --maintenance-confirmed`:

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.2.0/install.sh | sudo sh -s -- --usb-port 1-1.3 --config /path/to/machine.json --apply --maintenance-confirmed --enable-flashing-capability --enable-wifi
```

The public release contains an incomplete generic manifest. Your actual board
profiles/configs can remain on the Pi/USB alongside the local machine manifest;
relative `config_files` paths resolve against that local manifest directory.
All existing idle, backup, disk, profile and supervised-recovery gates apply.

| Flag | During apply | During inventory/audit/verify |
| --- | --- | --- |
| `--enable-flashing-capability` | Install STM32 build/DFU tools and permit the guarded flash path | Record capability choice; no install/write |
| `--flash` | Write firmware only with the capability flag AND verified DFU profile | Refused |
| `--enable-wifi` | Unblock Wi-Fi, bring wireless interfaces up, enable NetworkManager Wi-Fi if already in use | Audit records preference; verify checks that preference without changing radios |

Flashing requires **both** `--enable-flashing-capability --flash`. Capability
alone never writes firmware or enters a bootloader. Wi-Fi remains disabled by
default; `--enable-wifi` overrides `disable_wifi: true` in the manifest.
`disable_wifi: false` also selects the enabled policy. No SSID/password is
added, so association depends on existing profiles. Ethernet remains required
for maintenance/readiness. Missing Wi-Fi hardware produces a clear warning;
a requested interface that remains administratively down or blocked fails
verification. Repeat `--enable-wifi` when verifying an enabled-Wi-Fi deployment.

`install.sh` embeds the downloader and an exact release ZIP SHA-256; no local
bootstrap file or GitHub account/token is required on the Pi. The tag URL pins
the launcher version. Initial launcher trust comes from your HTTPS access to
this repository; payload hashing does not provide an independent signature
for that first launcher. The source script can be downloaded/reviewed first.
No password is stored or injected by the ATtiny, and no startup/udev rule or
reboot-resume task is installed. Standard sudo may request a password on the
terminal. A future ATtiny command must execute in an already authorized shell.

Maintainers: `release.py --repository OWNER/REPO --tag v0.2.0 --output /outside/source/release`
builds the ZIP, checksum, embedded `install.sh` and exact one-command text.
Publish the generated ZIP/checksum as release assets and commit `install.sh`
before tagging that same release. Generated files are outside the source tree
so the ZIP cannot recursively include itself. Failed download workspaces remain
in `/tmp/autoprinter-download-*` for inspection.

An optional verified existing RGB `neopixel`/`dotstar` LED can indicate successful
apply readiness via `ready_led` fields `section`, `name`, exact `pin` (data pin
for dotstar), and `verified_by`. The script refuses an overridden `SET_LED`
macro; no arbitrary status macro or guessed board pin is executed. Leave this
field null until your LED wiring/config is supplied.

## Validation of this release

Run `python3 -m unittest discover -s tests -v` from the extracted folder.
The included tests cover print/heat/motion/unknown-state refusal, USB identity
and removal, process cancellation policy, disk gates, checksum/path checks,
config preservation and limited pruning. See `VALIDATION.md` for the actual
results and remaining hardware bench checks. No automatic install, flash,
reboot or printer motion was performed while creating this release.
