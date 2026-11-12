"""One-shot privileged VT return. Fixed commands only; never starts a service.

ROOT_CODE is sent to sudo/python on completion, without installing a worker.
Execute the same code into this module for policy tests and diagnostics.
"""

ROOT_CODE = r'''
import json,os,re,subprocess,sys
from pathlib import Path

def usb_matches(session,usb_root=Path('/sys/bus/usb/devices')):
    if not isinstance(session,dict) or not re.fullmatch(r'\d+-\d+(?:\.\d+)*',str(session.get('port',''))): return False
    if any(not re.fullmatch('[0-9a-f]{4}',str(session.get(k,''))) for k in ('vid','pid')): return False
    if any(not re.fullmatch('[1-9][0-9]*',str(session.get(k,''))) for k in ('busnum','devnum')): return False
    if not isinstance(session.get('serial'),str): return False
    device=usb_root/session['port']
    for field,key in [('vid','idVendor'),('pid','idProduct'),('serial','serial'),('busnum','busnum'),('devnum','devnum')]:
        try: value=(device/key).read_text().strip()
        except OSError: value=''
        if value!=session[field]: return False
    return True

def service_state(runner):
    p=runner(['systemctl','show','KlipperScreen','-p','ActiveState','-p','SubState','-p','MainPID','-p','TTYPath'],capture_output=True,text=True,timeout=10)
    if p.returncode: raise RuntimeError('Cannot read KlipperScreen service state')
    return dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)

def process_identity(pid,proc_root):
    # Field 22 (starttime) after comm, whose text can itself contain spaces.
    return (proc_root/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[19]

def display_vt(main_pid,proc_root=Path('/proc')):
    pending=[main_pid]; seen=set(); vts=set()
    while pending:
        pid=pending.pop()
        if pid in seen: continue
        seen.add(pid)
        if len(seen)>128: raise RuntimeError('Display process tree is too large/ambiguous')
        base=proc_root/str(pid)
        try:
            children=(base/'task'/str(pid)/'children').read_text().split()
            pending.extend(int(x) for x in children)
        except OSError: pass
        try:
            comm=(base/'comm').read_text().strip()
            if comm not in ('Xorg','X','Xwayland','weston','cage'): continue
            argv=(base/'cmdline').read_bytes().split(b'\0')
            explicit={int(x[2:]) for x in argv if re.fullmatch(rb'vt[1-9][0-9]*',x)}
            if explicit: vts.update(explicit); continue
            tty_nr=int((base/'stat').read_text().rsplit(')',1)[1].split()[4])
            # Linux virtual consoles: character-device major 4, minors 1..63.
            if 1025<=tty_nr<=1087: vts.add(tty_nr-1024)
        except (OSError,ValueError,IndexError): continue
    if len(vts)!=1 or not 1<=next(iter(vts))<=63:
        raise RuntimeError('Cannot identify one actual KlipperScreen display VT')
    return next(iter(vts))

def restore(session,usb_root=Path('/sys/bus/usb/devices'),proc_root=Path('/proc'),dev_root=Path('/dev'),runner=None):
    runner=runner or subprocess.run
    if not usb_matches(session,usb_root): return 130,{'status':'SKIPPED_USB_CHANGED'}
    props=service_state(runner)
    if props.get('ActiveState')!='active' or props.get('SubState')!='running':
        return 3,{'status':'SKIPPED_KLIPPERSCREEN_NOT_RUNNING'}
    pid=int(props.get('MainPID','0'))
    if pid<=0: raise RuntimeError('No live KlipperScreen main process')
    started=process_identity(pid,proc_root)
    vt=display_vt(pid,proc_root)
    if not (dev_root/('tty'+str(vt))).is_char_device(): raise RuntimeError('Display VT device is absent')
    current=service_state(runner)
    if any(current.get(k)!=props.get(k) for k in ('ActiveState','SubState','MainPID')) or process_identity(pid,proc_root)!=started:
        raise RuntimeError('KlipperScreen changed before display return')
    if not usb_matches(session,usb_root): return 130,{'status':'SKIPPED_USB_CHANGED'}
    p=runner(['/usr/bin/chvt',str(vt)],capture_output=True,text=True,timeout=10)
    if p.returncode: raise RuntimeError('chvt could not restore the GUI: '+p.stderr.strip())
    return 0,{'status':'RETURNED_TO_RUNNING_KLIPPERSCREEN','vt':vt,'main_pid':pid}

def main(argv=None):
    try:
        if sys.platform!='linux' or os.geteuid()!=0: raise RuntimeError('Root Linux display-return command required')
        values=sys.argv[1:] if argv is None else argv
        if len(values)!=1: raise RuntimeError('One captured USB session required')
        rc,result=restore(json.loads(values[0]))
        print('DISPLAY_RETURN_JSON '+json.dumps(result),flush=True)
        return rc
    except (OSError,ValueError,RuntimeError,IndexError,subprocess.SubprocessError) as error:
        print('DISPLAY_RETURN_JSON '+json.dumps({'status':'DISPLAY_RETURN_FAILED','error':str(error)}),flush=True)
        return 2

if __name__=='__main__': sys.exit(main())
'''

exec(compile(ROOT_CODE, '<display_return>', 'exec'))
