#!/usr/bin/env python3
"""Generate visible, one-shot Flipper/Digispark launchers for a logged-in Pi shell.

No login credentials, desktop shortcuts, target discovery or implicit flash.
Generation is local and never connects to a printer. Review the typed command.
"""
import argparse
import json
from pathlib import Path
import re
import shlex


TAG = 'v0.5.0'
REPOSITORY = 'xt-programmer/autoprinter-installer'


def command(mode, device, extra=(), vid=None, pid=None, serial=None):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--device', choices=['flipper','digispark'], required=True)
    parser.add_argument('--mode', choices=['inventory','audit','peripherals','configs','board-flash','full'], default='inventory')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true', help='send Enter once after typing; default is review-only')
    parser.add_argument('--vid'); parser.add_argument('--pid'); parser.add_argument('--serial')
    parser.add_argument('arguments', nargs=argparse.REMAINDER, help='installer arguments after --')
    args = parser.parse_args(argv)
    extra = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    try:
        content, typed = render(args.device,args.mode,extra,args.execute,args.vid,args.pid,args.serial)
        if not args.output.parent.is_dir(): raise ValueError('Output parent must already exist')
        with args.output.open('x',encoding='ascii',newline='\n') as stream: stream.write(content)
    except (ValueError, OSError) as error: parser.error(str(error))
    print('Created ' + str(args.output) + '\nTyped command:\n' + typed)
    return 0


if __name__ == '__main__': raise SystemExit(main())
