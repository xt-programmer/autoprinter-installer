# Operator-supplied 500 macro/settings sets

Only `macros500.cfg` and `settings500.cfg` are bundled. The exact source bytes
are retained and each SHA-256 is checked against `config_sets/manifest.json`.

| Printer profile | Supplied folder | Header version |
| --- | --- | --- |
| `cartographer` | `500 PROBE` | 6.5 |
| `manual-z` | `500 V5.6` | 5.6 |

Read-only plan on a Linux Pi:

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.4.0/install.sh | sudo sh -s -- --configs
```

For a fresh/missing `printer.cfg`, select a reviewed type explicitly with
`--printer-profile cartographer` or `--printer-profile manual-z`. A currently
named USB Cartographer can also select the former when printer.cfg is absent.
No probe history alone establishes the current printer type. For an existing
machine, auto selection follows the active include graph: `[cartographer]` and
`[mcu cartographer]` with the Z virtual probe endstop select Cartographer; a
physical MCU/GPIO Z endstop without Cartographer selects manual Z. Sensorless,
other-probe, conflicting or ambiguous layouts refuse. An explicit profile
cannot override a contradictory active configuration. Plugging a spare probe
into a configured manual-Z printer does not change its config set.

Version comparison uses the one numeric `Version:` comment in the first three
lines, compared as integer components: 6.10 > 6.9; 6.5 equals 6.5.0. Dates and
filesystem timestamps do not determine which file is newer.

- Missing file: install selected template.
- Lower version: back up the existing bytes and replace it.
- Same version and same hash: leave it alone.
- Same version with local changes: preserve it and report the difference.
- Higher version: preserve it.
- Missing/malformed/ambiguous header: block changes to both files for review.

The normal full-stack installer applies this policy automatically, using
`printer_profile` in its machine manifest (`auto` by default). The accessory
installer also applies it and accepts `--printer-profile`. The two names cannot
also be provided through `config_files`, avoiding two competing config sources.
Legacy extensionless required names normalize to their `.cfg` filenames.

For a config-only supervised USB apply, add your trigger identity:

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.4.0/install.sh | sudo sh -s -- --configs --usb-port YOUR_TRIGGER --apply --maintenance-confirmed
```

For an explicitly supervised `pi` SSH shell, replace `--usb-port YOUR_TRIGGER`
with `--supervised-ssh --ssh-session-pid "$$"`. This config-only mode requires
inactive Klipper and 3 GiB free before a change; it performs no package or
network operations and supports the inspected Debian 11/Python 3.9 hosts as
well as newer userspaces. This does not bypass full-stack OS/Ethernet gates.

The two targets and selected template hashes are rechecked before writing.
Originals and a transaction journal are kept in each run's `config-files-before`
directory under `/var/lib/autoprinter/`. Replacement is atomic per file, with
fsync; the pair is not a single atomic filesystem transaction. Interruption can
leave one updated file, so Klipper remains stopped, originals remain available,
and there is no automatic restart, rollback or resume.

Config-only and accessory apply never start Klipper. A full-stack apply that
changed either managed file also leaves printer services stopped, regardless
of confirmation flags given before the update. Review the resulting files,
restore/start services under supervision, then use read-only readiness checks.
Successful copying is `CONFIG_FILES_UPDATED_REVIEW_REQUIRED`, not printer readiness.

These supplied files contain machine-specific motion, heating, homing and macro
behavior. Their use was selected by the operator for the 500 fleet. They do not
identify board pins or validate another machine. The installer does not create
or edit printer.cfg/includes, cartographer.cfg, serial paths, probe offsets,
SAVE_CONFIG calibration, Wi-Fi, or MCU build/flash settings in this mode. Fresh
printers still need their own reviewed configuration and commissioning.
