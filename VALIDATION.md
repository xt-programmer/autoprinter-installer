# Validation record - v0.6.0

12 November 2026: automatic private BadUSB console login without a resident
worker, manual Enter, pre-login, autologin policy or sudoers modification.

- Windows: 160 discovered, 156 passed, four POSIX-only skips.
  [All 160 passed in Linux CI on Python 3.9/3.12/3.14](https://github.com/xt-programmer/autoprinter-installer/actions/runs/43233546675).
  New tests cover exact tty/pi checks, credential
  input validation, private sudo pipe, stale enumeration before root/network,
  hash failure before sudo, argument restrictions, automatic Enter/hotkey,
  bounded capsule lines, deterministic helper bytes, no credential output,
  one-shot Digispark generation and no keystrokes after process start.
- Read-only SSH inspection of the authorized Orange Pi confirms US/pc105
  mapping, Bash, /dev/tty3 present with no login session and a normal getty
  template. No AutoPrinter worker/service/udev launch rule was installed.
- Physical Flipper login/hotkey delivery and ATtiny compilation/delivery remain
  untested. A fresh spare getty/login prompt is required; blind keystrokes
  cannot safely identify arbitrary existing console state. Stock Flipper's
  run-on-connect is supported by its official source, not tested on a device.
- Firmware and host transaction logic remains the v0.5 validated implementation;
  no additional MCU write, reset, printer configuration or heater/motor command
  was used to implement console automation. The bench remains uncommissioned.

# Earlier validation record - v0.5.0

12 November 2026: early flash eligibility/host-retention gate, three verified
STM32 build profiles, full flash backup/read-back/reset proof, artifact-only
programming and one-shot HID command generation.

- 149 tests discovered on Windows: 145 passed, four POSIX-only tests skipped.
  All 149 passed in [Linux CI on Python 3.9, 3.12 and 3.14](https://github.com/xt-programmer/autoprinter-installer/actions/runs/43222743334).
- All three real pinned firmware bundles were compiled in isolated CI:
  [firmware run 43201247770](https://github.com/xt-programmer/autoprinter-installer/actions/runs/43201247770).
  Config/image/vector/dictionary/source hashes are validated in the suite.
- One H723 Octopus Pro application write completed on the USB-powered Orange
  Pi 3 LTS bench. A full 1 MiB backup/read-back, unchanged 128 KiB bootloader,
  exact application bytes and dictionary before/after explicit reset passed.
  Matching host source was promoted to 29d4ec2d6d840ee839333306003a400da186d41d
  only after proof; prior source retained, existing native environment unchanged.
- Initial soft-DFU request HUP and post-DFU udev link delay were fixed. The
  physical transaction required supervised verification continuation without
  another firmware download. This is one write, not a clean one-shot CLI test.
  The corrected complete CLI still needs a fresh hardware run. F446/F429 have
  compile/policy validation, not physical flash validation here.
- Failure/removal tests verify one write at most, no leave/promotion after
  failed read-back/dictionary/reset, preserved bootloader, healthy-host hold,
  known-original repair exception and cancellation without repair fallback.
- HID tests cover exactly one HID interface/identity, product/serial mismatch,
  ambiguity/missing trigger, conflicting selectors, frozen enumeration handover,
  printable shell arguments, no implicit flash/apply, manual Enter by default,
  one-shot execution and ATtiny flash-storage/polling generation. No physical
  Flipper or ATtiny keystrokes have been exercised. ATtiny compilation remains
  pending with the actual board/core/library.
- Post-flash camera responses exposed UVC JPEG EOI followed by one zero-padding
  byte; marker checks now allow at most 16 trailing zero bytes and reject
  truncation/other padding. No camera image was saved. This is a marker/transport
  check, not an image-decoder or optics test.
- Klipper remains inactive/disabled without commissioned printer.cfg. No motion,
  heater, calibration, cold power-cycle, Pi reboot or physical HDMI test occurred.
  Reference printers remained read-only. The bench's retained source reduces
  current free space below the 3 GiB entry gate for a new apply transaction.

# Earlier validation record - v0.4.0

12 November 2026: added exact operator macro/settings baselines (Cartographer
6.5, manual-Z 5.6), a first-three-lines numeric version policy, backups/journal,
shared full-stack/accessory integration and a config-only guarded entry point.

- Four bundled source files match the supplied Windows files byte-for-byte.
  Git attributes preserve their line endings on other platforms; the template
  manifest and release checksums independently verify their hashes.
- 116 tests discovered on Windows: 112 passed, four POSIX-only tests skipped.
- New temporary-tree tests cover profile selection, spare/absent probes,
  first-three-line headers, numeric version order, missing/older replacement,
  same-version local edits/newer preservation, unknown-header refusal, template
  tampering, concurrent change, exact backups, mid-pair cancellation and refusal
  to auto-start Klipper after config changes despite earlier confirmations.
- PROCESS.txt mirrors the process.txt written to Desktop/AutoInstaller and
  documents the implemented branch order and current gaps.
- No networked printer was modified to validate this change. In particular,
  the read-only reference hosts remained read-only. File-policy tests are not
  a physical printing/commissioning test of these supplied macro/settings sets.

# Earlier validation record — v0.3.0

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
