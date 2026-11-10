# Validation record — v0.2.1

Prepared 10 November 2026. Test host: Windows, Python 3.12.14.

- 62 automated tests discovered: **60 passed, 2 skipped**.
- Skipped tests require real Linux process groups/pread and POSIX directory
  fsync. Their test cases are included for execution on the target Linux bench.
- Python source compilation and command-line help checks completed.
- Safety tests simulate unsafe/unknown printing and heater/motion states,
  recovery isolation refusal, USB disappearance/replug, cancellation before
  command start and during a child command, invalid/ambiguous DFU selections,
  flash offsets/chip mismatch, checksum mismatch, config preservation, archive
  traversal/symlink/size rejection, disk shortage and limited pruning.
- Final release packaging is checked for expected files, valid JSON, Python
  syntax, LF shell-script endings and archive integrity.

No APT operation, systemd change, download/apply on a Pi, MCU build/flash,
printer motion/heating, or hardware HDMI test was performed. WSL/Linux was not
available in the authoring environment. The supported-machine allowlist does
not establish hardware compatibility.

## Required bench validation before deployment

1. On a cloned/sacrificial Pi storage image, run all tests under Linux, then
   inventory/audit every target board/image/hub topology. Capture both normal
   and ROM DFU identities and verify exact board/MCU markings, clock, offset and
   build settings from a known working setup.
2. Supply and review printer files; verify all includes and any startup/delayed
   G-code, sensors, limits, firmware/probe compatibility and calibration.
3. Use actuator power isolation for initial fresh/recovery installation. Test
   missing internet, authentication errors, APT locks/broken state, low storage,
   missing peripherals, and every unsupported/custom layout refusal.
4. Confirm refusal during printing, paused/error states, heating, a warm bed,
   extruding/moving, and pending calibration. Confirm an isolation flag cannot
   override a known live unsafe state.
5. Unplug the trigger during audit, staging/build and a harmless long command.
   Verify cancellation, no automatic restart/resume, and correct recovery logs.
   Firmware interruption testing requires a recoverable spare MCU and verified
   ROM DFU/SWD recovery; it is not appropriate on a production printer.
6. Test the exact pinned release set, screen startup and console handoff,
   camera/probe absence, optional plugin, Wi-Fi block and Ethernet/static-IP
   preservation. Inspect nginx permissions/port/authentication on that image.
7. Practice restoring host source/venv/database/unit files from a failed
   deployment and, separately, recovery of a spare MCU. Verify retained paths
   and off-device backups before production use.
8. Under supervision, perform the manufacturer's physical commissioning checks
   for endstops, motion directions, heater sensors/control, probe and extrusion.
   `SOFTWARE_READY` alone does not cover these checks.

The GitHub Actions workflow runs the same suite on Linux/Python 3.9 and 3.12,
checks shell syntax, and supports a manual live release-download inventory smoke
test. See the repository Actions tab for the current run results. The new tests
cover the feature flags, selected Wi-Fi policies, bootstrap forwarding, USB
identity capture, ZIP traversal, HTTPS downgrade and exact release hashes.
