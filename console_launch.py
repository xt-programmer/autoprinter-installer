#!/usr/bin/env python3
"""Temporary BadUSB console session. No daemon, udev rule or startup task.

make_hid embeds a hash-pinned download of bundled_source(), which includes the
normal USB selector/guard. Credentials arrive only in the in-memory request.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import datetime
import bootstrap
import display_return


def bundled_source(root=None):
    """Produce one self-contained, deterministic release asset."""
    root=Path(root) if root is not None else Path(__file__).parent
    boot=(root/'bootstrap.py').read_text(encoding='utf-8')
    display=(root/'display_return.py').read_text(encoding='utf-8')
    own=(root/'console_launch.py').read_text(encoding='utf-8')
    prefix="import types,sys\n_b=types.ModuleType('bootstrap')\n"
    prefix+="exec(compile("+repr(boot)+",'<bootstrap>','exec'),_b.__dict__)\nsys.modules['bootstrap']=_b\n"
    prefix+="_d=types.ModuleType('display_return')\nexec(compile("+repr(display)+",'<display_return_module>','exec'),_d.__dict__)\nsys.modules['display_return']=_d\n"
    return (prefix+own).encode('utf-8')


def logged_user():
    import pwd
    return pwd.getpwuid(os.geteuid()).pw_name


def validate(request):
    if sys.platform!='linux': raise RuntimeError('Linux console required')
    user=request['username']; vt=request['vt']
    if user!='pi' or logged_user()!=user:
        raise RuntimeError('Expected the configured pi login, not a root/other shell')
    if type(vt)!=int or not 2<=vt<=6 or os.ttyname(0)!='/dev/tty'+str(vt):
        raise RuntimeError('Wrong controlling input console; refusing launch')
    password=request['password']
    if not isinstance(password,str) or not 1<=len(password)<=128 or any(not 32<=ord(x)<=126 for x in password):
        raise RuntimeError('Password must be one printable ASCII line')
    tag=request['tag']
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',tag): raise RuntimeError('Pinned release tag required')
    flags=request['flags']
    if not isinstance(flags,list) or any(not isinstance(x,str) or '\x00' in x or '\n' in x or '\r' in x for x in flags):
        raise RuntimeError('Invalid argument vector')
    if any(x.split('=',1)[0] in ('--supervised-ssh','--ssh-session-pid','--url','--sha256','--usb-port','--usb-vid','--usb-pid','--usb-serial') for x in flags):
        raise RuntimeError('Console launch uses its independently captured USB session')
    expected=request.get('launcher_sha256')
    if expected is not None and not re.fullmatch('[0-9a-f]{64}',expected): raise RuntimeError('Invalid launcher hash')
    return password


def launch(request, log_root=None):
    """Run once, forwarding output; the root installer owns cancellation."""
    guard=None; work=None; log=None; reader=None; process=None; session=None; password=None; rc=2
    try:
        password=validate(request)
        selector=request['trigger']
        device,identity=bootstrap.hid_identity(selector['vid'],selector['pid'],selector.get('serial'),selector.get('product'))
        session={'port':device.name,'vid':identity['idVendor'],'pid':identity['idProduct'],
                 'serial':identity['serial'],'busnum':identity['busnum'],'devnum':identity['devnum']}
        if session!=request['expected_trigger']: raise RuntimeError('USB changed during console bootstrap download')
        guard=bootstrap.Presence(device,identity).start()
        if shutil.disk_usage(tempfile.gettempdir()).free<100*1024*1024: raise RuntimeError('Less than 100 MiB temporary space')
        work=Path(tempfile.mkdtemp(prefix='autoprinter-console-'))
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        logs=Path(log_root or '/home/pi/printer_data/logs'); logs.mkdir(parents=True,exist_ok=True)
        log=(logs/('autoprinter-console-'+stamp+'.log')).open('xb')
        os.chmod(log.name,0o600)
        def emit(message):
            data=(message+'\n').encode(); sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); log.write(data); log.flush()
        emit('AutoPrinter automatic console session release='+request['tag']+'; no resident worker. Log: '+str(log.name))
        opener=urllib.request.build_opener(bootstrap.HTTPSRedirects())
        url='https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/'+request['tag']+'/install.sh'
        payload=bytearray(); started=time.monotonic()
        with opener.open(url,timeout=3) as response:
            if not response.geturl().startswith('https://'): raise RuntimeError('HTTPS required')
            while True:
                guard.check(); chunk=response.read(8192); guard.check()
                if not chunk: break
                payload.extend(chunk)
                if len(payload)>128*1024 or time.monotonic()-started>60: raise RuntimeError('Launcher download limit')
        expected=request.get('launcher_sha256')
        if expected and hashlib.sha256(payload).hexdigest()!=expected: raise RuntimeError('Launcher checksum mismatch')
        launcher=work/'install.sh'; launcher.write_bytes(payload); os.chmod(launcher,0o600)
        guard.check()
        # env is a root command argument, not sudo's filtered caller environment.
        # sudo reads the credential from a private pipe, not guessed prompt timing.
        command=['sudo','-S','-p','','--','/usr/bin/env','AUTOPRINTER_TRIGGER_SESSION='+json.dumps(session),
                 'PYTHONDONTWRITEBYTECODE=1','DEBIAN_FRONTEND=noninteractive','/bin/sh',str(launcher)]+request['flags']
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        process.stdin.write((password+'\n').encode()); process.stdin.close()
        request.pop('password',None)
        def output():
            written=0
            while True:
                chunk=os.read(process.stdout.fileno(),8192)
                if not chunk: break
                sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
                if written<8*1024*1024:
                    log.write(chunk[:8*1024*1024-written]); log.flush(); written+=len(chunk)
        reader=threading.Thread(target=output); reader.start()
        removed=False
        while process.poll() is None:
            try: guard.check()
            except RuntimeError:
                if not removed:
                    emit('USB cancellation observed. The root installer guard stops its active work; no automatic restart.')
                    removed=True
            time.sleep(.1)
        reader.join(); process.stdout.close()
        rc=130 if removed else process.returncode
        emit('Installer exit='+str(rc)+'. Completing console session/display return.')
    except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as error:
        print('CONSOLE LAUNCH REFUSED: '+str(error),flush=True); rc=2
    finally:
        request.pop('password',None)
        if reader and reader.is_alive(): reader.join()
        # Wait for actual completion, not a timed HID hotkey. Never start a
        # stopped GUI or do more work after removal/cancellation/signal exit.
        if guard and session and password and 0<=rc<128 and process is not None and process.poll() is not None:
            try:
                guard.check()
                command=['sudo','-S','-p','','--','/usr/bin/python3','-I','-B','-c',display_return.ROOT_CODE,json.dumps(session)]
                returned=subprocess.run(command,input=(password+'\n').encode(),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=30)
                message=returned.stdout.decode(errors='replace').strip()
                if message: emit(message)
                if returned.returncode==130: rc=130
            except RuntimeError:
                rc=130
            except (OSError,subprocess.SubprocessError) as error:
                print('DISPLAY RETURN FAILED: '+str(error),flush=True)
        password=None
        if log: emit('AutoPrinter session exit='+str(rc)+'. Review result; completion alone does not mean printer ready.')
        if log: log.close()
        if guard: guard.close()
        # No configuration/service/firmware cleanup. This is our private generic
        # downloaded launcher, not a retained installer backup or credential file.
        if work: shutil.rmtree(work)
    return rc
