"""Portable safety-policy tests. No real printer, packages, network or root needed."""
import copy
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import autoprinter as app


class Guard:
    def __init__(self):
        self.cancelled = threading.Event()
        self.device = {"port": "1-1"}

    def check(self):
        if self.cancelled.is_set():
            raise app.Removed("test removal")


def idle():
    return {
        "webhooks": {"state": "ready"}, "print_stats": {"state": "standby"},
        "pause_resume": {"is_paused": False}, "virtual_sdcard": {"is_active": False},
        "idle_timeout": {"state": "Idle"}, "configfile": {"save_config_pending": False},
        "motion_report": {"live_velocity": 0.0, "live_extruder_velocity": 0.0},
        "heaters": {"available_heaters": ["extruder", "heater_bed", "heater_generic chamber"]},
        "extruder": {"target": 0, "power": 0, "temperature": 25},
        "heater_bed": {"target": 0, "power": 0, "temperature": 24},
        "heater_generic chamber": {"target": 0, "power": 0, "temperature": 23}}


class IdlePolicyTests(unittest.TestCase):
    def test_fully_known_idle_allowed(self):
        app.safe_idle(idle())

    def test_active_paused_error_unknown_print_blocked(self):
        for state in ("printing", "paused", "error", "unknown", None):
            with self.subTest(state=state):
                s = idle()
                s["print_stats"]["state"] = state
                with self.assertRaises(app.Refuse):
                    app.safe_idle(s)

    def test_missing_every_safety_object_blocks(self):
        for key in idle():
            s = idle()
            del s[key]
            with self.subTest(key=key), self.assertRaises(app.Refuse):
                app.safe_idle(s)

    def test_hidden_chamber_heater_blocks(self):
        for field, value in (("target", 35), ("power", .1), ("temperature", 51)):
            s = idle()
            s["heater_generic chamber"][field] = value
            with self.subTest(field=field), self.assertRaises(app.Refuse):
                app.safe_idle(s)

    def test_missing_or_nonfinite_heater_data_blocks(self):
        for value in (None, "0", float("nan"), float("inf"), True):
            s = idle()
            s["extruder"]["target"] = value
            with self.subTest(value=value), self.assertRaises(app.Refuse):
                app.safe_idle(s)

    def test_unsaved_calibration_blocks(self):
        s = idle()
        s["configfile"]["save_config_pending"] = True
        with self.assertRaises(app.Refuse):
            app.safe_idle(s)

    def test_motion_sd_pause_block(self):
        for obj, field, value in (("motion_report", "live_velocity", .01),
                                   ("motion_report", "live_extruder_velocity", .01),
                                   ("virtual_sdcard", "is_active", True),
                                   ("pause_resume", "is_paused", True),
                                   ("idle_timeout", "state", "Printing")):
            s = idle()
            s[obj][field] = value
            with self.subTest(obj=obj, field=field), self.assertRaises(app.Refuse):
                app.safe_idle(s)


