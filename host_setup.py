#!/usr/bin/env python3
"""Fresh host-only setup over supervised SSH. No printer configuration or MCU writes.

This separate bench path deliberately does not claim printer readiness. It never
starts/enables Klipper, changes radio/network settings, installs KlipperScreen,
or exposes flashing options. The normal USB-gated installer is unchanged.
"""
import argparse
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import sys
import threading
import time

import autoprinter as ap

PACKAGES = ["git", "python3-venv", "python3-dev", "build-essential", "libffi-dev",
            "libssl-dev", "libjpeg-dev", "zlib1g-dev", "libsodium-dev", "libopenjp2-7",
            "liblmdb-dev", "pkg-config", "nginx"]


class SSHGuard:
    def __init__(self, pid, proc=Path("/proc")):
        self.pid, self.proc = pid, proc
        self.cancelled = threading.Event()
        self.reason = "Controlling SSH session ended or setup was interrupted"
        self.device = {"port": "ssh-host-only"}
        self.started = self.start_time()
        environ = (proc / str(pid) / "environ").read_bytes().split(b"\0")
        connections = [x.split(b"=", 1)[1].decode() for x in environ if x.startswith(b"SSH_CONNECTION=")]
        if len(connections) != 1 or len(connections[0].split()) != 4:
            raise ap.Refuse("--ssh-session-pid must identify the current pi SSH shell")
        self.client = str(ipaddress.ip_address(connections[0].split()[0]))
        self.server = str(ipaddress.ip_address(connections[0].split()[2]))
        status = ap.read(proc / str(pid) / "status")
        uid = [x.split()[1] for x in status.splitlines() if x.startswith("Uid:")]
        import pwd
        if uid != [str(pwd.getpwnam("pi").pw_uid)]:
            raise ap.Refuse("Controlling session must belong to pi")
        self.check()

    def start_time(self):
        try:
            return (self.proc / str(self.pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            self.cancelled.set()
            raise ap.Removed("Controlling SSH session is unavailable")

    def check(self):
        if self.cancelled.is_set() or self.start_time() != self.started:
            self.cancelled.set()
            raise ap.Removed(self.reason)


class HostSetup(ap.Installer):
    def preflight(self, host):
        if os.geteuid() != 0:
            raise ap.Refuse("Host setup requires sudo")
        if not self.args.supervised_host_setup:
            raise ap.Refuse("Specify --supervised-host-setup for this fresh host-only installation")
        if host["model"] != "OrangePi 3 LTS" or host["machine"] != "aarch64":
            raise ap.Refuse("This bench setup is currently limited to the inspected OrangePi 3 LTS aarch64")
        if host["os"].get("ID") != "ubuntu" or host["os"].get("VERSION_ID") != "26.04" or not self.args.experimental_ubuntu_26_04:
            raise ap.Refuse("This bench setup needs the inspected Ubuntu 26.04 image and explicit experimental opt-in")
        self.validate_layout()
        if self.report["existing_installation"]:
            raise ap.Refuse("Host-only bench setup refuses an existing printing service; use reviewed normal maintenance")
        if self.systemd_state("nginx").get("LoadState") == "loaded":
            raise ap.Refuse("Existing nginx service needs review before host-only setup")
        for name in ("klipper", "moonraker", "klippy-env", "moonraker-env", "mainsail"):
            p = self.home / name
            if p.exists() or p.is_symlink():
                raise ap.Refuse("Existing host path requires inspection before setup: " + str(p))
        if (self.data / "config/printer.cfg").exists():
            raise ap.Refuse("Printer configuration exists; host-only fresh setup refuses")
        if (self.data / "config/moonraker.conf").exists():
            raise ap.Refuse("Existing Moonraker configuration requires inspection")
        if self.report["dpkg_audit"]:
            raise ap.Refuse("Broken/interrupted APT state requires manual review")
        for name in ("klipper", "moonraker"):
            ap.require_digest(self.c.get("revisions", {}).get(name), 40)
        ap.require_digest(self.c.get("mainsail", {}).get("sha256"))
        url = self.c["mainsail"].get("url", "")
        if not url.startswith("https://github.com/mainsail-crew/mainsail/releases/download/") or not url.endswith("/mainsail.zip") or "/latest/" in url:
            raise ap.Refuse("Pin the official versioned Mainsail release URL")
        ap.check_free(["/", "/var", self.home])
        _, route = self.command("ip", "-j", "route", "get", "1.1.1.1", timeout=15)
        routes = json.loads(route)
        if len(routes) != 1:
            raise ap.Refuse("No unique internet route")
        interface = routes[0].get("dev", "")
        if (Path("/sys/class/net") / interface / "wireless").exists() and not self.args.allow_wifi_transport:
            raise ap.Refuse("Wi-Fi SSH transport requires --allow-wifi-transport; it will be preserved")
        self.command("curl", "--fail", "--silent", "--show-error", "--proto", "=https", "--max-time", "20", "--output", "/dev/null", "https://github.com", timeout=25)
        self.report["transport"] = {"interface": interface, "wifi_preserved": True, "ssh_client": self.guard.client}

    def install_host_packages(self):
        self.command("apt-get", "-o", "DPkg::Lock::Timeout=20", "update", timeout=600)
        self.command("apt-get", "-s", "--no-remove", "--no-install-recommends", "install", *PACKAGES, timeout=120)
        ap.check_free(["/", "/var", self.home])
        self.command("apt-get", "-o", "DPkg::Lock::Timeout=20", "-o", "Dpkg::Options::=--force-confold", "--yes", "--no-remove", "--no-install-recommends", "install", *PACKAGES, timeout=2400)
        self.change("Installed host-only dependencies; no OS upgrade, firmware tools, display or network-manager changes")

    def stage_host(self):
        paths = {}
        for name, env, requirements in (("klipper", "klippy-env", "scripts/klippy-requirements.txt"),
                                         ("moonraker", "moonraker-env", "scripts/moonraker-requirements.txt")):
            ap.check_free(["/", "/var", self.home])
            source = self.checkout(name)
            environment = self.stage / env
            self.venv(environment, source / requirements)
            paths[name], paths[env] = source, environment
        self.command(paths["klippy-env"] / "bin/python", paths["klipper"] / "klippy/klippy.py", "--help", user="pi", timeout=60)
        self.command(paths["moonraker-env"] / "bin/python", "-m", "moonraker", "--help", user="pi", cwd=paths["moonraker"], timeout=60)
        self.download_mainsail()
        self.change("Built and checked isolated pinned Klipper/Moonraker environments and verified Mainsail assets")
        return paths

    def install_host_files(self, paths):
        self.user_directory(self.data)
        for sub in ("config", "logs", "comms", "gcodes", "systemd"):
            self.user_directory(self.data / sub)
        for name, path in paths.items():
            self.activate_path(self.home / name, path)
        klipper_unit = """[Unit]
Description=Klipper host (commissioning required)
After=network.target
ConditionPathExists=/home/pi/printer_data/config/printer.cfg
[Service]
Type=simple
User=pi
SupplementaryGroups=dialout
ExecStart=/home/pi/klippy-env/bin/python /home/pi/klipper/klippy/klippy.py /home/pi/printer_data/config/printer.cfg -l /home/pi/printer_data/logs/klippy.log -a /home/pi/printer_data/comms/klippy.sock
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
"""
        ap.atomic_write(Path("/etc/systemd/system/klipper.service"), klipper_unit, self.guard, 0o644)
        self.command("systemctl", "daemon-reload", timeout=30)
        self.command("systemctl", "disable", "klipper", timeout=30)
        self.change("Installed Klipper unit disabled, with missing-config start condition; no printer.cfg fabricated")
        moonraker_config = """[server]
host: 0.0.0.0
port: 7125
klippy_uds_address: /home/pi/printer_data/comms/klippy.sock
[authorization]
trusted_clients:
  127.0.0.1
  ::1
  {client}
cors_domains:
  http://{server}:8080
[machine]
provider: systemd_cli
[file_manager]
enable_object_processing: True
[history]
[announcements]
""".format(client=self.guard.client, server=self.guard.server)
        ap.atomic_write(self.data / "config/moonraker.conf", moonraker_config, self.guard, 0o640)
        self.command("chown", "pi:pi", self.data / "config/moonraker.conf")
        self.unit("moonraker", """[Unit]
Description=Moonraker API
After=network.target
[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/moonraker
ExecStart=/home/pi/moonraker-env/bin/python -m moonraker -d /home/pi/printer_data
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
""")
        www = Path("/var/www/autoprinter-mainsail") / self.run_id
        www.parent.mkdir(parents=True, exist_ok=True)
        self.guard.check()
        shutil.copytree(self.stage / "mainsail", www)
        for p in [www] + list(www.rglob("*")):
            self.guard.check()
            p.chmod(0o755 if p.is_dir() else 0o644)
        self.activate_path(self.home / "mainsail", www)
        site = Path("/etc/nginx/conf.d/autoprinter-mainsail.conf")
        if site.exists() or site.is_symlink():
            raise ap.Refuse("Unexpected existing Mainsail nginx site")
        content = """# Managed by AutoPrinter host-only setup
server {
 listen 8080;
 server_name _;
 root WWWROOT;
 index index.html;
 client_max_body_size 0;
 location / { try_files $uri $uri/ /index.html; }
 location /websocket {
  proxy_pass http://127.0.0.1:7125/websocket;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_read_timeout 86400;
 }
 location ~ ^/(printer|api|access|machine|server)(/|$) {
  proxy_pass http://127.0.0.1:7125;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $remote_addr;
 }
}
""".replace("WWWROOT", str(www))
        ap.atomic_write(site, content, self.guard, 0o644)
        self.command("nginx", "-t", timeout=15)
        self.command("systemctl", "daemon-reload", timeout=30)
        self.command("systemctl", "enable", "nginx", "moonraker", timeout=30)
        self.command("systemctl", "reload-or-restart", "nginx", timeout=30)
        self.command("systemctl", "start", "moonraker", timeout=45)
        self.change("Started Mainsail nginx endpoint on port 8080 and Moonraker; private home permissions preserved")

    def setup(self):
        host = self.inventory()
        self.preflight(host)
        self.setup_state()
        self.report["status"] = "HOST_SETUP_IN_PROGRESS"
        self.backup()
        self.install_host_packages()
        paths = self.stage_host()
        self.install_host_files(paths)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            self.guard.check()
            try:
                server = ap.url_json("http://127.0.0.1:7125/server/info")
                break
            except (OSError, ValueError, ap.Refuse):
                self.guard.cancelled.wait(2)
        else:
            raise ap.Refuse("Moonraker API did not start; inspect journalctl -u moonraker")
        self.command("curl", "--fail", "--silent", "--show-error", "--max-time", "10", "--output", "/dev/null", "http://127.0.0.1:8080/", timeout=15)
        state = self.systemd_state("klipper")
        if state.get("ActiveState") != "inactive" or state.get("UnitFileState") != "disabled":
            raise ap.Refuse("Klipper must remain inactive and disabled after host-only setup")
        self.report.update(status="HOST_INSTALLED_NOT_PRINTER_READY", moonraker=server, web_url="http://" + self.guard.server + ":8080",
                           remaining_free_mib=round(shutil.disk_usage("/").free / ap.MIB))
        self.save_report()
        self.emit("HOST_INSTALLED_NOT_PRINTER_READY: " + self.report["web_url"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True)
    parser.add_argument("--ssh-session-pid", type=int, required=True)
    parser.add_argument("--supervised-host-setup", action="store_true")
    parser.add_argument("--experimental-ubuntu-26-04", action="store_true")
    parser.add_argument("--allow-wifi-transport", action="store_true")
    args = parser.parse_args(argv)
    guard = SSHGuard(args.ssh_session_pid)
    def interrupted(signum, frame):
        guard.cancelled.set()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    args.apply, args.enable_wifi, args.enable_flashing_capability = True, True, False
    config = json.loads(Path(args.config).read_text())
    config.update(console_progress=False, adopt_standard_layout=False, required_config=[], disable_wifi=False)
    installer = HostSetup(args, config, guard)
    import fcntl
    fd = os.open("/run/autoprinter.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            installer.setup()
            return 0
        except (ap.Refuse, OSError, ValueError, KeyError) as error:
            installer.report["status"] = "HOST_SETUP_FAILED"
            installer.report["blockers"].append(str(error))
            installer.emit("HOST_SETUP_FAILED: " + str(error))
            with contextlib.suppress(ap.Refuse, OSError):
                installer.save_report()
            return 2
        finally:
            if installer.logfile:
                installer.logfile.close()


if __name__ == "__main__":
    sys.exit(main())
