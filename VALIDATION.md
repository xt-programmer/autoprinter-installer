# Validation record — v0.3.0

12 November 2026: native KIAUH layout on Orange Pi 3 LTS, aarch64,
Armbian/Ubuntu 26.04, kernel 6.18.44 and Python 3.14.4.

- Crowsnest v5.0.12 (`436042452f564f3d737e3b980a213f849a8a0562`),
  official installer and its ustreamer build fallback, installed and running.
- Logitech C270 stable USB capture path selected; SoC video0/video1 excluded.
  Complete JPEG received through the port-80 Mainsail webcam proxy. Camera
  registered in Moonraker; no captured image saved.
- KlipperScreen (`26d262522b9bd9fe7d67f67d3c6f90aa156d1b07`), native
  environment/service and original requirements, including PyGObject 3.50.2,
  built and imported successfully on Python 3.14. Full GUI ran for 25 seconds
  under Xvfb, connected to Moonraker and displayed the expected disconnected
  Klipper state. Timeout exit 124 was intentional test termination.
- Physical HDMI disconnected. Enabled native service correctly skipped startup
  through its display-presence condition. Physical display output remains pending.
- Live `peripherals.py --apply` completed against the installed native layout,
  preserved port 80/network, checked camera JPEG and returned no Moonraker warnings.
- Cartographer absent from current USB and retained kernel evidence, so its
  installer/source/dependencies were skipped. Positive detection, latching,
  legacy migration refusal and unrelated H723/DFU exclusions have policy tests;
  the positive installation path still needs an actual probe bench test.
- H723 Octopus enumerated normally as a Klipper USB MCU; no serial interaction,
  reset/flash, printer configuration, motor/heater commands or reboot occurred.
  Klipper remains inactive/disabled, without printer.cfg.
- Windows suite: 93 discovered, 90 passed and 3 Linux-only tests skipped.
  All 93 passed on the physical Linux Pi, including process-group cancellation
  and directory fsync. Native port-80 preservation, nginx syntax, HTTP assets
  and WebSocket JSON-RPC were also verified live. CI includes Python 3.9,
  3.12 and 3.14.

This is one host/peripheral combination, not a compatibility matrix or a printer
readiness claim. Initial native software setup and subsequent repair were both
exercised; the complete fresh accessory CLI flow still needs a clean-image run.

# Earlier validation record — v0.2.1

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
