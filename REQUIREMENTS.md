# Coverage against the supplied requirements

The source text in Desktop/AutoInstaller was treated as the specification.
Missing physical board settings/configuration are intentionally left for the
operator. This table distinguishes implemented paths from v0.2 limitations.

| Item | v0.2 behavior |
| --- | --- |
| info: ATtiny downloads/runs GitHub script | Optional checksum-pinned USB-gated bootstrap; public GitHub release and one-command launcher provided; HID firmware/login provisioning remains an operator task. Ordinary ATtiny is not a USB storage drive. |
| info: wait 30 min / OK to remove | Explicit completion result and removal message. Elapsed time alone never implies completion. |
| info: Pi models and Linux images | Model, architecture and userspace allowlist; no physical hardware matrix has been validated. |
| info: pi user / Ethernet / internet | Standard pi layout checked; Ethernet route/carrier and bound HTTPS probe checked before apply. No password changes or insecure login automation. |
| info: optional probe/camera | Explicit probe identity gates plugin installation; absent devices receive no optional installation. Camera inventory only. |
| info: independent restart | No automatic Pi or MCU power cycle/reboot, and no script startup service. |
| info: abort when trigger removed | Latched 100 ms polling plus operation checkpoints and command process-group cancellation; no automatic subsequent maintenance/recovery. Hard real-time stop is not possible; see README. |
| info: no boot rerun | No autorun/udev/systemd/timer/cron rule for this script and no reboot resume token. |
| info: near-zero storage | Console-first refusal before persistent staging; narrow explicit old-G-code pruning when idle. No blind cache/log deletion. |
| info: Octopus bootloader flash | Opt-in exact verified full build profile, physical port + serial, ROM DFU only, source pinned with host. Missing settings refuse. |
| 0a: working HDMI + KlipperScreen | Install/maintain supported KlipperScreen path; check connected HDMI and active service. Actual GUI/EDID/rotation verified by operator. No universal HDMI repair. |
| 0b: progress on HDMI console | After safe service stop, mirror progress to tty1 and use chvt when available. No forced switch during a print. Audit stays on caller terminal. |
| 1a: Linux | Platform check; Windows invocation refuses modification. |
| 1b: Ethernet internet | Physical Ethernet route, carrier and HTTPS probe bound to interface; no guessed IP/DHCP rewrite. |
| 2: Pi identification | Device-tree model, architecture, Armbian/os-release inventory and apply allowlist. |
| 3: existing stack or install | Recognize MainsailOS/KIAUH markers and supported standard paths; create fresh stack without needing either manager. Multiple/custom layouts refuse. |
| 4a: 500 MB / old G-code | Explicit prune flag, safe idle, old regular direct-child G-code only, stop at 500 MiB. No deletion by default. |
| 4b: 3 GB gate | At least 3 GiB on root, /var and home before installs; repeated checks plus extra backup estimate. More can be required. |
| 5a: four components | Pinned staged Klipper, Moonraker, Mainsail and KlipperScreen; retained prior source/environments; missing supported units provisioned. |
| 5b: dependencies | Explicit APT dependency set and fresh Python environments; pip check. Broken dpkg state refuses for manual recovery. |
| 6: compatible versions | Reviewed immutable source pins; dependency checks, live Klipper protocol readiness for configured MCUs and captured version constants. Latest pins alone are not compatibility certification. |
| 7: MCU visibility | USB/sysfs and serial inventory; configured Octopus port tied to loaded main-MCU serial path during verification; mapped Cartographer must connect as mcu cartographer. |
| 8: normal / bootloader | Exact selected normal USB identity or STM32 ROM DFU identity. Generic VID:PID not used for board identification. Katapult/CAN/SD workflows unsupported. |
| 9: H7 / F4 | Report runtime MCU constants when available; flash needs physically verified exact MCU. ROM DFU alone cannot reliably identify board revision/clock/offset. |
| 10: firmware compatibility | Live ready handshake, reported firmware versions and optional exact MCU-constant check; selected Octopus can be built/flashed from host source pin. No probe firmware updater in v0.2. |
| 11: logs | Recent Linux error journal and recent Klipper error/traceback/shutdown matches printed/reported; historical matches are not blindly treated as present faults. |
| 12: required configs | Exact presence check; missing files require checksum-pinned operator copies. Existing differing configs never overwritten. Machine-specific calibration not invented. |
| 13: Wi-Fi off / Ethernet on | Verify Ethernet before the selected Wi-Fi policy and again at readiness. Default is block/down; --enable-wifi opts into unblock/up and matching readiness checks. No interface-manager replacement; persistent boot overlay changes outside v0.2. |
| 14: only necessary processes | Process inventory and explicit narrow optional-service disable list; no claim to identify every unnecessary process across arbitrary images. Essential processes not killed by heuristics. |
| 15: changes + screen return | Persistent change ledger and log; on successful supervised ready apply return to tty7. Validation/failure/recovery stays stopped/visible for inspection. |
| info: LED end signal | Optional verified RGB LED via constrained SET_LED; no guessed pins/macros. Completion remains explicit in console/report. |

Therefore this is a guarded first implementation, not a universal zero-touch
printer repair tool. Additional hardware profiles and a supervised hardware
test matrix are necessary before broad deployment.
