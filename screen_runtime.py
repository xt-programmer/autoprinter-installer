"""Native KlipperScreen X11 adoption and startup checks; no I/O at import."""
import shlex

MARKER='# AutoPrinter managed native X11 runtime.\n'
X11_EXEC='/usr/bin/xinit /usr/bin/setpriv --inh-caps=-all --ambient-caps=-all /home/pi/.KlipperScreen-env/bin/python /home/pi/KlipperScreen/screen.py -- /usr/bin/X :0 vt7 -keeptty -nolisten tcp'
LAUNCH_KEYS={'User','WorkingDirectory','ExecStart','ExecStartPost','Environment','TTYPath'}


def service_fields(text):
    section=None; values={}
    for line in text.splitlines():
        line=line.strip()
        if not line or line.startswith(('#',';')): continue
        if line.startswith('['): section=line
        elif section=='[Service]' and '=' in line:
            key,value=line.split('=',1)
            if key in LAUNCH_KEYS: values.setdefault(key,[]).append(shlex.split(value))
    return values


def native_x11_override(unit,expected,dropins=(),custom_launcher=False):
    # Preserve custom launch/backends; do not bypass an operator launch script.
    if custom_launcher or service_fields(unit)!=service_fields(expected):
        raise ValueError('Customized KlipperScreen launcher/backend needs review before native X11 repair')
    for text in dropins:
        if text.startswith(MARKER):
            if text!=x11_override(): raise ValueError('Edited managed KlipperScreen drop-in needs review')
        elif service_fields(text):
            raise ValueError('Another KlipperScreen drop-in overrides launch settings; preserve/review it')
    return x11_override()


def x11_override():
    return MARKER+'''[Unit]
StartLimitIntervalSec=120
StartLimitBurst=3

[Service]
# Keep Xorg and the service on tty7. Strip capabilities only from the GUI
# client; retain glycin/bubblewrap sandboxing and normal Xorg privileges.
ExecStart=
ExecStart=%s
ExecStartPost=
RestartSec=10
'''%X11_EXEC


def check_service(text,expected_pid=None,expected_restarts=0):
    props=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
    if props.get('ActiveState')!='active' or props.get('SubState')!='running':
        raise ValueError('KlipperScreen is not active/running: '+str(props))
    try: pid=int(props['MainPID']); restarts=int(props['NRestarts'])
    except (KeyError,ValueError): raise ValueError('Missing KlipperScreen runtime facts')
    if pid<=0 or restarts<0 or restarts!=expected_restarts or expected_pid is not None and pid!=expected_pid:
        raise ValueError('KlipperScreen restarted/exited during startup')
    return pid


def check_startup_log(text):
    if any(x in text for x in ('Uncaught exception','Fatal error in main loop','Failed to load image')):
        raise ValueError('KlipperScreen reported a fresh startup/rendering error')
    if 'Screen resolution:' not in text or 'Current panel hierarchy:' not in text:
        raise ValueError('No fresh KlipperScreen display/panel startup evidence')
    return True
