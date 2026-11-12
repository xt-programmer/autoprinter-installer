# Recovery after failure or interruption

1. Keep the machine supervised. Prevent new print submissions. Use the physical
   stop/isolation controls if movement or heating is unsafe. Do not assume a
   console message or disconnected host proves actuator power is off.
2. Do not immediately reboot or rerun apply. An interrupted APT operation may
   need package recovery; a started firmware write may need ROM DFU recovery.
   USB removal deliberately suppresses all automatic rollback/restart work.
3. Inspect `/var/lib/autoprinter/<run-id>/run.log`, `report.json`, the manifest
   snapshot and any `FIRMWARE_WRITE_STARTED` marker. The JSON may describe the
   last completed stage rather than the abort itself. `moves` records each
   destination, retained path and new source, with intent written before moves.
4. Back up the run directory and retained trees to another device before any
   recovery. The run's `backup_sha256` can be checked with `sha256sum`. Inspect
   `tar -tf backup.tar` before restoring anything. It contains absolute-root
   relative paths, local configuration/database/service snapshots, not an OS
   image or previous MCU firmware.
5. With a verified idle printer or physical actuator isolation, stop the printer
   services under supervision. To restore a retained source/venv path, inspect
   the exact `destination` and `retained` in `moves`; preserve the current
   destination by renaming it to an unused failed-state name, then rename the
   retained path back. A retained item may itself be a symlink from a previous
   deployment. Keep the referenced staging directories until recovery is done.
   Never recursively delete guessed paths. The ledger is advisory; verify the
   actual filesystem after a power loss before any rename.
6. Extract `backup.tar` into a separate temporary inspection directory. Restore
   only the reviewed original files/units; restore the Moonraker database only
   while Moonraker is stopped. A newer Moonraker may have migrated its database,
   so reverting source without the matching backup can be insufficient. Remove
   only positively identified new AutoPrinter units/site files if rolling back
   a fresh install. Restore prior service enable/active states from the report,
   then `systemctl daemon-reload` and verify configuration before starting.
7. Package changes, account group membership and runtime radio blocks are not
   reversed by the source-tree restore. Review the APT log and saved changes.
   A package manager must be inactive before any supervised `dpkg --configure
   -a`/APT repair; do not remove lock files or run such commands blindly. If the
   storage has I/O errors, recover from a known-good full storage image instead.
8. If `FIRMWARE_WRITE_STARTED` exists or DFU download was interrupted, keep motor/
   heater power isolated. Follow the exact inspected Octopus revision's BOOT/
   reset/power instructions from its manufacturer. Re-enter STM32 ROM DFU and
   use the verified profile's nonzero application offset and known-good firmware.
   v0.5 records full mapped MCU flash in flash-before.bin plus SHA-256 before
   application download; flash-after.bin and flash-result.json record read-back
   and reset proof. Also inspect HOST_PROMOTION_INTENT.json and retained source
   when artifact-mode host promotion was requested. The backup is not an
   automatic rollback or a generic bootloader repair file. If
   ROM DFU does not work, use the manufacturer's SWD/recovery procedure with
   qualified supervision. Do not infer an MCU from `0483:df11`.
9. Restore power/start services only under supervision, inspect the display,
   check the machine calibration/config, and run read-only `--verify`. Never
   treat a successful Python test suite as evidence that the real printer's
   heaters, endstops or wiring are correct.

There is intentionally no one-command automatic rollback: host packages,
database migrations and MCU flash do not share an atomic transaction. Explicit
inspection prevents a source rollback from silently reconnecting a mismatched
MCU or starting an unverified printer.
