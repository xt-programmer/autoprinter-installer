#!/usr/bin/env python3
"""Supervised accessory setup for an existing single-pi KIAUH layout.

Default: read-only plan. Apply requires inactive Klipper, backups and a live
USB trigger or an explicitly selected supervised SSH session. Never flashes,
creates printer.cfg, sends G-code, changes networking, or reboots.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import platform
import re
import signal
import tarfile
import urllib.request

import autoprinter as ap
import peripheral_history as ph
import config_profiles as cp
from host_setup import SSHGuard

HOME = Path('/home/pi')
CONFIG = HOME / 'printer_data/config'
PINS = {
    'crowsnest': ('https://github.com/mainsail-crew/crowsnest.git', '436042452f564f3d737e3b980a213f849a8a0562', 'v5'),
    'KlipperScreen': ('https://github.com/KlipperScreen/KlipperScreen.git', '26d262522b9bd9fe7d67f67d3c6f90aa156d1b07', 'master'),
    'cartographer3d-plugin': ('https://github.com/Cartographer3D/cartographer3d-plugin.git', '7cb58488c45de64235c13f6c771e8a31903b1fb0', 'main'),
}
SCREEN_PACKAGES = 'xinit xinput x11-xserver-utils xserver-xorg-input-evdev xserver-xorg-input-libinput xserver-xorg-legacy xserver-xorg-video-fbdev libgirepository1.0-dev libcairo2-dev gir1.2-gtk-3.0 librsvg2-common libdbus-glib-1-dev autoconf libmpv2 dbus-x11 cmake libsystemd-dev'.split()
INCLUDE = 'autoprinter-peripherals.conf'


def plan(snapshot, history, journal=''):
    merged = ph.merge(history, snapshot, ph.kernel_evidence(journal))
    return {'current': snapshot, 'history': merged, 'install': dict(ph.wanted(merged, snapshot), KlipperScreen=True)}


def complete_jpeg(data):
    # Some UVC MJPEG frames include a small zero pad after the EOI marker.
    # Accept at most 16 such bytes; truncated images/other trailing data fail.
    return data.startswith(b'\xff\xd8') and data[-18:].rstrip(b'\x00').endswith(b'\xff\xd9')


def camera_configuration(snapshot):
    nodes = [n['device'] for c in snapshot['usb_webcam'] for n in c.get('nodes', [])]
    if not nodes:
        return None  # Historical-only camera: install software without guessing a device.
    if len(nodes) != 1 or not nodes[0].startswith('/dev/v4l/by-id/'):
        raise ap.Refuse('Camera configuration needs exactly one stable USB capture path; preserve/manual-review multiple cameras')
    return '''# AutoPrinter managed USB camera; never select SoC decoder video0.
[crowsnest]
log_level: verbose
rollover_on_start: false
no_proxy: false

[cam 1]
mode: ustreamer
port: 8080
device: %s
resolution: 640x480
max_fps: 15
''' % nodes[0]


def refuse_legacy_cartographer(config=CONFIG):
    for file in config.rglob('*.cfg'):
        if re.search(r'^\s*(?:#\*#\s*)?\[scanner(?:\s|\])', file.read_text(errors='replace'), re.M | re.I):
            raise ap.Refuse('Legacy [scanner] configuration (including SAVE_CONFIG) requires operator migration: ' + str(file))
    for directory in (HOME/'klipper/klippy/extras', HOME/'klipper/klippy/plugins'):
        for name in ('scanner.py', 'idm.py'):
            file = directory / name
            if file.exists() or file.is_symlink():
                raise ap.Refuse('Legacy probe plugin requires manual migration before vendor cleanup: ' + str(file))
        cartographer=directory/'cartographer.py'
        if (cartographer.exists() or cartographer.is_symlink()) and (cartographer.is_symlink() or cartographer.read_text().strip()!='from cartographer.extra import *'):
            raise ap.Refuse('Existing Cartographer implementation requires manual migration: '+str(cartographer))


class Setup:
    def __init__(self, args, guard, report):
        self.args, self.guard, self.report = args, guard, report
        self.runner = ap.Runner(guard, self.emit)
        self.state = None
        self.log = None
        self.sections = []

    def emit(self, message):
        if self.log:
            self.log.write(message + '\n'); self.log.flush()
        print(message[-1200:], flush=True)

    def run(self, *args, **kwargs):
        return self.runner.run(args, **kwargs)

    def write(self, path, data, user=False, mode=0o644):
        self.guard.check()
        ap.atomic_write(path, data, self.guard, mode)
        if user:
            self.run('chown', 'pi:pi', path)

    def preflight(self):
        if os.geteuid() != 0 or not self.args.maintenance_confirmed:
            raise ap.Refuse('Apply requires sudo and --maintenance-confirmed')
        host = ap.inspect_host()
        os_info = host['os']
        supported = os_info.get('ID') == 'debian' and os_info.get('VERSION_ID') in ('12','13') or os_info.get('ID') == 'ubuntu' and os_info.get('VERSION_ID') in ('22.04','24.04')
        experimental = os_info.get('ID') == 'ubuntu' and os_info.get('VERSION_ID') == '26.04' and self.args.experimental_ubuntu_26_04
        if not (supported or experimental):
            raise ap.Refuse('Unsupported accessory userspace/architecture; Ubuntu 26.04 needs explicit experimental opt-in')
        model_check=dict(host)
        if experimental: model_check['os']={'ID':'ubuntu','VERSION_ID':'24.04'}
        ap.supported_host(model_check)
        for path in (HOME/'klipper', HOME/'moonraker', HOME/'klippy-env', HOME/'moonraker-env', CONFIG):
            if not path.is_dir() or path.is_symlink():
                raise ap.Refuse('Accessory mode requires reviewed real KIAUH directories: ' + str(path))
        for service in ('klipper', 'moonraker'):
            _, unit = self.run('systemctl', 'cat', service, timeout=15)
            if 'User=pi' not in unit or '/home/pi/' not in unit:
                raise ap.Refuse('Custom/multiple printer layout needs manual integration')
        for service, source, env in (('KlipperScreen', 'KlipperScreen', '.KlipperScreen-env'), ('crowsnest', 'crowsnest', 'crowsnest-env')):
            unit = Path('/etc/systemd/system')/(service+'.service')
            if unit.exists() and not all(value in unit.read_text() for value in ('User=pi', '/home/pi/'+source, '/home/pi/'+env)):
                raise ap.Refuse('Custom accessory service requires review: '+service)
        _, active = self.run('systemctl', 'is-active', 'klipper', check=False)
        if active != 'inactive':
            raise ap.Refuse('Stop Klipper under supervised maintenance before accessorizing; active/failed/activating states refuse')
        _, broken = self.run('dpkg', '--audit', timeout=30)
        if broken:
            raise ap.Refuse('Interrupted/broken package state needs review')
        ap.check_free(['/', '/var', HOME])
        # Refuse conflicting existing include; never replace operator configuration.
        include = CONFIG/INCLUDE
        if include.exists() and not include.read_text().startswith('# Managed peripheral registrations.'):
            raise ap.Refuse('Existing accessory include is not owned by this installer')
        self.original_include = include.read_text() if include.exists() else ''
        self.original_moonraker = (CONFIG/'moonraker.conf').read_text()
        combined = '\n'.join(p.read_text(errors='replace') for p in CONFIG.glob('*.conf') if p.name != INCLUDE)
        for name in ('crowsnest', 'KlipperScreen', 'cartographer', 'cartographer_plugin'):
            if re.search(r'^\s*\[update_manager\s+'+name+r'\]', combined, re.M | re.I):
                raise ap.Refuse('Existing update-manager entry requires manual adoption: ' + name)
        if self.report['install']['cartographer']:
            refuse_legacy_cartographer()
        self.config_plan=cp.plan(CONFIG,self.args.printer_profile,bool(self.report['current']['cartographer']))
        cp.validate(self.config_plan)
        self.report['config_plan']=self.config_plan
        cam = CONFIG/'crowsnest.conf'
        if self.report['install']['usb_webcam'] and cam.exists():
            text = cam.read_text()
            if not self.report['current']['usb_webcam'] and text.startswith('# AutoPrinter') and re.search(r'^\[cam\s',text,re.M):
                self.report['preserve_existing_camera_config'] = True
            if not (text.startswith('# AutoPrinter') or text.startswith('#### crowsnest.conf') and 'device: /dev/video0' in text):
                # Existing working setups are preserved, not reset to our defaults.
                self.report['preserve_existing_camera_config'] = True

    def backup(self):
        self.state = Path('/var/lib/autoprinter') / ('peripherals-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S-%f'))
        self.state.mkdir(parents=True, mode=0o700)
        self.log = (self.state/'install.log').open('a', buffering=1)
        with tarfile.open(self.state/'before.tar', 'w', dereference=False) as archive:
            for path in (CONFIG, HOME/'printer_data/systemd', Path('/etc/systemd/system'), Path('/etc/X11/Xwrapper.config'), Path('/etc/polkit-1/rules.d/KlipperScreen.rules'), ph.DEFAULT_STATE):
                self.guard.check()
                if path.exists():
                    archive.add(path, arcname=str(path).lstrip('/'))
        self.report['backup'] = str(self.state/'before.tar')
        self.report['backup_sha256'] = ap.sha256(self.state/'before.tar', self.guard)
        self.write(self.state/'report.json', json.dumps(self.report, indent=2), mode=0o600)

    def source(self, name):
        origin, revision, branch = PINS[name]
        path = HOME/name
        if path.exists():
            if path.is_symlink():
                raise ap.Refuse('Refusing symlink source: ' + str(path))
            _, actual_origin = self.run('git', '-C', path, 'remote', 'get-url', 'origin', user='pi')
            _, head = self.run('git', '-C', path, 'rev-parse', 'HEAD', user='pi')
            _, dirty = self.run('git', '-C', path, 'status', '--porcelain', '--untracked-files=no', user='pi')
            if actual_origin != origin or head != revision or dirty:
                raise ap.Refuse('Existing source differs from reviewed pin; preserve and review ' + str(path))
        else:
            self.run('git', 'init', path, user='pi')
            self.run('git', '-C', path, 'remote', 'add', 'origin', origin, user='pi')
            self.run('git', '-C', path, 'fetch', '--depth=1', 'origin', revision, user='pi', timeout=300)
            self.run('git', '-C', path, 'checkout', '--detach', revision, user='pi')
        self.report.setdefault('sources', {})[name] = revision
        return path

    def observe(self):
        self.guard.check()
        ph.record(self.report['current'], ph.kernel_evidence(self.report.get('kernel_evidence', '')))
        directory = Path('/usr/local/lib/autoprinter')
        directory.mkdir(parents=True, exist_ok=True, mode=0o755)
        self.write(directory/'peripheral_history.py', Path(ph.__file__).read_bytes(), mode=0o755)
        command = '/usr/bin/python3 /usr/local/lib/autoprinter/peripheral_history.py record --event $env{DEVPATH}'
        rules = '# Record facts only. Never install software or issue MCU commands.\n'
        rules += 'ACTION=="add", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", RUN+="'+command+'"\n'
        rules += 'ACTION=="add", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_interface", ATTR{bInterfaceClass}=="0e", RUN+="'+command+'"\n'
        self.write(Path('/etc/udev/rules.d/70-autoprinter-device-history.rules'), rules)
        self.run('udevadm', 'control', '--reload-rules')

    def configs(self):
        def inactive():
            _,state=self.run('systemctl','is-active','klipper',check=False,timeout=15)
            if state!='inactive': raise ap.Refuse('Klipper became active before config replacement')
        self.report['config_changes']=cp.apply(CONFIG,self.config_plan,self.state/'config-files-before',self.guard,
            lambda path,data,mode:self.write(path,data,mode=mode),
            ownership=lambda path:self.run('chown','pi:pi',path),before_write=inactive)

    def crowsnest(self):
        if not self.report['install']['usb_webcam']:
            return
        source = self.source('crowsnest')
        if not (HOME/'crowsnest-env/bin/python').exists() or not Path('/etc/systemd/system/crowsnest.service').is_file():
            self.run('make', 'install', cwd=source, timeout=2400, env={
                'BASE_USER':'pi', 'SUDO_USER':'pi', 'CROWSNEST_UNATTENDED':'1',
                'CROWSNEST_SKIP_REBOOT_PROMPT':'1', 'CROWSNEST_ADD_CROWSNEST_MOONRAKER':'0',
                'CROWSNEST_CONFIG_PATH':str(CONFIG), 'CROWSNEST_LOG_PATH':str(HOME/'printer_data/logs'),
                'CROWSNEST_ENV_PATH':str(HOME/'printer_data/systemd')})
        desired = camera_configuration(self.report['current'])
        previous_webcam=re.search(r'^\[webcam USB Camera\]\n.*?(?=^\[|\Z)',self.original_include,re.M|re.S)
        if not desired and previous_webcam:
            self.sections.append(previous_webcam.group(0))
        if desired and not self.report.get('preserve_existing_camera_config'):
            self.write(CONFIG/'crowsnest.conf', desired, user=True)
            self.sections.append('''[webcam USB Camera]
location: printer
enabled: True
service: mjpegstreamer
target_fps: 15
stream_url: /webcam/?action=stream
snapshot_url: /webcam/?action=snapshot
''')
        elif not desired and not self.report.get('preserve_existing_camera_config'):
            # Do not start vendor /dev/video0 default when only historical evidence exists.
            self.write(CONFIG/'crowsnest.conf', '[crowsnest]\nlog_level: verbose\nrollover_on_start: false\nno_proxy: false\n', user=True)
        self.sections.append((source/'resources/moonraker_update.txt').read_text())
        self.report['camera_present'] = bool(desired)

    def screen(self):
        source = self.source('KlipperScreen')
        env = HOME/'.KlipperScreen-env'
        self.run('apt-get', '-o', 'DPkg::Lock::Timeout=20', 'update', timeout=600)
        self.run('apt-get', '-s', '--no-remove', '--no-install-recommends', 'install', *SCREEN_PACKAGES)
        self.run('apt-get', '-o', 'DPkg::Lock::Timeout=20', '-o', 'Dpkg::Options::=--force-confold', '--yes', '--no-remove', '--no-install-recommends', 'install', *SCREEN_PACKAGES, timeout=2400)
        if not (env/'bin/python').exists():
            self.run('python3', '-m', 'venv', env, user='pi', env={'HOME':str(HOME)})
        self.run(env/'bin/python', '-m', 'pip', 'install', '--no-cache-dir', '-r', source/'scripts/KlipperScreen-requirements.txt', user='pi', env={'HOME':str(HOME)}, timeout=1800)
        self.run(env/'bin/python', '-c', 'import gi; gi.require_version("Gtk","3.0"); from gi.repository import Gtk; import mpv,jinja2,requests,sdbus,psutil,websocket', user='pi')
        if not Path('/etc/systemd/system/KlipperScreen.service').exists():
            self.run('groupadd', '-f', 'klipperscreen')
            self.run('usermod', '-a', '-G', 'klipperscreen,tty,video', 'pi')
            unit = (source/'scripts/KlipperScreen.service').read_text()
            for a,b in {'KS_USER':'pi', 'KS_ENV':str(env), 'KS_DIR':str(source), 'KS_BACKEND':'X'}.items():
                unit = unit.replace(a,b)
            self.write(Path('/etc/systemd/system/KlipperScreen.service'), unit)
            self.write(Path('/etc/X11/Xwrapper.config'), 'allowed_users=anybody\nneeds_root_rights=yes\n')
        drop = Path('/etc/systemd/system/KlipperScreen.service.d')
        drop.mkdir(exist_ok=True)
        self.write(drop/'display-presence.conf', "[Service]\n# Skip headless DRM startup; avoid an X restart loop.\nExecCondition=/bin/sh -c 'grep -qx connected /sys/class/drm/*/status'\n")
        policy=Path('/etc/polkit-1/rules.d/KlipperScreen.rules')
        if not policy.exists():
            self.write(policy,'''polkit.addRule(function(action, subject) {
    if (subject.isInGroup("klipperscreen") &&
        (action.id.startsWith("org.freedesktop.login1.power-off") ||
         action.id.startsWith("org.freedesktop.login1.reboot"))) return polkit.Result.YES;
});
''')
        self.sections.append('''[update_manager KlipperScreen]
type: git_repo
path: ~/KlipperScreen
origin: https://github.com/KlipperScreen/KlipperScreen.git
primary_branch: master
managed_services: KlipperScreen
virtualenv: ~/.KlipperScreen-env
requirements: scripts/KlipperScreen-requirements.txt
''')

    def cartographer(self):
        if not self.report['install']['cartographer']:
            self.report['cartographer'] = 'SKIPPED_NO_POSITIVE_DETECTION'
            return
        refuse_legacy_cartographer()
        source = self.source('cartographer3d-plugin')
        # Follow the documented PyPI installer; preinstall modern numpy so its
        # historical ~=1.16 fallback cannot break newer Python environments.
        env = HOME/'klippy-env'
        self.run(env/'bin/python', '-m', 'pip', 'freeze', user='pi')
        self.run('tar', '-cpf', self.state/'klippy-env-before.tar', '-C', HOME, 'klippy-env', timeout=600)
        with tarfile.open(self.state/'probe-files-before.tar','w',dereference=False) as archive:
            for directory in (HOME/'klipper/klippy/extras', HOME/'klipper/klippy/plugins'):
                for name in ('cartographer.py','idm.py','scanner.py'):
                    file=directory/name
                    if file.exists() or file.is_symlink(): archive.add(file,arcname=str(file).lstrip('/'))
            exclude=HOME/'klipper/.git/info/exclude'
            if exclude.exists(): archive.add(exclude,arcname=str(exclude).lstrip('/'))
        self.run(env/'bin/python', '-m', 'pip', 'install', '--no-cache-dir', 'numpy>=1.16', user='pi', env={'HOME':str(HOME)}, timeout=1200)
        self.run('bash', source/'scripts/install.sh', '-k', HOME/'klipper', '-e', env, user='pi', env={'HOME':str(HOME)}, timeout=1200)
        self.run(env/'bin/python', '-c', 'import cartographer.extra', user='pi',env={'PYTHONPATH':str(HOME/'klipper/klippy'),'HOME':str(HOME)})
        self.run(env/'bin/python','-m','pip','check',user='pi')
        self.sections.append('''[update_manager cartographer_plugin]
type: python
channel: stable
virtualenv: ~/klippy-env
project_name: cartographer3d-plugin
is_system_service: False
managed_services: klipper
''')
        self.report['cartographer'] = 'PLUGIN_INSTALLED_CONFIG_AND_CALIBRATION_REQUIRED'

    def finish(self):
        include = '# Managed peripheral registrations. No printer hardware configuration.\n'+'\n'.join(self.sections)
        self.write(CONFIG/INCLUDE, include, user=True)
        marker = '[include '+INCLUDE+']'
        if marker not in self.original_moonraker:
            self.write(CONFIG/'moonraker.conf', self.original_moonraker.rstrip()+'\n\n'+marker+'\n', user=True, mode=0o640)
        self.run('systemctl', 'daemon-reload')
        self.run('systemctl', 'enable', 'KlipperScreen')
        self.run('systemctl', 'start', 'KlipperScreen')
        if self.report['install']['usb_webcam']:
            self.run('systemctl', 'enable', 'crowsnest')
            if self.report.get('camera_present') or self.report.get('preserve_existing_camera_config'):
                self.run('systemctl', 'restart', 'crowsnest')
        self.run('systemctl', 'restart', 'moonraker')
        for attempt in range(30):
            self.guard.check()
            try:
                info=ap.url_json('http://127.0.0.1:7125/server/info')
                if info.get('warnings'): raise ap.Refuse('Moonraker warnings: '+str(info['warnings']))
                with urllib.request.urlopen('http://127.0.0.1/',timeout=3) as response:
                    if response.status!=200: raise ap.Refuse('Mainsail port 80 failed')
                if self.report.get('camera_present'):
                    with urllib.request.urlopen('http://127.0.0.1/webcam/?action=snapshot',timeout=3) as response:
                        data=response.read(2*ap.MIB)
                        if response.headers.get_content_type()!='image/jpeg' or not complete_jpeg(data):
                            raise ap.Refuse('Camera snapshot is not a complete JPEG')
                        self.report['camera_snapshot_bytes']=len(data)
                break
            except (OSError,ValueError,ap.Refuse):
                if attempt==29: raise
                self.guard.cancelled.wait(1)
        _, active=self.run('systemctl','is-active','klipper',check=False)
        if active!='inactive': raise ap.Refuse('Unexpected Klipper state changed during accessory maintenance')
        self.report['moonraker_warnings']=info.get('warnings',[])
        self.report['status'] = 'ACCESSORIES_INSTALLED_NOT_PRINTER_READY'
        self.write(self.state/'report.json', json.dumps(self.report, indent=2), mode=0o600)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--printer-profile',choices=cp.PROFILES,default='auto')
    parser.add_argument('--maintenance-confirmed', action='store_true')
    parser.add_argument('--experimental-ubuntu-26-04', action='store_true')
    parser.add_argument('--supervised-ssh', action='store_true')
    parser.add_argument('--ssh-session-pid', type=int)
    for field in ('port','vid','pid','serial'):
        parser.add_argument('--usb-'+field)
    return parser.parse_args(argv)


def main(argv=None):
    args = arguments(argv)
    guard = setup = lock = None
    try:
        if platform.system() != 'Linux':
            raise ap.Refuse('Run on the target Linux Pi')
        snapshot = ph.current()
        import subprocess
        result = subprocess.run(['journalctl','-k','--no-pager','-o','cat','--case-sensitive=no','--grep=cartographer|Found UVC'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
        report = plan(snapshot, ph.load(), result.stdout)
        report['kernel_evidence'] = result.stdout
        try: report['config_plan']=cp.plan(CONFIG,args.printer_profile,bool(snapshot['cartographer']))
        except (ValueError,OSError) as error: report['config_plan']={'error':str(error)}
        if not args.apply:
            report['status'] = 'AUDIT_ONLY'
            print(json.dumps(report, indent=2)); return 0
        if args.supervised_ssh:
            if args.usb_port or not args.ssh_session_pid:
                raise ap.Refuse('SSH mode needs --ssh-session-pid and cannot be combined with USB trigger')
            guard = SSHGuard(args.ssh_session_pid)
        else:
            selector = {k:getattr(args,'usb_'+k) for k in ('port','vid','pid','serial') if getattr(args,'usb_'+k) is not None}
            trigger = ap.select_usb(ap.usb_devices(), selector)
            inherited = os.environ.get('AUTOPRINTER_TRIGGER_SESSION')
            if inherited and any(trigger.get(k)!=v for k,v in json.loads(inherited).items()):
                raise ap.Removed('USB changed during download')
            guard = ap.USBGuard(trigger).start()
        def interrupted(signum, frame):
            guard.cancelled.set()
        signal.signal(signal.SIGINT, interrupted); signal.signal(signal.SIGTERM, interrupted)
        import fcntl
        lock = os.fdopen(os.open('/run/autoprinter.lock', os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600),'a')
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        setup = Setup(args, guard, report)
        setup.preflight(); setup.backup(); setup.configs(); setup.observe()
        setup.crowsnest(); setup.screen(); setup.cartographer(); setup.finish()
        print('RESULT: '+report['status']+'\nReport: '+str(setup.state/'report.json'))
        return 0
    except (ap.Refuse, OSError, ValueError, KeyError, TypeError) as error:
        print('ACCESSORY SETUP STOPPED: '+str(error), flush=True)
        # Never start/stop Klipper or attempt automatic rollback after a failure.
        if setup and setup.state and guard and not guard.cancelled.is_set():
            setup.report['status']='INCOMPLETE'; setup.report['error']=str(error)
            with contextlib.suppress(OSError, ap.Refuse):
                setup.write(setup.state/'report.json',json.dumps(setup.report,indent=2),mode=0o600)
        return 2
    finally:
        if setup and setup.log: setup.log.close()
        if isinstance(guard, ap.USBGuard): guard.close()
        if lock: lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
