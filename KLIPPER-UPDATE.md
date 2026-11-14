# Update payload v0.8.0

Only the selected Octopus MCU, Klipper host/environment and necessary build
dependencies are updated. Printer configs, plugin versions, other printer services
and radio settings stay unchanged. No resident worker, reboot or later retry task.

The private Flipper kit has two scripts:

- **01-PLAN-UPDATE.txt**: inspect board, current host revision, free space, latest
  upstream commit and potential cleanup. No serial command, deletion, stop or flash.
- **02-UPDATE-LATEST.txt**: actual update. It includes capability, maintenance,
  physically confirmed Octopus, disconnected heater/motor power and soft-DFU
  confirmations. Select only after those conditions are currently true.

Copy both to Flipper SD Card/badusb/. Close qFlipper. With USB unplugged, open
the desired script in Bad USB, USB mode, US layout; press Run to arm on-connect,
then insert. Re-arm each time. Requires the established pi login/password, fresh
tty3 prompt and network. No printer-side typing/Enter is needed. Leave Flipper
connected until the Pi finishes and returns to KlipperScreen; Flipper Done only
means typing ended. The private ZIP contains the credential; it is not published.

The payload auto-selects exactly one supported normal Klipper USB MCU. None,
multiple eligible boards, unknown descriptors or unsupported STM models produce
red error. Supported physically confirmed Octopus profiles are F446, F429 and H723.
A chip/product string alone does not identify the PCB. Initial ROM-DFU/unflashed
boards, UART/Katapult, corrupt/missing hosts and arbitrary STM boards require the
separate reviewed installation procedure. An old firmware baseline is required.

## Exact order

1. Check board presence/serial/STM profile, native pi service/source/environment,
   Debian/Ubuntu family and supported Pi hardware. Capture the separate trigger
   enumeration or supervised SSH PID and take the shared lock. Read current host
   commit/version; check old environment and retain its package/config fingerprint.
   Logs/report: `/var/lib/autoprinter/update-*/`.
2. A running printer must pass the complete ready/idle/unpaused/stationary,
   zero heater targets/output, temperature and unsaved-config checks. Capture
   current G-code paths, recheck and stop only Klipper **before user-file deletion**.
   A positively identified existing MCU protocol error may enter repair under
   physical isolation; unknown activity/errors refuse. That case does not delete
   G-code because its current job cannot be proven. Initially inactive stays inactive.
3. Require 3 GiB entry space on the relevant filesystems. If short, clean APT
   downloads, then remove only as much eligible material as needed: older dated
   config backups first, then G-code **at least 48 hours old**. Keep `printer.cfg`,
   newest `printer-YYYYMMDD_HHMMSS.cfg`, current/recent G-code, all configs referenced
   by active includes, unrelated files, symlink targets and recovery backups.
   Recheck identity/include references before unlink. Delete only on a filesystem
   actually short of space. `cleanup.jsonl` records deletion intent/result.
   These authorized deletions are permanent. Still short: abort before DFU/flash.
4. Resolve official Klipper master once to a full SHA and clone that exact source
   privately. Use the **same frozen SHA** for board and host; do not resolve again
   mid-job. `--source-commit FULL_SHA` optionally pins a reviewed version. Install
   only missing build dependencies, without a general OS upgrade.
5. Preserve installed external `.py`/`.so` plugins/stable external links with hash
   receipts. Reject collisions, unsafe links and tracked source edits. Copy the
   old environment privately, preserving plugin dependencies; install the new
   Klipper requirements there and run pip/help checks. Keep installed source/env.
   Reserve the environment-copy size plus 700 MiB, then 500 MiB before MCU work.
6. With Klipper inactive, identify current MCU firmware/dictionary and verify the
   STM model. Request bootloader and require actual ROM DFU at the same physical
   port. On failure/disappearance show red and try once more: **two entry attempts
   total**. The installed host is unchanged. Restore its running state only if
   the original normal board is still proven reachable.
7. Bind DFU to physical port + ROM serial + alt 0. Validate internal flash map,
   permissions, bootloader/application vectors, and back up **all** readable flash
   with hash and original dictionary before any firmware write.
8. Guard that DFU identity during `make clean`, reviewed menuconfig-equivalent
   seed, `make olddefconfig`, normalized profile validation and `make -j2`.
   This is noninteractive menuconfig, setting STM architecture/model, offset,
   crystal and USB PA11/PA12. Save image/dictionary/config/compiler/hash manifest
   outside generated outputs; recheck source SHA/contents after build.
9. Verify DFU again before writing. Check map and original bootloader/untouched
   bytes before every attempt. Flash only the application; read all flash back
   and verify exact new image plus unchanged bootloader/outside sectors. A failed
   download/read-back can retry on the same proven target with intact outside
   bytes: **two new-image attempts total**, including any synchronization reflash.
   No mass erase, unprotect, option-byte write or speculative replacement target.
