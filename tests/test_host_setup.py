import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import autoprinter as ap
import host_setup as hs


class HostBoundaryTests(unittest.TestCase):
    def test_host_mode_has_no_flashing_option(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            hs.main(["--config", "test.json", "--ssh-session-pid", "1", "--flash"])

    def test_guard_latches_missing_or_reused_session(self):
        with tempfile.TemporaryDirectory() as t:
            proc = Path(t)
            session = proc / "42"
            session.mkdir()
            (session / "stat").write_text("42 (bash) " + " ".join(["S"] + ["0"] * 18 + ["100"]))
            (session / "environ").write_bytes(b"SSH_CONNECTION=10.0.0.2 40000 10.0.0.3 22\0")
            (session / "status").write_text("Uid:\t1000\t1000\t1000\t1000\n")
            fake_pwd = types.SimpleNamespace(getpwnam=lambda name: types.SimpleNamespace(pw_uid=1000))
            with patch.dict(sys.modules, {"pwd": fake_pwd}):
                guard = hs.SSHGuard(42, proc)
            self.assertEqual(guard.client, "10.0.0.2")
            (session / "stat").write_text("42 (bash) " + " ".join(["S"] + ["0"] * 18 + ["101"]))
            with self.assertRaises(ap.Removed):
                guard.check()
            (session / "stat").write_text("42 (bash) " + " ".join(["S"] + ["0"] * 18 + ["100"]))
            with self.assertRaises(ap.Removed):
                guard.check()

    def test_non_ssh_process_cannot_be_used_as_guard(self):
        with tempfile.TemporaryDirectory() as t:
            session = Path(t) / "42"
            session.mkdir()
            (session / "stat").write_text("42 (bash) " + " ".join(["S"] + ["0"] * 18 + ["100"]))
            (session / "environ").write_bytes(b"HOME=/home/pi\0")
            with self.assertRaises(ap.Refuse):
                hs.SSHGuard(42, Path(t))

    def host(self):
        from test_safety import Guard
        args = ap.arguments([])
        args.supervised_host_setup = True
        args.experimental_ubuntu_26_04 = True
        return hs.HostSetup(args, {}, Guard())

    def test_existing_printer_installation_blocks_bench_mode(self):
        host = self.host()
        host.validate_layout = Mock()
        host.report["existing_installation"] = True
        host.setup_state = Mock()
        with patch.object(os, "geteuid", return_value=0, create=True), self.assertRaises(ap.Refuse):
            host.preflight({"model": "OrangePi 3 LTS", "machine": "aarch64", "os": {"ID": "ubuntu", "VERSION_ID": "26.04"}})
        host.setup_state.assert_not_called()

    def test_experimental_image_needs_explicit_opt_in(self):
        host = self.host()
        host.args.experimental_ubuntu_26_04 = False
        host.validate_layout = Mock()
        with patch.object(os, "geteuid", return_value=0, create=True), self.assertRaises(ap.Refuse):
            host.preflight({"model": "OrangePi 3 LTS", "machine": "aarch64", "os": {"ID": "ubuntu", "VERSION_ID": "26.04"}})
        host.validate_layout.assert_not_called()


if __name__ == "__main__":
    unittest.main()
