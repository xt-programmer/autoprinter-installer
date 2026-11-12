# Automatic USB launch — no resident worker

v0.6 adds automatic Linux-console login to BadUSB. A prepared Flipper payload
or suitable ATtiny keyboard switches to a spare console, logs in as `pi`,
downloads the installer and executes its reviewed arguments. **No keyboard or
Enter on the printer, and no AutoPrinter service, worker, udev trigger rule,
sudoers change or autologin setup is needed.**

The target still needs its normal USB keyboard/virtual-console support, Python
3.9+, curl, sudo and internet. The Orange Pi reference uses US/pc105 mapping,
Bash, has `/dev/tty3` with no logged-in session and a normal getty template.
The operator confirms the other Pis use the same keyboard/language. A
touchscreen's lack of typing controls does not prevent Linux receiving USB HID.

BadUSB sends blind keys: **tty3 must initially be a fresh login prompt**, rather
than a logged-in shell, application, autologin session or locked GUI. Switching
VT starts the image's existing console login. No forced interrupt, reboot,
logout of an unknown session or guessed prompt recovery is attempted. The new
completion return still needs its full insertion retest; the earlier real
Flipper login/inventory succeeded on the reference Pi.

## Insertion-only Flipper tests

1. Copy your **privately generated** automatic `.txt` files to **SD Card/badusb/**
   using qFlipper/mobile app; then close qFlipper.
2. With USB **disconnected**, select the payload in Bad USB, choose **USB** mode
   and **US** layout. Press **Run while disconnected** to arm run-on-connect,
   then insert its data cable into the target Pi's USB host port. No Pi action
   is required. Arm it again before each new connection; stock Flipper is not a
   permanently auto-armed ATtiny.
3. Start with automatic inventory, then audit/peripheral plan. Select apply
   only after its machine settings and arguments have been reviewed.
4. Keep the cable connected and the same Bad USB script open until the **Pi**
   finishes. Flipper **Done means typing finished**, not installation finished.
   Changing script/app/mode or reconnecting can cancel maintenance.

Official sources: [script commands](https://developer.flipper.net/flipperzero/doxygen/badusb_file_format.html),
[USB/layout/file transfer](https://docs.flipper.net/zero/bad-usb), and
[run-on-connect implementation](https://github.com/flipperdevices/flipperzero-firmware/blob/dev/applications/main/bad_usb/helpers/ducky_script.c).
No custom Flipper firmware is required.

## Automatic sequence

Wait for USB; send Ctrl+Alt+F3; wait five seconds; type `pi`; wait 2.5 seconds;
type the supplied password; wait twenty seconds for the shell. Disable history
for this temporary shell, transfer a compact capsule in short lines, then run
it with Enter. There is no manual login or sudo confirmation on the printer.

Before sudo, check exact user/tty, require exactly one matching HID and capture
its port/VID/PID/serial/bus/enumeration. Download a SHA-256-pinned temporary
helper with USB checks during transfer. The helper fetches the tag-pinned
installer, optionally checks its additional SHA-256, and supplies sudo's password
through a private pipe. It does not guess when sudo prompts. The frozen USB
session passes through sudo into the root downloader/installer; intervening
re-enumeration refuses. The normal removal/cancellation guard remains in force.

No later keys enter a running installer. Output goes to console and
`/home/pi/printer_data/logs/autoprinter-console-*.log` (up to 8 MiB per log).
Root reports/backups remain under `/var/lib/autoprinter/`. After a started job
exits normally (including an ordinary refusal), the helper returns to an already
running KlipperScreen while the same USB trigger remains present. It queries
the GUI service and its real display-server process to identify the actual VT,
rechecks process identity/USB, then runs only chvt. It never starts a stopped GUI.
Cancellation, signals, missing/ambiguous GUI or an early bootstrap error leave
the console; the log records why. The job's exit result is retained.

The shell logs out immediately after the helper finishes, preparing tty3 for
the next insertion; there is no 15-second hold or later injected hotkey. Wait
for the Pi/GUI to finish, not Flipper Done. The temporary helper installs no
listener/startup task and does not automatically reboot/start printer services.
Release v0.6.1's automatic inventory login/download/run was exercised on the
real Flipper/Pi with exit 0; v0.6.2 fixes its missing display return. The new
completion-return sequence awaits the operator's next full insertion test.
Earlier optional peripheral-history rules are a separate accessory feature;
this launch mechanism neither adds nor depends on them.

## Generate private payloads

The login password is plaintext in the keystrokes and recoverable from the
capsule. Keep generated login files private; they are excluded from the public
release. The generator never prints the credential or encoded command.

On the preparation computer, put the `pi` password in a one-line local file.
From the extracted release (Windows may use `python` instead of `python3`):

```sh
python3 make_hid.py --device flipper --console-login --password-file pi-password.txt --mode inventory --output auto-inventory.txt
python3 make_hid.py --device flipper --console-login --password-file pi-password.txt --mode audit --output auto-audit.txt
python3 make_hid.py --device flipper --console-login --password-file pi-password.txt --mode peripherals --output auto-peripheral-plan.txt
```

`--console-login` includes automatic login and Enter. Default console is tty3;
`--vt 2` through `--vt 6` selects a different **known fresh** spare console.
Output files must not already exist. `--launcher-sha256` adds a pin for the
tag's install.sh; the ready private test kit includes that pin.
`--login-wait-ms` can increase the shell-startup wait for a slower image.

Accessory apply on an inspected native KIAUH installation:

```sh
python3 make_hid.py --device flipper --console-login --password-file pi-password.txt --mode peripherals --output auto-accessories.txt -- --apply --maintenance-confirmed --printer-profile auto
```

Klipper must already be inactive and at least 3 GiB free. Missing/ambiguous
printer type refuses; select a reviewed `manual-z`/`cartographer` when needed.
Ubuntu 26.04 additionally needs `--experimental-ubuntu-26-04`. This mode handles
conditional webcam/Cartographer/KlipperScreen and macro/settings files. It never
updates Klipper itself, flashes, or changes Wi-Fi.

Full maintenance requires a completed local machine manifest:

```sh
python3 make_hid.py --device flipper --console-login --password-file pi-password.txt --mode full --output auto-maintain.txt -- --config /home/pi/autoprinter/machine.json --apply --maintenance-confirmed --enable-flashing-capability --enable-wifi
```

OS/layout, Ethernet, storage, idle/isolation, file/version and board checks remain.
Missing settings/configs cause a reported refusal, not guessed pins/calibration
or an interactive question. The bench still lacks printer.cfg and currently has
less than 3 GiB free. Software/config changes may intentionally leave services
stopped for commissioning. Automatic launch does not assert display/machine
confirmations. `--enable-flashing-capability` alone never writes; actual writes
need `--flash` and [BOARD-FLASH.md](BOARD-FLASH.md)'s independent target/profile
gates. A healthy Klipper is held unless the requested board write verifies.
The bench's Ubuntu 26.04 is experimental in accessory mode; it remains outside
the full-stack OS allowlist.

## Trigger / ATtiny / existing-shell alternatives

Flipper scripts use `ID 1234:abcd AutoPrinter:Supervised launcher`, a local bench
selector based on the documentation's example. Match exact VID/PID/product,
optionally serial, and exactly one HID. Freeze its physical enumeration. USB
descriptors select presence, not authentication or MCU identity. Keep the trigger
separate from boards that reset; use properly assigned IDs in a product.

For ATtiny/Digispark the generator also accepts `--device digispark` together
with `--console-login`. It runs once on insertion and polls USB afterward,
without repeating. Use its actual keyboard VID/PID via `--vid/--pid`; upstream
defaults are 16c0:27db, which some clones/bootloaders change. The command chunks
use PROGMEM. Compile with the actual board/core/library and check flash/RAM;
automatic-login compilation and physical delivery remain pending. Use a larger
HID board if a particular ATtiny core has insufficient usable flash.

Earlier `hid/flipper/*-review.txt` and `*-run.txt` files remain **existing-shell**
alternatives. They do not log in. Without `--console-login` the generator still
defaults to manual Enter. Use the new private automatic-console files for the
requested no-keyboard workflow.

USB removal cannot instantly stop blocking kernel I/O, APT or a submitted flash
write. Interrupted firmware may need recovery. Maintain supervision and actual
actuator isolation for initial programming tests.

For v0.7.0 the generator also accepts `--mode klipper-update-only`. This is
the separate paired Klipper/Octopus transaction described in KLIPPER-UPDATE.md.
Default audit still writes no firmware; apply implies the board write and host
promotion, and requires capability, maintenance, isolated power, confirmed
Octopus/profile and bootloader-request flags. The private kit's 06/07 scripts
are tied to the inspected .170 H723 identity, not generic printer selectors.
Keep its USB supervision and normal screen-return helper through the entire
update/recovery. No monitor/worker is installed for later protocol errors.
