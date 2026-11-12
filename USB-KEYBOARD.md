# Run AutoPrinter from a Flipper Zero or ATtiny keyboard

Use a **direct USB connection to the target Pi**, a monitor, and an already
logged-in `pi` terminal for the first tests. This launcher types the pinned
v0.5.0 Linux download command; the Pi downloads and runs the installer. The
keyboard does not carry the installer files. Ethernet/internet, Python 3.9+,
curl, bash and working sudo must already be available.

A keyboard cannot log in to an unknown image, inspect the screen, discover a
password, or verify heater/motor isolation. There is no login automation, stored
password, autorun service, reboot/resume task or background installation here.
The public manifest still needs machine-specific files/settings before a full
installation. Successful inventory is not a complete installation.

## First Flipper tests

1. Copy `hid/flipper/*.txt` from the release to the Flipper's **SD Card/badusb/**
   using qFlipper or the mobile app. Close qFlipper before using Bad USB.
2. On the Pi, log in as `pi`, open a terminal/console, and manually run `sudo -v`.
   Enter the Pi's password yourself if asked. Leave a clean shell prompt focused.
   The payload uses `sudo -n`, which refuses expired/missing authorization instead
   of typing into a password prompt. Repeat `sudo -v` before each later test.
3. On Flipper select **Bad USB → 01-inventory-review.txt**, choose **USB** mode,
   and match its keyboard layout to the Pi's active layout. Use a data USB cable
   into the Pi's USB host port, then press **Run**.
4. It waits two seconds, types slowly, and **does not press Enter**. Check that the
   command begins `bash -o pipefail -c`, has the v0.5.0 GitHub URL, and ends with
   the exit-status display. Check quotes, slashes, hyphens and pipe characters.
   If wrong, clear the line and correct the layout. If correct, press Enter on
   a normal keyboard. Inventory downloads code and reads hardware; it does not
   install packages, change configs/services/radios, or interact with MCU serial.
5. After inventory finishes, run **02-audit-review.txt** the same way. The
   downloader must report `HID trigger resolved to physical port ...`. It binds
   the exact device enumeration for the audit. **03-peripherals-plan-review.txt**
   then shows current/historical webcam, Cartographer and config decisions.
6. Keep the Flipper connected and the selected Bad USB script open until the
   **Pi** reports the result and returns to its shell. A Flipper “Done” means
   typing finished; it does not mean installation finished. Leaving the app,
   selecting another script, reconnecting or changing USB mode can cancel work.

`01-inventory-run.txt` and `02-audit-run.txt` send Enter once. Use these only
after the corresponding review test works. They never inject another command
after starting the installer. There is no fixed “wait 30 minutes then reboot.”

