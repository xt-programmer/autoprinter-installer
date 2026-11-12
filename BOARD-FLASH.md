# Octopus flash eligibility and host-update gate

The installer treats flash eligibility as a decision to investigate, not proof
that programming will succeed. A working installed Klipper host stays at its
current version unless the board transaction reaches **VERIFIED**. Missing or
corrupt entry points/environments can be repaired separately; after an unverified
flash the full-stack path only repairs a known original source revision and
leaves services stopped. Configuration or firmware-version mismatch alone does
not classify a host as corrupt.

The [Voron Octopus procedure](https://docs.vorondesign.com/build/software/octopus_klipper.html)
and [BTT profile](https://github.com/bigtreetech/BIGTREETECH-OCTOPUS-V1.0/blob/master/Firmware/Klipper/generic-bigtreetech-octopus.cfg)
give these USB settings, also explicitly selected by the operator:

| MCU | Bootloader offset | Crystal | Architecture / interface |
| --- | --- | --- | --- |
| STM32F446 | 32 KiB | 12 MHz | STM32 / USB PA11, PA12 |
| STM32F429 | 32 KiB | 8 MHz | STM32 / USB PA11, PA12 |
| STM32H723 | 128 KiB | 25 MHz | STM32 / USB PA11, PA12 |

`build_firmware.py` runs **make clean**, writes those menuconfig-equivalent
settings non-interactively, runs **make olddefconfig**, validates the normalized
MCU/offset/crystal/interface/chip-derived USB serial and no startup GPIO outputs,
then runs **make**. It validates the image/vector table and saves the build
config, protocol dictionary, source commit, compiler and hashes. An interactive
menu is not required for repeatable supervised automation. The real Kconfig
offset symbols are hexadecimal suffixes `_8000` and `_20000`; decimal suffixes
`_32768`/`_131072` are invalid. This corrects the earlier profile check.

The full-stack mode builds its pinned source locally. The separate artifact-only
mode can use release-pinned builds from `firmware-bundles/`; all three profiles
compiled successfully in the isolated CI build linked in that directory. Those
images are pinned builds, not automatically updated upstream binaries. An
external bundle requires an explicitly supplied expected firmware SHA-256.

Read-only eligibility audit, replacing the target port/serial with the inspected
Octopus values (do not use a probe's port/serial):

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.5.0/install.sh | sudo sh -s -- --board-flash --chip stm32h723xx --target-port TARGET_PORT --target-serial TARGET_SERIAL
```

Actual programming additionally requires **all** of:

```text
--apply --flash --enable-flashing-capability --maintenance-confirmed
--actuator-power-disconnected --board-confirmed-octopus
```

Use a separate USB trigger with `--usb-port ...` or an explicitly supervised pi
SSH shell with `--supervised-ssh --ssh-session-pid "$$"`. A normal running Klipper
USB target additionally needs `--allow-bootloader-request`. Add
`--prepare-host-update` to stage and promote a matching native host source only
after success. That artifact-mode promotion requires a healthy existing host,
unchanged dependency requirements and no unsupported external source plugins;
otherwise it refuses. It preserves native real directories, units and the
existing environment, and retains the original source tree. The general
full-stack repair path handles missing/corrupt host environments separately.

The artifact mode checks the supported Raspberry/Orange Pi hardware, requires
3 GiB at transaction entry and reserves at least
500 MiB during staged work. It does not install a compiler or upgrade packages.
An already-inactive Klipper service, exact selected board identity and isolated
actuator/heater power are required. UART, Katapult, guessed GPIO/bootloader
offsets and arbitrary STM32 DFU devices are not handled by this transaction.
ROM DFU VID:PID alone does not identify a chip; an initially DFU/unflashed board
needs the operator's verified physical Octopus/MCU selection. Stock bootloader
presence is checked; blank MCUs without a bootloader need a separate reviewed
bootloader procedure, not an application image at an invented zero offset.

Transaction order:

1. Match exact physical USB port and serial. Reject unrelated/probe descriptors.
   Check the build artifact's hash, profile, vectors and source pin. Take the
   shared lock and verify inactive Klipper. Stage a candidate without changing
   installed Klipper or its dependency environment.
2. If starting in normal mode, identify the live MCU without loading printer.cfg
   or configuring outputs. Request ROM bootloader with Klipper's documented
   virtual-serial transition. Open with O_NOCTTY and ignore the expected HUP.
   Independently require ROM DFU at the same physical port; command exit alone
   never proves the transition. A missing/unsupported transition skips writes.
3. Bind dfu-util to **DFU-only VID:PID + physical port + exact DFU serial + alt 0**.
   The DFU serial can differ from the normal Klipper serial. Check Internal Flash
   layout, range, erase-sector alignment and read/erase/write permissions.
4. Read the entire described internal flash into a backup. Check its size/hash
   and bootloader vector. Protected/unreadable/invalid targets refuse a write.
5. Record WRITE_STARTED. Program only the application at the selected offset,
   without mass erase, unprotect, option-byte writes, force flags or leaving DFU.
   A programming error stops here: no retry, host promotion or dependent update.
6. Read internal flash back. Require exact application bytes and unchanged
   bootloader plus sectors outside the application erase range. Otherwise stop.
7. Leave DFU in command mode, without downloading again. Wait for both normal
   USB enumeration AND the stable udev serial link. Require the live protocol
   dictionary to equal the built dictionary.
8. Explicitly reset only that MCU after checking the built dictionary hash.
   Require re-enumeration and the same live dictionary after reset. This checks
   reset/boot persistence, not a physical power-cycle test.
9. Mark VERIFIED. Only now may the matching host source/environment be promoted.
   The full-stack path does this before other stack/config staging; artifact
   mode promotes only when requested. Later dependency failures cannot cause
   an unverified firmware to authorize host activation.

No services are automatically restarted by artifact mode. An application
programming failure can leave the MCU requiring recovery; keeping old host
software does not undo an incomplete firmware write. Original flash bytes,
hashes, source retention and promotion intent remain available. There is no
automatic firmware rollback or repeated download after failure/removal.

Hardware validation: one H723 Octopus Pro application write was made on the
USB-powered Orange Pi bench. Full flash backup/read-back, bootloader preservation,
normal identity and a subsequent MCU reset all passed. Source commit
`29d4ec2d6d840ee839333306003a400da186d41d` was promoted only after those checks.
The attempt exposed HUP handling and a udev-link race; verification was completed
without a second application download, and regression tests cover both issues.
The corrected one-shot CLI still needs a clean-run hardware test. F446/F429 have
compile/policy tests but no physical flash test here. Printer configuration,
motor/heater commissioning and physical power-cycle testing remain separate.
