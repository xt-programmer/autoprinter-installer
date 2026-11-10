Put verified machine configuration files and the full Klipper `make menuconfig`
generated `.config` here. No guessed board pin assignments or MCU images are shipped.

For each supplied config add an entry to `config_files` in your manifest, for example:

```json
"printer.cfg": {
  "file": "profiles/printer.cfg",
  "sha256": "THE_ACTUAL_64_CHARACTER_SHA256"
}
```

The destination filename is exactly the key. `macros500` and `settings500` are
extensionless because activity.txt names them that way. If you mean `.cfg` files,
change `required_config` and the corresponding keys. Their includes, macro
definitions, wiring, sensor types and calibration are machine-specific.

For DFU fill every `octopus` field in manifest.json using the actual inspected
board, its known working `.config`, and its USB inventory in normal and ROM DFU
modes. Use `sha256sum profiles/octopus.config` on Linux or
`Get-FileHash .\profiles\octopus.config -Algorithm SHA256` on Windows; lowercase
the resulting digest. See README.md for flash restrictions and recovery.
