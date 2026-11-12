#!/usr/bin/env python3
"""Generate one-shot Flipper/Digispark launchers, optionally with console login.

Console-login output is private and contains the supplied login credential.
Generation is local and never connects to a printer. No resident worker.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import zlib


TAG = 'v0.6.2'
REPOSITORY = 'xt-programmer/autoprinter-installer'


def invocation(mode, device, extra=(), vid=None, pid=None, serial=None):
    if mode not in ('inventory', 'audit', 'peripherals', 'configs', 'board-flash', 'full'):
        raise ValueError('Unknown launcher mode')
    if device not in ('flipper', 'digispark'):
        raise ValueError('Unknown keyboard device')
    # The Flipper script sets this lab-only selector before enumeration. The
    # Digispark must use the actual VID/PID of its installed keyboard firmware.
    vid, pid = vid or ('1234' if device == 'flipper' else '16c0'), pid or ('abcd' if device == 'flipper' else '27db')
    if not all(re.fullmatch('[0-9a-f]{4}', x) for x in (vid, pid)):
        raise ValueError('VID/PID must be four lowercase hexadecimal digits')
    extra = list(extra)
    if any(not isinstance(x, str) or any(ord(c) < 32 or ord(c) > 126 for c in x) for x in extra):
        raise ValueError('Only printable ASCII arguments are supported')
    owned = {'--url','--sha256','--usb-port','--usb-vid','--usb-pid','--usb-serial',
             '--hid-vid','--hid-pid','--hid-serial','--hid-product', '--supervised-ssh',
             '--ssh-session-pid', '--inventory','--peripherals','--configs','--board-flash'}
    if any(x.split('=', 1)[0] in owned for x in extra):
        raise ValueError('Mode/download/trigger options belong to the generator')
    if mode == 'inventory' and extra:
        raise ValueError('Inventory takes no additional maintenance arguments')
    if mode == 'audit' and any(x.split('=', 1)[0] in ('--apply','--verify','--flash','--allow-prune') for x in extra):
        raise ValueError('Audit mode cannot request maintenance')
    flags = ['--inventory'] if mode == 'inventory' else ['--hid-vid', vid, '--hid-pid', pid]
    if mode != 'inventory':
        if device == 'flipper': flags += ['--hid-product', 'Supervised launcher']
        if serial is not None: flags += ['--hid-serial', serial]
        if mode in ('peripherals', 'configs', 'board-flash'): flags += ['--' + mode]
    flags += extra
    if '--flash' in extra and not all(x in extra for x in ('--apply','--enable-flashing-capability','--maintenance-confirmed')):
        raise ValueError('Flash needs explicit apply, capability and maintenance flags')
    if '--apply' in extra and '--maintenance-confirmed' not in extra:
        raise ValueError('Apply needs --maintenance-confirmed')
    return flags,vid,pid


def command(mode, device, extra=(), vid=None, pid=None, serial=None):
    flags,vid,pid=invocation(mode,device,extra,vid,pid,serial)
    url = 'https://raw.githubusercontent.com/%s/%s/install.sh' % (REPOSITORY, TAG)
    # pipefail reports a failed first download instead of success from an empty
    # shell. sudo never prompts while receiving a pipeline on its stdin.
    pipeline = "curl --proto '=https' --proto-redir '=https' -fSL " + shlex.quote(url)
    pipeline += ' | sudo -n sh -s -- ' + shlex.join(flags)
    text = 'bash -o pipefail -c ' + shlex.quote(pipeline)
    text += '; ap_rc=$?; printf "\\nAutoPrinter exit=%s\\n" "$ap_rc"; (exit "$ap_rc")'
    return text, vid, pid


def render(device, mode='inventory', extra=(), execute=False, vid=None, pid=None, serial=None):
    typed, vid, pid = command(mode, device, extra, vid, pid, serial)
    if device == 'flipper':
        # ID must be first. Do not switch scripts/apps until the Pi is finished:
        # doing so can remove or re-enumerate the presence trigger.
        lines = ['ID %s:%s AutoPrinter:Supervised launcher' % (vid, pid),
                 'REM Logged-in Pi shell only. Authenticate sudo manually first.',
                 'REM Match the Pi keyboard layout. No passwords in this file.',
                 'DELAY 2000', 'DEFAULT_STRING_DELAY 20', 'STRING ' + typed]
        if execute: lines += ['DELAY 1000', 'ENTER']
        else: lines += ['REM Review the command, then press Enter on a real keyboard.']
        lines += ['REM Keep this script open and USB connected until the Pi reports completion.']
        return '\n'.join(lines) + '\n', typed
    # PROGMEM avoids keeping the long HTTPS command in the ATtiny85's 512-byte
    # RAM. Poll USB forever after the one transmission; never repeat in loop().
    sketch = '''// Digispark ATtiny85 + DigiKeyboard. US keyboard layout.
// Log in and run sudo -v manually before inserting. No passwords stored.
// Verify actual USB IDs match the generated --hid-vid/--hid-pid.
#include "DigiKeyboard.h"
#include <avr/pgmspace.h>
const char launchCommand[] PROGMEM = %s;
void setup() {
  DigiKeyboard.delay(5000);
  DigiKeyboard.sendKeyStroke(0);
  for (unsigned int i = 0; i < sizeof(launchCommand)-1; ++i) {
    DigiKeyboard.write(pgm_read_byte(launchCommand+i));
    DigiKeyboard.delay(20);
  }
%s
}
void loop() { DigiKeyboard.delay(1000); }
''' % (json.dumps(typed), '  DigiKeyboard.delay(1000);\n  DigiKeyboard.sendKeyStroke(KEY_ENTER);' if execute else '  // Review command and press Enter manually. No automatic execution.')
    return sketch, typed


def console_payload(device, mode, password, extra=(), vt=3, vid=None, pid=None, serial=None, launcher_sha256=None, login_wait_ms=20000):
    """Private automatic-login payload. tty must initially be at a fresh getty."""
    import console_launch
    flags,vid,pid=invocation(mode,device,extra,vid,pid,serial)
    if type(vt)!=int or not 2<=vt<=6: raise ValueError('Use a spare console tty2..tty6')
    if type(login_wait_ms)!=int or not 5000<=login_wait_ms<=120000: raise ValueError('Login wait must be 5000..120000 ms')
    if not isinstance(password,str) or not 1<=len(password)<=128 or any(not 32<=ord(c)<=126 for c in password):
        raise ValueError('Password must be a single printable ASCII line')
    if launcher_sha256 is not None and not re.fullmatch('[0-9a-f]{64}',launcher_sha256):
        raise ValueError('Invalid launcher SHA-256')
    # Even inventory has presence supervision in automatic-console mode.
    if mode=='inventory':
        flags=['--hid-vid',vid,'--hid-pid',pid]
        if device=='flipper': flags+=['--hid-product','Supervised launcher']
        if serial is not None: flags+=['--hid-serial',serial]
        flags+=['--inventory']
    trigger={'vid':vid,'pid':pid}
    if device=='flipper': trigger['product']='Supervised launcher'
    if serial is not None: trigger['serial']=serial
    request={'username':'pi','password':password,'vt':vt,'tag':TAG,'flags':flags,'trigger':trigger,'launcher_sha256':launcher_sha256}
    helper=console_launch.bundled_source()
    helper_hash=hashlib.sha256(helper).hexdigest()
    helper_url='https://github.com/%s/releases/download/%s/console-launch.py'%(REPOSITORY,TAG)
    # A small unprivileged first stage checks user/tty and captures enumeration
    # BEFORE the helper download. The expected helper digest comes from the
    # reviewed preparation source, not from that download response.
    capsule='''import os,sys,json,hashlib,urllib.request
from pathlib import Path
import pwd
r=REQUEST
if not (sys.platform=='linux' and pwd.getpwuid(os.geteuid()).pw_name=='pi' and os.ttyname(0)=='/dev/tty'+str(r['vt'])): raise RuntimeError('Unexpected login/console')
s=r['trigger']; root=Path('/sys/bus/usb/devices')
def read(p,k):
 try: return (p/k).read_text().strip()
 except OSError: return ''
ds=[p for p in root.glob('*') if ':' not in p.name and read(p,'idVendor')==s['vid'] and read(p,'idProduct')==s['pid'] and ('serial' not in s or read(p,'serial')==s['serial']) and ('product' not in s or read(p,'product')==s['product']) and any(read(i,'bInterfaceClass')=='03' for i in root.glob(p.name+':*'))]
if len(ds)!=1: raise RuntimeError('USB trigger missing/ambiguous')
p=ds[0]; keys={'port':None,'vid':'idVendor','pid':'idProduct','serial':'serial','busnum':'busnum','devnum':'devnum'}
def identity(): return {k:read(p,v) if v else p.name for k,v in keys.items()}
r['expected_trigger']=identity()
class HTTPS(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,url):
  if not url.startswith('https://'): raise RuntimeError('HTTPS required')
  return super().redirect_request(req,fp,code,msg,headers,url)
b=bytearray()
with urllib.request.build_opener(HTTPS()).open(HELPER_URL,timeout=3) as f:
 if not f.geturl().startswith('https://'): raise RuntimeError('HTTPS required')
 while True:
  if identity()!=r['expected_trigger']: raise RuntimeError('USB removed/re-enumerated')
  c=f.read(8192)
  if identity()!=r['expected_trigger']: raise RuntimeError('USB removed/re-enumerated')
  if not c: break
  b.extend(c)
  if len(b)>131072: raise RuntimeError('Helper size limit')
if hashlib.sha256(b).hexdigest()!=HELPER_HASH: raise RuntimeError('Helper checksum mismatch')
ns={'__name__':'autoprinter_console_payload'}
exec(compile(b,'<verified-console-helper>','exec'),ns)
sys.exit(ns['launch'](r))
'''.replace('HELPER_URL',repr(helper_url)).replace('HELPER_HASH',repr(helper_hash)).replace('REQUEST',repr(request))
    encoded=base64.b64encode(zlib.compress(capsule.encode(),9)).decode('ascii')
    # Each tty line stays short even with long manifests/flash arguments. All
    # chunks are base64 literals; the one process-start command is sent last.
    texts=['unset HISTFILE; set +o history', "export AP_AUTOPRINTER_CAPSULE=''" ]
    for offset in range(0,len(encoded),240):
        texts.append('AP_AUTOPRINTER_CAPSULE="$AP_AUTOPRINTER_CAPSULE"\''+encoded[offset:offset+240]+"'")
    boot="import os,base64,zlib;exec(zlib.decompress(base64.b64decode(os.environ.pop('AP_AUTOPRINTER_CAPSULE'))))"
    texts.append('python3 -c '+shlex.quote(boot)+'; ap_rc=$?; unset AP_AUTOPRINTER_CAPSULE; printf "\\nAutoPrinter exit=%s\\n" "$ap_rc"; exit "$ap_rc"')
    events=[('delay',3000),('key','CTRL ALT F'+str(vt)),('delay',5000),('text','pi'),('key','ENTER'),
            ('delay',2500),('text',password),('key','ENTER'),('delay',login_wait_ms)]
    for text in texts: events += [('text',text),('key','ENTER'),('delay',250)]
    if device=='flipper':
        lines=['ID %s:%s AutoPrinter:Supervised launcher'%(vid,pid),
               'REM PRIVATE: contains login credentials. Expected fresh tty'+str(vt)+' getty; US keyboard.',
               'REM Arm Run while disconnected, then insert. No Pi keyboard/worker needed.',
               'DEFAULT_STRING_DELAY 15']
        for kind,value in events:
            lines.append(('DELAY '+str(value)) if kind=='delay' else ('STRING '+value) if kind=='text' else value)
        lines+=['REM No more keys. Keep app/cable connected until Pi reports completion; Done means typing finished.']
        return '\n'.join(lines)+'\n'
    # Dedicated VT hotkey scancode is 0x3a for F1, then consecutive through F6.
    lines=['// PRIVATE automatic console login. US keyboard. Contains credential.',
           '#include "DigiKeyboard.h"','#include <avr/pgmspace.h>',
           'void typeFlash(const char* p) { for (unsigned int i=0; pgm_read_byte(p+i); ++i) { DigiKeyboard.write(pgm_read_byte(p+i)); DigiKeyboard.delay(15); } }']
    commands=[]; index=0
    for kind,value in events:
        if kind=='text':
            lines.append('const char text%d[] PROGMEM = %s;'%(index,json.dumps(value)))
            commands.append('typeFlash(text%d);'%index); index+=1
        elif kind=='delay': commands.append('DigiKeyboard.delay(%d);'%value)
        elif value=='ENTER': commands.append('DigiKeyboard.sendKeyStroke(KEY_ENTER);')
        else: commands.append('DigiKeyboard.sendKeyStroke(%d, MOD_CONTROL_LEFT | MOD_ALT_LEFT);'%(0x39+vt))
    lines+=['void setup() { DigiKeyboard.sendKeyStroke(0);']+['  '+x for x in commands]+['}','void loop() { DigiKeyboard.delay(1000); }']
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--device', choices=['flipper','digispark'], required=True)
    parser.add_argument('--mode', choices=['inventory','audit','peripherals','configs','board-flash','full'], default='inventory')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true', help='send Enter once after typing; default is review-only')
    parser.add_argument('--vid'); parser.add_argument('--pid'); parser.add_argument('--serial')
    parser.add_argument('--console-login',action='store_true',help='private automatic tty login and execution, no worker')
    parser.add_argument('--password-file',type=Path,help='single-line pi password; copied into private generated payload')
    parser.add_argument('--vt',type=int,default=3,help='spare fresh Linux login console, default tty3')
    parser.add_argument('--launcher-sha256',help='optional additional pin for the tag launcher')
    parser.add_argument('--login-wait-ms',type=int,default=20000,help='console shell startup wait, 5000..120000 ms')
    parser.add_argument('arguments', nargs=argparse.REMAINDER, help='installer arguments after --')
    args = parser.parse_args(argv)
    extra = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    try:
        if args.console_login:
            if args.password_file is None: raise ValueError('--console-login requires --password-file')
            password=args.password_file.read_text(encoding='utf-8').rstrip('\r\n')
            content=console_payload(args.device,args.mode,password,extra,args.vt,args.vid,args.pid,args.serial,args.launcher_sha256,args.login_wait_ms)
            typed=None
        else:
            if args.password_file or args.launcher_sha256: raise ValueError('Credential/hash options require --console-login')
            content, typed = render(args.device,args.mode,extra,args.execute,args.vid,args.pid,args.serial)
        if not args.output.parent.is_dir(): raise ValueError('Output parent must already exist')
        with args.output.open('x',encoding='ascii',newline='\n') as stream: stream.write(content)
    except (ValueError, OSError) as error: parser.error(str(error))
    print('Created ' + str(args.output) + ('\nPRIVATE automatic console-login payload; credential/commands not printed.' if typed is None else '\nTyped command:\n'+typed))
    return 0


if __name__ == '__main__': raise SystemExit(main())
