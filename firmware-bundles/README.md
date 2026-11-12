# Release-pinned Octopus firmware

These three USB images were built from Klipper commit
`29d4ec2d6d840ee839333306003a400da186d41d` using the supplied build_firmware.py
and reviewed F446/F429/H723 profiles. Each directory includes its normalized
menuconfig settings, protocol dictionary, image hashes and compiler identity.

Build provenance: https://github.com/xt-programmer/autoprinter-installer/actions/runs/43201247770

Corresponding upstream source:
https://github.com/Klipper3d/klipper/tree/29d4ec2d6d840ee839333306003a400da186d41d
and downloadable source archive:
https://github.com/Klipper3d/klipper/archive/29d4ec2d6d840ee839333306003a400da186d41d.tar.gz

The v0.5.0 release also provides the corresponding source alongside its binary
assets as `klipper-source-29d4ec2.zip`. Build scripts and exact configurations
are in the installer archive and this directory.

Klipper and these firmware images are licensed under GNU GPLv3; see COPYING
and the corresponding source. The checked-in build profiles and scripts provide
the configuration used for these images. The standalone builder also supports
building another explicitly pinned upstream checkout.

These are Octopus-family USB application images, not universal STM32 images or
bootloader installers. A selected physical board, verified MCU/profile, actuator
power isolation and the script's target/backup/read-back/boot checks are required.
An image is never selected solely by generic STM32 DFU VID:PID. A release-pinned
artifact is not an automatically updated latest firmware download.
