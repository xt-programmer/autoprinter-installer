# Conditional accessories on an existing KIAUH installation

`peripherals.py` adds accessories to a single printer with user `pi`, real native
`/home/pi/{klipper,klippy-env,moonraker,moonraker-env,KlipperScreen,crowsnest}`
directories and `~/printer_data`. KlipperScreen/Crowsnest directories can be
absent before initial setup. It does not prepare the base host or configure an MCU.

Read-only plan, directly on the Linux Pi:

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.3.0/install.sh | sudo sh -s -- --peripherals
```

For supervised SSH installation on the inspected Ubuntu 26.04 bench, from the
logged-in `pi` shell (the shell expands `$$` before entering sudo):

```sh
curl --proto '=https' --proto-redir '=https' -fsSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.3.0/install.sh | sudo sh -s -- --peripherals --apply --maintenance-confirmed --supervised-ssh --ssh-session-pid "$$" --experimental-ubuntu-26-04
```

For normal USB-supervised operation, replace the SSH flags with
`--usb-port YOUR_TRIGGER_PORT`; optional expected VID/PID/serial selectors are
supported. Use a separate trigger, not the camera/probe whose connection may
change. The downloader and installer both check its enumeration identity.

Apply refuses active/failed Klipper, unsupported hosts, non-native or custom
service layouts, mismatched source pins, broken APT state, conflicting updater
sections and less than 3 GiB free before starting. Stop Klipper as part of
supervised maintenance before apply. The script never stops/starts Klipper,
sends G-code, opens its MCU serial device, flashes, calibrates or fabricates
`printer.cfg`. Signals/session loss cancel command groups at checkpoints, with
the same blocking-I/O limitations as the main installer. No automatic rollback
or reboot follows interruption.

Behavior:

- KlipperScreen uses the upstream source, dependencies, native environment
  `~/.KlipperScreen-env`, service template and X11 start script. This avoids
  replacing networking with NetworkManager or installing optional language
  fonts/development-only mpv packages. The unit skips headless DRM startup;
  connect a display and `sudo systemctl start KlipperScreen` to test it. Legacy
  framebuffer-only displays need explicit review of the display-presence
  drop-in; they are not inferred from an HDMI connector.
- Crowsnest v5 installs only after positive USB video-class or retained UVC
  evidence. On the tested Orange Pi, the official installer builds ustreamer
  when Mainsail's APT repository lacks a matching package. One currently
  attached USB camera receives its stable `/dev/v4l/by-id/*-video-index0`
  capture path, 640x480 at 15 fps and `/webcam/` through the existing port-80
  proxy. SoC decoders and metadata nodes are excluded. Existing camera configs
  are preserved. Ambiguous/multiple cameras require manual configuration. A
  historical-only detection installs software without guessing `/dev/video0`.
- Cartographer installs only after explicit named USB manufacturer/product,
  serial-by-id or retained kernel evidence. Generic Klipper H723/F4 and STM32
  DFU identities are not probe detections. Follow the [official plugin
  procedure](https://docs.cartographer3d.com/cartographer-probe/installation-and-setup/software-configuration/klipper-setup/):
  run the reviewed vendor script with native paths, install its current stable
  PyPI package, and register its Python updater. The source script is pinned;
  its PyPI package/dependencies resolve at install time. NumPy is installed first
  to avoid the vendor script's old fallback on newer Python. Legacy scanner/
  IDM or non-scaffold Cartographer implementations refuse automatic migration.
  Configs, virtualenv and probe files are backed up before vendor cleanup.
  Firmware download/flash, probe offsets, pin settings, homing and calibration
  remain separate reviewed commissioning steps. CAN-only/unnamed devices cannot
  be identified safely by this passive USB detector and require integration.
- Positive detections persist in `/var/lib/autoprinter/device-history.json`.
  A small udev callback records future USB facts even between maintenance runs;
  it never installs software or talks to hardware. Unplugging does not erase
  evidence or working camera configuration. Corrupt history fails closed.
  Hardware never observed by the Pi and absent from its journal cannot be inferred.
- Backups, checksums and reports are in
  `/var/lib/autoprinter/peripherals-<timestamp>/`. The managed Moonraker include
  registers accessories without replacing the main configuration. Default
  audit creates neither persistent history nor udev rules. To stop passive
  recording, remove `/etc/udev/rules.d/70-autoprinter-device-history.rules` and
  run `sudo udevadm control --reload-rules`; keep the history for future use.

Success is `ACCESSORIES_INSTALLED_NOT_PRINTER_READY`. It checks Moonraker warnings,
Mainsail on port 80 and a complete camera JPEG when a camera is present. It does
not certify physical display output, firmware compatibility or printer safety.
The main full-stack maintenance path is separate; use this accessory mode to
apply the conditional detection policy to an already installed native host.
