# Klipper and Octopus only

`--klipper-update-only` selects a separate transaction. Without `--apply` it
audits the selected board and release image. With `--apply` it requests both
firmware flashing and promotion of the matching Klipper host. It does not
install/update Mainsail, Moonraker, KlipperScreen, Crowsnest or Cartographer;
it preserves printer configs, service units, the Python environment and radio
settings. A running Klipper is stopped only after a complete ready/idle check.
An originally inactive Klipper stays inactive.

Audit example for the inspected H723 bench (other printers need their own exact
board identity from inventory):

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.7.0/install.sh | sudo sh -s -- --klipper-update-only --chip stm32h723xx --target-port 5-1 --target-serial 410022000251313239393734
```

For the actual supervised write, leave the selected Flipper trigger connected
and explicitly confirm the physical board/profile and disconnected actuator
power. Run this after those conditions are true:

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.7.0/install.sh | sudo sh -s -- --klipper-update-only --hid-vid 1234 --hid-pid abcd --hid-product 'Supervised launcher' --chip stm32h723xx --target-port 5-1 --target-serial 410022000251313239393734 --apply --enable-flashing-capability --maintenance-confirmed --actuator-power-disconnected --board-confirmed-octopus --allow-bootloader-request
```

An already authorized SSH session can instead use
`--supervised-ssh --ssh-session-pid "$$"` in place of the three HID options.
The separate `--flash` and `--prepare-host-update` switches are implied by this
mode; capability and physical confirmations remain required. Extra full-stack
options, such as `--enable-wifi`, are rejected by its strict parser.

The default target is the **release-pinned** source/image pair, currently
`29d4ec2d6d840ee839333306003a400da186d41d`, rather than a moving upstream HEAD.
For a different reviewed version, build a bundle with `build_firmware.py` or
the firmware CI workflow, then pass `--firmware-bundle /local/bundle` and
`--expected-firmware-sha256 EXACT_IMAGE_HASH`. The host candidate is checked
out at that bundle's exact source commit. Source and image must stay paired.

1. Validate the artifact's hashes, MCU, crystal, USB interface, bootloader offset
   and vectors; select the exact physical board port and normal-mode serial.
   Check the supported Pi hardware, shared maintenance lock, 3 GiB entry space,
   `dfu-util`, native pi service/source/environment, host health and pip check.
2. Privately stage the pinned host without replacing the installed one. Changed
   dependency requirements or tracked local source edits refuse before flashing.
   Preserve untracked external `.py`/`.so` plugin modules and stable external
   symlinks, including Cartographer, with hash receipts. New-source collisions,
   missing/oversized modules and links into the replaced host tree refuse.
   No plugin repository/dependency is updated. Keep at least 500 MiB staging
   reserve. If Klipper was running, recheck idle and stop only Klipper.
3. Identify the original MCU without loading printer.cfg; retain its dictionary.
   Request DFU, then require actual ROM DFU at the same physical port. A request
   exit code or VID:PID alone is insufficient. **The installed host is still
   unchanged.** Failed DFU entry stops the update.
4. Bind DFU by port, ROM serial and alt 0. Validate its internal flash map and
   save the whole readable flash. Require valid existing bootloader AND old
   application vectors at the reviewed offsets before permitting a write.
5. Flash the new application once. Verify complete read-back, the exact image,
   unchanged bootloader and sectors outside the application's erase range.
   Require normal USB/udev return, the built live dictionary, an explicit MCU
   reset and the same dictionary again. No Pi reboot occurs.
6. Use the candidate's protocol parser to identify the live MCU and resolve core
   host command formats. Only after these checks promote the matching source,
   retaining the old native source tree. The existing environment stays in place.
7. If Klipper was running originally, start it and check up to 60 seconds for
   ready/idle, its actual service PID, five seconds of stable readiness and fresh
   startup errors. The normal startup loads its existing printer configuration;
   it may run existing startup macros, so keep actuator power disconnected.
   Cached Moonraker results from the old process cannot authorize success.
   An inactive installation receives protocol checks but is not commissioned
   or started by this mode.
8. A positively identified protocol error during this transaction triggers one
   bounded downgrade attempt: stop Klipper; validate the retained old source,
   plugins and backup hashes; request DFU from the exact same normal board; and
   require the same ROM identity/map. Read flash again and require equality with
   this transaction's verified new read-back before any recovery write.
9. Restore only the application erase sectors touched by the update, using
   their original backup bytes. Require **the entire flash to equal the original
   backup**, followed by the original live dictionary before and after MCU reset.
   Only then restore the retained old host and its original running/inactive
   state. Keep the failed candidate, reports and all flash backups.

`PROTOCOL_ERROR_PREVIOUS_PAIR_RESTORED` returns failure (exit 2): the requested
update failed, even though recovery succeeded. `PROTOCOL_ERROR_RECOVERY_INCOMPLETE`
requires inspection and leaves Klipper stopped where the connection permits.
Host-only rollback is never reported as restoration of the firmware/host pair.
Ordinary G-code "Unknown command" messages do not trigger a firmware downgrade.

USB removal/cancellation authorizes no further writes, rollback or service
restart. Transport, programming, read-back and unknown-identity failures stop;
they do not authorize a speculative recovery write or a second update attempt.
After failure Klipper can remain inactive and the board can remain in DFU.
Backups are under `/var/lib/autoprinter/board-flash-*/`; source retention and
promotion/recovery intent paths are in the report. Power-loss recovery is manual;
there is no installed worker, background monitor or automatic later retry.

This mode needs a normal, identifiable existing Klipper MCU to establish its
previous firmware baseline. Initially DFU, blank/unflashed boards and corrupt
hosts use the separate reviewed `--board-flash`/repair procedures. It handles
one selected Octopus, not simultaneous updates of every MCU. Other MCUs remain
unchanged; a resulting startup protocol mismatch causes the pair downgrade.
UART/Katapult/custom layouts and dependency migrations are outside this mode.

For BadUSB use `make_hid.py --mode klipper-update-only` with the same target
arguments. Add `--console-login --password-file /private/pi-password` for the
existing insertion-only login flow, and explicit apply/capability/isolation
flags for a write payload. Generated credential-bearing scripts stay private.
The private kit's 06/07 payloads target **only the inspected .170 H723 board**;
07 includes the physical isolation confirmations above. Do not select it until
those conditions are true. The bench currently has less than the required
3 GiB free, so apply will refuse until sufficient space is available.

Validation covers simulated DFU failure, one recovery write, exact full-image
restoration, unchanged bootloader, changed board/flash/backup refusal, source
restoration ordering, stale versus fresh protocol errors, cached PID exclusion,
plugin retention and USB cancellation. The new automatic downgrade has not yet
been exercised with a physical restorative flash; previous H723 update/read-back
and reset verification succeeded. F446/F429 remain without physical validation.