class USBTests(unittest.TestCase):
    def device(self, **kwargs):
        d = {"path": "unused", "port": "1-1", "vid": "0483", "pid": "df11", "serial": "ABC", "busnum": "1", "devnum": "8"}
        d.update(kwargs)
        return d

    def test_dfu_vid_pid_alone_never_selects(self):
        with self.assertRaises(app.Refuse):
            app.select_usb([self.device()], {"vid": "0483", "pid": "df11"})

    def test_physical_port_and_serial_selects_one(self):
        d = self.device()
        selected = app.select_usb([d, self.device(port="1-2", serial="DEF")], {"port": "1-1", "vid": "0483", "pid": "df11", "serial": "ABC"})
        self.assertEqual(selected, d)

    def test_ambiguous_usb_never_selects(self):
        with self.assertRaises(app.Refuse):
            app.select_usb([self.device(), self.device()], {"port": "1-1", "vid": "0483", "pid": "df11"})

    def test_wrong_serial_blocks(self):
        with self.assertRaises(app.Refuse):
            app.select_usb([self.device()], {"port": "1-1", "vid": "0483", "pid": "df11", "serial": "WRONG"})

    def test_replug_latches_cancellation(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            d = self.device(path=t)
            for key, value in (("idVendor", "0483"), ("idProduct", "df11"), ("serial", "ABC"), ("busnum", "1"), ("devnum", "8")):
                (p / key).write_text(value)
            guard = app.USBGuard(d)
            guard.check()
            (p / "devnum").write_text("9")
            with self.assertRaises(app.Removed):
                guard.check()
            (p / "devnum").write_text("8")
            with self.assertRaises(app.Removed):
                guard.check()

    def test_background_watcher_detects_removal(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            d = self.device(path=t)
            for key, value in (("idVendor", "0483"), ("idProduct", "df11"), ("serial", "ABC"), ("busnum", "1"), ("devnum", "8")):
                (p / key).write_text(value)
            guard = app.USBGuard(d, poll=.01).start()
            try:
                (p / "idVendor").unlink()
                self.assertTrue(guard.cancelled.wait(.5))
            finally:
                guard.close()


class FilePolicyTests(unittest.TestCase):
    def test_checksum_rejects_mutable_refs(self):
        for digest in ("master", "main", "HEAD", None, "a" * 39, "A" * 40):
            with self.subTest(digest=digest), self.assertRaises(app.Refuse):
                app.require_digest(digest, 40)

    def test_atomic_write_refuses_cancelled_guard(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "config"
            p.write_text("old")
            g = Guard()
            g.cancelled.set()
            with self.assertRaises(app.Removed):
                app.atomic_write(p, "new", g)
            self.assertEqual(p.read_text(), "old")

    @unittest.skipIf(os.name == "nt", "POSIX directory fsync is not available on Windows")
    def test_atomic_write_success(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "config"
            p.write_text("old")
            app.atomic_write(p, "new", Guard())
            self.assertEqual(p.read_text(), "new")

    def test_zip_path_traversal_rejected(self):
        for name in ("../escape", "/etc/passwd", "a\\..\\escape", "C:/escape"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as t:
                p = Path(t)
                with zipfile.ZipFile(p / "bad.zip", "w") as z:
                    z.writestr(name, "bad")
                dest = p / "dest"
                dest.mkdir()
                with self.assertRaises(app.Refuse):
                    app.safe_extract_zip(p / "bad.zip", dest, Guard())

    def test_zip_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            with zipfile.ZipFile(p / "bad.zip", "w") as z:
                entry = zipfile.ZipInfo("link")
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(entry, "/etc/passwd")
            with self.assertRaises(app.Refuse):
                app.safe_extract_zip(p / "bad.zip", p / "dest", Guard())

    def test_valid_zip_extracted(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            with zipfile.ZipFile(p / "good.zip", "w") as z:
                z.writestr("assets/main.js", "okay")
                z.writestr("index.html", "okay")
            app.safe_extract_zip(p / "good.zip", p / "dest", Guard())
            self.assertEqual((p / "dest/assets/main.js").read_text(), "okay")

    def test_zip_size_limit_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            with zipfile.ZipFile(p / "bad.zip", "w") as z:
                z.writestr("large", "x" * 20)
            with self.assertRaises(app.Refuse):
                app.safe_extract_zip(p / "bad.zip", p / "dest", Guard(), limit=10)

    def test_prune_only_old_gcode_direct_children(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            now = time.time()
            for name in ("old.gcode", "fresh.gcode", "old.cfg", "old.log"):
                f = p / name
                f.write_text("data")
                timestamp = now if name.startswith("fresh") else now - 100 * 86400
                os.utime(f, (timestamp, timestamp))
            nested = p / "subdir"
            nested.mkdir()
            (nested / "job.gcode").write_text("data")
            self.assertEqual([x.name for x in app.prune_candidates(p, now=now)], ["old.gcode"])

    def test_hardlinked_gcode_is_retained(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            f = p / "old.gcode"
            f.write_text("data")
            os.utime(f, (1, 1))
            os.link(f, p / "other.gcode")
            self.assertEqual(app.prune_candidates(p), [])

    def test_low_space_gate_for_each_filesystem(self):
        usage = Mock(free=app.GIB)
        with patch.object(app.shutil, "disk_usage", return_value=usage), self.assertRaises(app.Refuse):
            app.check_free([Path.cwd()])


class InstallerPolicyTests(unittest.TestCase):
    def installer(self, argv=None, config=None):
        args = app.arguments(argv or ["--apply", "--maintenance-confirmed"])
        return app.Installer(args, config or {}, Guard())

    def test_flash_requires_verified_profile(self):
        inst = self.installer(["--apply", "--flash"])
        with self.assertRaises(app.Refuse):
            inst.flash_profile()

    def flash_setup(self, root):
        root = Path(root)
        config = root / "octopus.config"
        config.write_text('CONFIG_MCU="stm32f446xx"\nCONFIG_MACH_STM32=y\nCONFIG_USBSERIAL=y\nCONFIG_STM32_FLASH_START_32768=y\nCONFIG_STM32_CLOCK_REF_12M=y\n')
        profile = {"board": "octopus-1.1", "mcu": "stm32f446xx", "verified_by": "test-fixture",
                   "recovery_instructions": "test-only", "bootloader_kib": 32,
                   "flash_address": "0x08008000", "clock_config": "CONFIG_STM32_CLOCK_REF_12M=y",
                   "build_config": "octopus.config", "build_config_sha256": app.sha256(config),
                   "dfu_usb": {"port": "1-2", "vid": "0483", "pid": "df11", "serial": "DFU123"},
                   "normal_usb": {"port": "1-2", "vid": "1d50", "pid": "614e", "serial": "NORMAL123"}}
        inst = self.installer(["--config", str(root / "manifest.json"), "--apply", "--flash"], {"octopus": profile})
        device = dict(profile["dfu_usb"], path="unused", busnum="1", devnum="7")
        return inst, device

    def test_verified_exact_flash_profile_accepts_without_flashing(self):
        with tempfile.TemporaryDirectory() as t:
            inst, d = self.flash_setup(t)
            with patch.object(app, "usb_devices", return_value=[d]):
                p, selected, config = inst.flash_profile()
            self.assertEqual(selected["serial"], "DFU123")
            self.assertEqual(p["flash_address"], "0x08008000")

    def test_bad_flash_offset_chip_or_serial_blocks(self):
        for field, value in (("bootloader_kib", 0), ("flash_address", "0x08000000"), ("mcu", "stm32h723xx")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as t:
                inst, d = self.flash_setup(t)
                inst.c["octopus"][field] = value
                with patch.object(app, "usb_devices", return_value=[d]), self.assertRaises(app.Refuse):
                    inst.flash_profile()

    def test_dfu_target_without_explicit_serial_blocked(self):
        with tempfile.TemporaryDirectory() as t:
            inst, d = self.flash_setup(t)
            del inst.c["octopus"]["dfu_usb"]["serial"]
            with patch.object(app, "usb_devices", return_value=[d]), self.assertRaises(app.Refuse):
                inst.flash_profile()

    def test_trigger_cannot_be_flash_target(self):
        with tempfile.TemporaryDirectory() as t:
            inst, d = self.flash_setup(t)
            inst.guard.device["port"] = "1-2"
            with patch.object(app, "usb_devices", return_value=[d]), self.assertRaises(app.Refuse):
                inst.flash_profile()

    def test_ambiguous_dfu_targets_blocked(self):
        with tempfile.TemporaryDirectory() as t:
            inst, d = self.flash_setup(t)
            with patch.object(app, "usb_devices", return_value=[d, d]), self.assertRaises(app.Refuse):
                inst.flash_profile()

    def test_low_space_cannot_reach_package_mutation(self):
        inst = self.installer()
        for method in ("validate_layout", "validate_manifest", "ethernet_check", "idle_check", "setup_state", "install_packages"):
            setattr(inst, method, Mock())
        with patch.object(app.os, "geteuid", return_value=0, create=True), patch.object(app, "supported_host"), patch.object(app, "check_free", side_effect=app.Refuse("disk full")):
            with self.assertRaises(app.Refuse):
                inst.apply({})
        inst.setup_state.assert_not_called()
        inst.install_packages.assert_not_called()

    def test_unknown_printer_state_cannot_reach_mutation(self):
        inst = self.installer()
        for method in ("validate_layout", "validate_manifest", "ethernet_check", "setup_state", "install_packages"):
            setattr(inst, method, Mock())
        inst.idle_check = Mock(side_effect=app.Refuse("no Moonraker"))
        with patch.object(app.os, "geteuid", return_value=0, create=True), patch.object(app, "supported_host"), patch.object(app, "check_free"):
            with self.assertRaises(app.Refuse):
                inst.apply({})
        inst.setup_state.assert_not_called()
        inst.install_packages.assert_not_called()

    def test_recovery_requires_physical_power_assertion(self):
        inst = self.installer(["--apply", "--maintenance-confirmed", "--recovery-isolated", "--flash"])
        inst.report["existing_installation"] = True
        for method in ("validate_layout", "validate_manifest", "ethernet_check", "setup_state"):
            setattr(inst, method, Mock())
        with patch.object(app.os, "geteuid", return_value=0, create=True), patch.object(app, "supported_host"), patch.object(app, "check_free"):
            with self.assertRaises(app.Refuse):
                inst.apply({})
        inst.setup_state.assert_not_called()

    def test_recovery_cannot_override_known_printing_state(self):
        inst = self.installer(["--apply", "--maintenance-confirmed", "--recovery-isolated", "--flash", "--actuator-power-disconnected"])
        inst.snapshot = idle()
        inst.snapshot["print_stats"]["state"] = "printing"
        inst.report["existing_installation"] = True
        for method in ("validate_layout", "validate_manifest", "ethernet_check", "setup_state"):
            setattr(inst, method, Mock())
        with patch.object(app.os, "geteuid", return_value=0, create=True), patch.object(app, "supported_host"), patch.object(app, "check_free"):
            with self.assertRaises(app.Refuse):
                inst.apply({})
        inst.setup_state.assert_not_called()

    def test_bundle_file_traversal_and_digest_block(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            inst = self.installer(["--config", str(p / "manifest.json")])
            f = p / "printer.cfg"
            f.write_text("data")
            self.assertEqual(inst.bundle_file("printer.cfg", app.sha256(f)).resolve(), f.resolve())
            with self.assertRaises(app.Refuse):
                inst.bundle_file("printer.cfg", "0" * 64)
            with self.assertRaises(app.Refuse):
                inst.bundle_file("../escape", "0" * 64)

    def test_different_existing_printer_config_never_overwritten(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            (p / "config").mkdir()
            (p / "config/printer.cfg").write_text("original")
            (p / "supplied.cfg").write_text("new")
            inst = self.installer(["--config", str(p / "manifest.json")], {"config_files": {"printer.cfg": {"file": "supplied.cfg", "sha256": app.sha256(p / "supplied.cfg")}}})
            inst.data = p
            inst.report["required_files"] = {"printer.cfg": True}
            with self.assertRaises(app.Refuse):
                inst.validate_configs()
            self.assertEqual((p / "config/printer.cfg").read_text(), "original")

    def test_unsupported_host_and_arch_block(self):
        with self.assertRaises(app.Refuse):
            app.supported_host({"model": "Random ARM board", "machine": "aarch64", "os": {"ID": "debian", "VERSION_ID": "12"}})

    def test_remote_api_url_blocked(self):
        for url in ("http://192.168.1.2:7125", "https://127.0.0.1:7125", "http://user@localhost:7125", "http://localhost:7125/path"):
            with self.subTest(url=url), self.assertRaises(app.Refuse):
                app.api_base(url)

    def test_valid_local_api(self):
        self.assertEqual(app.api_base("http://127.0.0.1:7125/"), "http://127.0.0.1:7125")

    def test_guarded_runner_cancels_child_group(self):
        g = Guard()
        runner = app.Runner(g, emit=lambda text: None)
        process = Mock()
        process.poll.return_value = None
        def pread(*args):
            g.cancelled.set()
            return b""
        with patch.object(app.subprocess, "Popen", return_value=process), patch.object(app.os, "pread", side_effect=pread, create=True), patch.object(app, "kill_group") as kill:
            with self.assertRaises(app.Removed):
                runner.run(["fake-command"])
        kill.assert_called_once_with(process)

    def test_cancelled_runner_never_starts_command(self):
        g = Guard()
        g.cancelled.set()
        with patch.object(app.subprocess, "Popen") as popen, self.assertRaises(app.Removed):
            app.Runner(g).run(["fake-command"])
        popen.assert_not_called()

    def test_removal_prevents_failure_restart_or_stop(self):
        inst = self.installer()
        inst.stopped = True
        inst.guard.cancelled.set()
        inst.command = Mock()
        with self.assertRaises(app.Removed):
            inst.quiesce_on_failure()
        inst.command.assert_not_called()

    def test_cli_default_is_audit(self):
        args = app.arguments([])
        self.assertFalse(args.apply)
        self.assertFalse(args.verify)
        self.assertFalse(args.flash)

    def test_windows_runtime_refuses_mutations(self):
        with patch.object(app.platform, "system", return_value="Windows"), patch("sys.stderr", io.StringIO()):
            self.assertEqual(app.main(["--apply"]), 2)

    @unittest.skipUnless(sys.platform == "linux", "Requires real Linux process groups and pread")
    def test_real_running_command_is_stopped_after_removal(self):
        with tempfile.TemporaryDirectory() as t:
            output = Path(t) / "marker"
            g = Guard()
            program = "import sys,time\nf=open(sys.argv[1],'a',buffering=1)\nwhile True:\n f.write('x\\n')\n time.sleep(.02)\n"
            timer = threading.Timer(.3, g.cancelled.set)
            timer.start()
            try:
                with self.assertRaises(app.Removed):
                    app.Runner(g, emit=lambda x: None).run([sys.executable, "-c", program, str(output)], timeout=3)
            finally:
                timer.cancel()
            before = output.stat().st_size
            time.sleep(.1)
            self.assertEqual(output.stat().st_size, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