10. Leave DFU without downloading again. Require normal USB plus udev path, exact
    built dictionary, explicit MCU reset and same dictionary after reset. Check
    staged host protocol parser/core formats. Only VERIFIED for that SHA permits
    host **and** environment promotion. Retain old native directories and intents.
11. Start only an originally active Klipper. Require actual current PID and five
    seconds of ready/idle within 60 seconds. Old logs/cached API do not prove success
    or cause downgrade. Fresh protocol error: one synchronization pass stops
    Klipper, checks selected MCU/host alignment, reflashes the frozen build only
    if needed within the same two-image budget, verifies host SHA and restarts/checks
    once. Already matching selected MCU is not blindly reflashed. Other MCUs/probes
    are not flashed here; unresolved secondary-MCU protocol mismatch fails the update.
12. After build/program/verification/startup failure with a valid backup, attempt
    old firmware/host/environment restoration. Verify retained source/plugins/env
    fingerprints, backup hash, exact board/ROM and flash map. Partial failed writes
    allow recovery only with original bootloader and untouched sectors intact.
    At most **one restorative download** beyond the two update-image attempts
    restores original affected application sectors at the nonzero offset. Already
    original flash requires no write. Require full-flash equality with the original
    backup, then original live dictionary before/after MCU reset. Only then restore
    old source/env and prior running/inactive state. Changing USB devnum is expected;
    fixed serial/port/map must match. Retain reports, firmware and recovery trees.
13. Verified success: `make clean`, APT download-cache cleanup, remove private
    identify/staging/download trees, record remaining space. Remove generated
    `out/`; retain tracked Makefiles, installed build dependencies and recovery
    artifacts. Show **green update success for five seconds**, matched version and
    actual ready/inactive state. Cleanup failure is reported separately without
    downgrading a verified pair. Existing helper returns to the actual display of
    already running KlipperScreen and logs out tty3.

Profiles follow [Voron's Octopus settings](https://docs.vorondesign.com/build/software/octopus_klipper.html):
F446 32 KiB/12 MHz; F429 32 KiB/8 MHz; H723 128 KiB/25 MHz; STM32/USB PA11/PA12.
DFU uses [dfu-util's port/serial/alt selectors](https://dfu-util.sourceforge.net/dfu-util.1.html).

## Failure behavior

Final red errors remain for eight seconds before returning to a running GUI.
Recovered old pair still means update failure (exit 2), never green. A pre-existing
protocol-error installation can be restored to that same previous error state.
Inactive/uncommissioned installations get green **update verified; Klipper remains
inactive** only; this does not commission printing or invent printer.cfg. Normal
startup loads existing macros/config: keep actuator power disconnected throughout.

An unplugged/fried board, unreadable flash, damaged bootloader, changed identity
or lost power can prevent restoration. Report `RECOVERY_INCOMPLETE`; never claim
rollback success without verification. Power-loss recovery is manual. Trigger
removal/signal cancellation stops further flash, rollback, cleanup and restart.
There is no background monitor for protocol errors after this job finishes.

## Linux command

Plan:

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.8.0/install.sh | sudo sh -s -- --klipper-update-only
```

Apply using the separate Flipper trigger and confirmed physical isolation:

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.8.0/install.sh | sudo sh -s -- --klipper-update-only --hid-vid 1234 --hid-pid abcd --hid-product 'Supervised launcher' --apply --enable-flashing-capability --maintenance-confirmed --actuator-power-disconnected --board-confirmed-octopus --allow-bootloader-request
```

For an authorized pi SSH shell replace HID options with
`--supervised-ssh --ssh-session-pid "$$"`. Optional `--chip stm32h723xx`,
`--target-port PORT`, `--target-serial SERIAL` select a specific known board.

For an explicitly supervised bench with an inspected storage/build-dependency
budget, `--bench-entry-space-mib 2048` can lower the initial allowance to 2 GiB.
It requires apply + supervised SSH, accepts only 2048..3072 MiB, and is rejected
for ordinary USB payloads. The environment-copy reserve and 500 MiB minimum
before MCU work remain enforced. Default entry space remains 3 GiB. This is an
explicit bench exception, not automatic low-space fallback.

Tests cover phase ordering/promotion gates, bounded entry/write attempts, partial
image recovery, expected DFU devnum change, source/env restoration, protocol sync,
cleanup cutoff/retention/includes and green after cleanup. On 14 November the full
SSH H723 path passed a fresh Pi build, one actual application write, whole-flash
read-back, preserved bootloader, live/post-reset dictionary checks and paired
source/environment promotion. It used the explicit measured bench space allowance.
Physical downgrade, F446/F429, cold-power testing and the complete Flipper/ATtiny
insertion/display-return sequence still need their own tests. See VALIDATION.md.
