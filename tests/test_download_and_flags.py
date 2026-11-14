"""Download entry point and opt-in policy tests; no network or printer needed."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import autoprinter as app
import bootstrap
import release


class NewFlagTests(unittest.TestCase):
    def installer(self, flags=(), config=None):
        from test_safety import Guard
        return app.Installer(app.arguments(list(flags)), config or {}, Guard())

    def test_capability_does_not_request_flash(self):
        args = app.arguments(["--enable-flashing-capability"])
        self.assertTrue(args.enable_flashing_capability)
        self.assertFalse(args.flash)
        self.assertFalse(args.apply)

    def test_write_requires_separate_enable(self):
        with patch.object(app.platform, "system", return_value="Linux"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(["--apply", "--flash"]), 2)
        inst = self.installer(["--apply", "--flash"])
        inst.command = Mock()
        with self.assertRaises(app.Refuse):
            inst.flash(Path("unused"))
        inst.command.assert_not_called()

    def test_default_wifi_disabled_flag_overrides_manifest(self):
        self.assertFalse(self.installer().wifi_enabled())
        self.assertTrue(self.installer(["--enable-wifi"], {"disable_wifi": True}).wifi_enabled())
        self.assertTrue(self.installer(config={"disable_wifi": False}).wifi_enabled())

    def test_enabled_wifi_verification_allows_unassociated_interface(self):
        inst = self.installer(["--enable-wifi"])
        inst.verify_wifi([{"type": "wlan", "soft": "unblocked", "hard": "unblocked"}], [{"name": "wlan0", "operstate": "down", "admin_up": True}])
        self.assertEqual(inst.report["wifi"]["policy"], "enabled")

    def test_enabled_wifi_still_blocked_or_admin_down_refuses(self):
        inst = self.installer(["--enable-wifi"])
        for soft, hard, up in (("blocked", "unblocked", True), ("unblocked", "blocked", True), ("unblocked", "unblocked", False)):
            with self.subTest(soft=soft, hard=hard, up=up), self.assertRaises(app.Refuse):
                inst.verify_wifi([{"type": "wlan", "soft": soft, "hard": hard}], [{"operstate": "down", "admin_up": up}])

    def test_disabled_wifi_must_be_blocked_and_down(self):
        inst = self.installer()
        inst.verify_wifi([{"type": "wlan", "soft": "blocked"}], [{"operstate": "down"}])
        with self.assertRaises(app.Refuse):
            inst.verify_wifi([{"type": "wlan", "soft": "unblocked"}], [])
        with self.assertRaises(app.Refuse):
            inst.verify_wifi([], [{"operstate": "up"}])

    def test_wifi_without_hardware_reports_warning(self):
        inst = self.installer(["--enable-wifi"])
        inst.verify_wifi([], [])
        self.assertTrue(inst.report["warnings"])

    def test_wifi_enable_sends_unblock_up_and_keeps_ethernet_gate(self):
        inst = self.installer(["--enable-wifi"])
        inst.command, inst.change, inst.ethernet_check = Mock(), Mock(), Mock()
        with tempfile.TemporaryDirectory() as t:
            interface = Path(t) / "wlan0"
            (interface / "wireless").mkdir(parents=True)
            with patch.object(app.Path, "glob", return_value=[interface]), patch.object(app.shutil, "which", return_value=None):
                inst.configure_wifi()
        inst.ethernet_check.assert_called_once()
        inst.command.assert_any_call("rfkill", "unblock", "wifi", timeout=15)
        inst.command.assert_any_call("ip", "link", "set", "dev", "wlan0", "up", timeout=15)

    def test_abbreviated_dangerous_flag_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            app.arguments(["--flas"])


class DownloadTests(unittest.TestCase):
    def fixture(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr("auto-installer-v0.2/autoprinter.py", "# test fixture only\n")
            z.writestr("auto-installer-v0.2/peripherals.py", "# accessory fixture only\n")
            z.writestr("auto-installer-v0.2/config_sync.py", "# config fixture only\n")
            z.writestr('auto-installer-v0.2/board_flash.py','# flash fixture only\n')
            z.writestr('auto-installer-v0.2/update_payload.py','# update fixture only\n')
            z.writestr("auto-installer-v0.2/manifest.json", "{}")
        return stream.getvalue()

    def invoke(self, folder, forwarded=(), checksum=None, usb=None):
        data = self.fixture()
        response = io.BytesIO(data)
        response.geturl = lambda: "https://github.com/example/repo/releases/download/v0.2.0/bundle.zip"
        opener = Mock()
        opener.open.return_value = response
        argv = ["--url", response.geturl(), "--sha256", checksum or hashlib.sha256(data).hexdigest()] + list(forwarded)
        with patch.dict(os.environ,{'AUTOPRINTER_TRIGGER_SESSION':''}), patch.object(bootstrap.sys, "platform", "linux"), patch.object(bootstrap.tempfile, "mkdtemp", return_value=str(folder)), patch.object(bootstrap.shutil, "disk_usage", return_value=Mock(free=1024**3)), patch.object(bootstrap.urllib.request, "build_opener", return_value=opener), patch.object(bootstrap.os, "execv") as execute, contextlib.redirect_stdout(io.StringIO()):
            if usb:
                device, identity = usb
                with patch.object(bootstrap, "usb_identity", return_value=usb), patch.dict(os.environ, {}, clear=False):
                    bootstrap.main(argv)
            else:
                bootstrap.main(argv)
        return execute

    def test_no_options_downloads_and_executes_inventory_only(self):
        with tempfile.TemporaryDirectory() as t:
            execute = self.invoke(Path(t))
            command = execute.call_args.args[1]
            self.assertEqual(command[-1], "--inventory")
            self.assertNotIn("--apply", command)

    def test_bad_hash_never_executes_payload(self):
        with tempfile.TemporaryDirectory() as t, self.assertRaises(RuntimeError):
            self.invoke(Path(t), checksum="0" * 64)

    def test_peripheral_audit_selects_accessory_entrypoint_without_apply(self):
        with tempfile.TemporaryDirectory() as t:
            execute=self.invoke(Path(t), ['--peripherals'])
            command=execute.call_args.args[1]
            self.assertTrue(command[1].endswith('peripherals.py'))
            self.assertNotIn('--apply',command)

    def test_config_mode_selects_config_entrypoint_and_forwards_profile(self):
        with tempfile.TemporaryDirectory() as t:
            command=self.invoke(Path(t),['--configs','--printer-profile','manual-z']).call_args.args[1]
            self.assertTrue(command[1].endswith('config_sync.py'))
            self.assertIn('manual-z',command)
            self.assertNotIn('--apply',command)

    def test_board_flash_audit_routes_without_any_implicit_flash(self):
        with tempfile.TemporaryDirectory() as t:
            command=self.invoke(Path(t),['--board-flash','--chip','stm32h723xx','--target-port','5-1','--target-serial','ABC']).call_args.args[1]
            self.assertTrue(command[1].endswith('board_flash.py'))
            self.assertNotIn('--flash',command); self.assertNotIn('--apply',command)

    def test_paired_update_routes_to_board_only_and_preserves_mode(self):
        with tempfile.TemporaryDirectory() as t:
            command=self.invoke(Path(t),['--klipper-update-only','--chip','stm32h723xx','--target-port','5-1','--target-serial','ABC']).call_args.args[1]
            self.assertTrue(command[1].endswith('update_payload.py'))
            self.assertIn('--klipper-update-only',command); self.assertNotIn('--apply',command)

    def test_specialized_modes_cannot_be_combined(self):
        with patch.object(bootstrap.sys,'platform','linux'),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            bootstrap.main(['--url','https://example.com/a.zip','--sha256','0'*64,'--configs','--klipper-update-only'])

    def test_peripheral_ssh_is_explicit_and_preserves_pid_argument(self):
        with tempfile.TemporaryDirectory() as t:
            execute=self.invoke(Path(t), ['--peripherals','--supervised-ssh','--ssh-session-pid','123','--apply'])
            command=execute.call_args.args[1]
            self.assertTrue(command[1].endswith('peripherals.py'))
            self.assertIn('--ssh-session-pid',command)
            self.assertIn('123',command)

    def test_apply_without_usb_refuses_before_download(self):
        with patch.object(bootstrap.sys, "platform", "linux"), patch.object(bootstrap.urllib.request, "build_opener") as opener, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bootstrap.main(["--url", "https://example.com/a.zip", "--sha256", "0" * 64, "--apply"])
        opener.assert_not_called()

    def test_physical_port_captures_identity_and_forwards_flags(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            device = root / "device"
            device.mkdir()
            identity = {"idVendor": "1234", "idProduct": "abcd", "serial": "TRIGGER", "busnum": "1", "devnum": "4"}
            for key, value in identity.items():
                (device / key).write_text(value)
            folder = root / "download"
            folder.mkdir()
            execute = self.invoke(folder, ["--usb-port", "1-1", "--enable-wifi", "--enable-flashing-capability", "--config", "/local/machine.json"], usb=(device, identity))
            command = execute.call_args.args[1]
            for value in ("--enable-wifi", "--enable-flashing-capability", "--config", "/local/machine.json", "TRIGGER", "1234", "abcd"):
                self.assertIn(value, command)
            self.assertNotIn("--flash", command)

    def test_usb_port_identity_discovery_is_exact(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            device = root / "1-1"
            device.mkdir()
            for key, value in (("idVendor", "1234"), ("idProduct", "abcd"), ("serial", "SERIAL"), ("busnum", "1"), ("devnum", "3")):
                (device / key).write_text(value)
            self.assertEqual(bootstrap.usb_identity("1-1", root=root)[1]["serial"], "SERIAL")
            with self.assertRaises(RuntimeError):
                bootstrap.usb_identity("1-1", serial="WRONG", root=root)

    def test_downloader_latches_reenumeration(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "devnum").write_text("4")
            p = bootstrap.Presence(root, {"devnum": "4"})
            p.check()
            (root / "devnum").write_text("5")
            with self.assertRaises(RuntimeError):
                p.check()
            (root / "devnum").write_text("4")
            with self.assertRaises(RuntimeError):
                p.check()

    def test_downloader_zip_traversal_blocked(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            with zipfile.ZipFile(root / "bad.zip", "w") as z:
                z.writestr("../escape", "bad")
            with self.assertRaises(RuntimeError):
                bootstrap.unpack(root / "bad.zip", root / "dest", lambda: None)

    def test_redirect_downgrade_blocked(self):
        with self.assertRaises(RuntimeError):
            bootstrap.HTTPSRedirects().redirect_request(None, None, 302, "", {}, "http://example.com/payload")

    def test_release_launcher_pins_exact_payload(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "source"
            root.mkdir()
            (root / "autoprinter.py").write_text("# fixture\n")
            (root / "bootstrap.py").write_text("# fixture bootstrap\n")
            (root / "manifest.json").write_text("{}")
            result = release.build(root, Path(t) / "out", "owner/repo", "v0.2.0")
            self.assertEqual(hashlib.sha256(Path(result["archive"]).read_bytes()).hexdigest(), result["sha256"])
            script = Path(result["launcher"]).read_text()
            self.assertIn(result["sha256"], script)
            self.assertIn('"$@"', script)
            self.assertIn("v0.2.0/install.sh", result["command"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
