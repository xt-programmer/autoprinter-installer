#!/usr/bin/env python3
"""Plan/install only the two 500 config files. No packages, MCU I/O or restarts."""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import platform
import signal
import autoprinter as ap
import config_profiles as cp
import peripheral_history as ph
from host_setup import SSHGuard


def arguments(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('--printer-profile',choices=cp.PROFILES,default='auto')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--maintenance-confirmed',action='store_true')
    parser.add_argument('--supervised-ssh',action='store_true')
    parser.add_argument('--ssh-session-pid',type=int)
    for name in ('port','vid','pid','serial'): parser.add_argument('--usb-'+name)
    return parser.parse_args(argv)


def main(argv=None):
    args=arguments(argv); guard=lock=None; state=None
    report={'mode':'apply' if args.apply else 'audit','status':'NOT_READY','changes':[]}
    try:
        if platform.system()!='Linux': raise ap.Refuse('Run on the target Linux Pi')
        config=Path('/home/pi/printer_data/config')
        planned=cp.plan(config,args.printer_profile,bool(ph.current()['cartographer']))
        report['config_plan']=planned
        if not args.apply:
            report['status']='AUDIT_ONLY'; print(json.dumps(report,indent=2)); return 0
        if os.geteuid()!=0 or not args.maintenance_confirmed: raise ap.Refuse('Apply requires sudo and --maintenance-confirmed')
        if args.supervised_ssh:
            if args.usb_port or not args.ssh_session_pid: raise ap.Refuse('SSH supervision needs its live shell PID and no USB selector')
            guard=SSHGuard(args.ssh_session_pid)
        else:
            selector={k:getattr(args,'usb_'+k) for k in ('port','vid','pid','serial') if getattr(args,'usb_'+k) is not None}
            trigger=ap.select_usb(ap.usb_devices(),selector)
            inherited=os.environ.get('AUTOPRINTER_TRIGGER_SESSION')
            if inherited and any(trigger.get(k)!=v for k,v in json.loads(inherited).items()): raise ap.Removed('Trigger changed during download')
            guard=ap.USBGuard(trigger).start()
        def interrupted(signum,frame): guard.cancelled.set()
        signal.signal(signal.SIGINT,interrupted); signal.signal(signal.SIGTERM,interrupted)
        import fcntl
        lock=os.fdopen(os.open('/run/autoprinter.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600),'a')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        host=ap.inspect_host()
        # Config-only mode performs no APT work; Debian 11/Python 3.9 is allowed.
        hardware=dict(host,os={'ID':'debian','VERSION_ID':'12'})
        ap.supported_host(hardware)
        runner=ap.Runner(guard)
        def inactive():
            _,active=runner.run(['systemctl','is-active','klipper'],check=False,timeout=15)
            if active!='inactive': raise ap.Refuse('Config changes require inactive Klipper; no services will be stopped automatically')
        inactive(); cp.validate(planned)
        if not planned['changes_required']:
            report['status']='CONFIG_FILES_PRESERVED'; print(json.dumps(report,indent=2)); return 0
        ap.check_free(['/','/var','/home/pi'])
        guard.check()
        state=Path('/var/lib/autoprinter')/('configs-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S-%f'))
        state.mkdir(parents=True,mode=0o700)
        def write(path,data,mode): ap.atomic_write(path,data,guard,mode)
        report['backup']=str(state/'config-files-before')
        write(state/'report.json',json.dumps(report,indent=2),0o600)
        changed=cp.apply(config,planned,state/'config-files-before',guard,write,
                         ownership=lambda path:runner.run(['chown','pi:pi',path]),before_write=inactive)
        report.update(status='CONFIG_FILES_UPDATED_REVIEW_REQUIRED',changes=changed)
        write(state/'report.json',json.dumps(report,indent=2),0o600)
        print(json.dumps(report,indent=2)); return 0
    except (ap.Refuse,OSError,ValueError,KeyError,TypeError) as error:
        report.update(status='INCOMPLETE' if state else 'BLOCKED',error=str(error))
        if state and guard and not guard.cancelled.is_set():
            with contextlib.suppress(OSError,ap.Refuse): ap.atomic_write(state/'report.json',json.dumps(report,indent=2),guard)
        print(json.dumps(report,indent=2)); return 2
    finally:
        if isinstance(guard,ap.USBGuard): guard.close()
        if lock: lock.close()


if __name__=='__main__': raise SystemExit(main())
