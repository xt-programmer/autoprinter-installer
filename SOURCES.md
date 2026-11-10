# Primary references

Implementation references checked while preparing v0.2 on 10 November 2026.
These links may evolve; actual installation uses manifest commit/release pins.

- [Klipper installation](https://www.klipper3d.org/Installation.html)
- [Klipper status reference](https://www.klipper3d.org/Status_Reference.html) — heater inventory, state, motion, MCU constants and unsaved config status
- [Klipper bootloader entry](https://www.klipper3d.org/Bootloader_Entry.html)
- [Klipper STM32 Kconfig](https://github.com/Klipper3d/klipper/blob/master/src/stm32/Kconfig)
- [Klipper Makefile](https://github.com/Klipper3d/klipper/blob/master/Makefile)
- [Octopus Pro v1.1 upstream example](https://github.com/Klipper3d/klipper/blob/master/config/generic-bigtreetech-octopus-pro-v1.1.cfg) — demonstrates revision-specific settings; not used as a generic printer config
- [Moonraker installation](https://moonraker.readthedocs.io/en/latest/installation/)
- [Moonraker source and API docs](https://github.com/Arksine/moonraker/tree/master/docs)
- [KlipperScreen installation](https://github.com/KlipperScreen/KlipperScreen/blob/master/docs/Installation.md)
- [KlipperScreen X11/service start script](https://github.com/KlipperScreen/KlipperScreen/blob/master/scripts/KlipperScreen-start.sh)
- [Mainsail releases](https://github.com/mainsail-crew/mainsail/releases)
- [Cartographer current Klipper setup](https://docs.cartographer3d.com/cartographer-probe/installation-and-setup/software-configuration/klipper-setup/)
- [Cartographer installer source](https://github.com/Cartographer3D/cartographer3d-plugin/blob/main/scripts/install.sh)
- [NetworkManager nmcli reference](https://networkmanager.dev/docs/api/latest/nmcli.html) — optional Wi-Fi radio enable using the existing manager
- [GitHub CLI repository creation](https://cli.github.com/manual/gh_repo_create)
- [GitHub CLI release creation](https://cli.github.com/manual/gh_release_create)

The current Cartographer plugin requires a migration from legacy scanner config;
v0.2 refuses that migration rather than deleting configuration. Bootloader/MCU
settings must be verified for each physical board; an upstream example for a
different revision is not adequate evidence.