These are the official Flipper [text script commands](https://developer.flipper.net/flipperzero/doxygen/badusb_file_format.html)
and [file transfer/USB/layout steps](https://docs.flipper.net/zero/bad-usb).
Physical Flipper and ATtiny keystroke execution remains a bench test; automated
tests check payload generation and Linux USB-selection/cancellation policies.

## The trigger and its limits

Every supplied Flipper script starts with:

```text
ID 1234:abcd AutoPrinter:Supervised launcher
```

This is a local bench selector using the documentation's example VID/PID, not a
registered product assignment or authentication credential. Maintenance commands
match `--hid-vid 1234 --hid-pid abcd --hid-product 'Supervised launcher'` and require
**exactly one connected matching HID interface**. No match or multiple matches
refuses before download. Optional `--hid-serial` adds an exact serial match.
For a deployed custom device use its properly assigned USB IDs.

The selected trigger's physical port, VID, PID, serial, bus and enumeration
number are frozen and passed into the existing removal guard. Unplug/replug
latches cancellation. HID discovery selects only the maintenance trigger;
Octopus selection remains an independently explicit port/serial/chip decision.
USB descriptors can be imitated; supervision comes from the operator.

Use only one launcher at a time. A webcam/probe/Octopus that may reset is not a
maintenance trigger. Removal cannot guarantee an instantaneous stop of blocking
kernel I/O or an already submitted firmware write. Keep actuator power isolated
for the initial programming tests and retain the recovery backups.

## Generate a reviewed installation payload

Run `make_hid.py` on the preparation computer from the extracted release. On
Windows use `python` instead of `python3`. Output paths must not already exist.
Default output types the command without Enter. Add `--execute` to send it once.

Accessory apply example for a reviewed **manual-Z** native KIAUH installation:

```sh
python3 make_hid.py --device flipper --mode peripherals --output accessory-apply.txt -- --apply --maintenance-confirmed --printer-profile manual-z
```

Choose `cartographer` for a reviewed Cartographer printer, or `auto` when its
existing config is unambiguous. Accessory apply requires inactive Klipper and
at least 3 GiB free. Ubuntu 26.04 additionally needs the explicit experimental
flag `--experimental-ubuntu-26-04`. Webcam/Cartographer conditions still apply;
this mode never flashes, updates Klipper itself or changes Wi-Fi.

Full installation with a completed local machine manifest:

```sh
python3 make_hid.py --device flipper --mode full --output machine-apply.txt -- --config /home/pi/autoprinter/machine.json --apply --maintenance-confirmed --enable-flashing-capability --enable-wifi
```

Capability alone does not request flashing. In full mode a healthy installed
Klipper is retained unless a requested board flash reaches VERIFIED. Firmware
programming requires `--flash` plus the complete reviewed board profile and
isolation/safety gates. See [BOARD-FLASH.md](BOARD-FLASH.md). Do not reuse one
printer's board serial or pin configuration on another printer.

Artifact-mode flash audit example (replace the inspected Octopus values):

```sh
python3 make_hid.py --device flipper --mode board-flash --output board-audit.txt -- --chip stm32h723xx --target-port TARGET_PORT --target-serial TARGET_SERIAL
```

No generated payload supplies actuator/isolation confirmations for you. For an
actual write add all flags documented in BOARD-FLASH.md yourself. There is no
generic “flash any board attached” payload.

## ATtiny85 / Digispark

`hid/digispark/AutoPrinterInventory/AutoPrinterInventory.ino` is a one-shot
DigiKeyboard sketch. It waits five seconds, types inventory, leaves Enter to
the operator, then continues polling USB without typing again. The command is
stored in AVR flash with PROGMEM rather than the ATtiny85's small RAM.

Generate an audit sketch with the **actual running keyboard** firmware IDs:

```sh
python3 make_hid.py --device digispark --mode audit --vid 16c0 --pid 27db --output AutoPrinterAudit.ino
```

The defaults match the upstream [Digispark keyboard usbconfig](https://github.com/digistump/DigisparkArduinoIntegration/blob/master/libraries/DigisparkKeyboard/usbconfig.h);
clones/other libraries may differ. Use inventory to inspect yours. The bootloader
USB identity can differ from the keyboard identity. Compile/upload using the
matching Digispark board/core and DigiKeyboard library; verify flash/RAM usage
and US keyboard mapping. No compiled ATtiny firmware is supplied or claimed
tested here. A plain ATtiny without suitable USB hardware/firmware is not a USB
keyboard, and neither a Digispark nor Flipper Bad USB acts as installer storage.

## Headless Pis

Bad USB needs a focused, logged-in console on the same Pi. Plugging Flipper into
a laptop while its SSH window is focused does not put that HID on the Pi; the
HID selector correctly refuses there. For a headless first test, manually run
the normal inventory command over SSH. Specialized apply modes support the
separate explicit `--supervised-ssh --ssh-session-pid "$$"` guard, as documented
in their guides. Do not combine SSH supervision with HID/USB selectors. Prepare
a console once on a cloned image before attempting a keyboard-only workflow.
