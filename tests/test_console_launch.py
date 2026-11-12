"""Automatic console payload policy; no real login, credential or firmware I/O."""
import ast
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bootstrap
import console_launch as console
import make_hid


class ConsoleTests(unittest.TestCase):
    def request(self):
        return {'username':'pi','password':'unit-test-credential','vt':3,'tag':'v0.6.0',
                'flags':['--hid-vid','1234','--hid-pid','abcd','--inventory'],
                'trigger':{'vid':'1234','pid':'abcd'},'expected_trigger':{},'launcher_sha256':None}

    def test_login_requires_expected_pi_and_tty(self):
        request=self.request()
        with patch.object(console.sys,'platform','linux'), patch.object(console,'logged_user',return_value='pi'), patch.object(console.os,'ttyname',return_value='/dev/tty3',create=True):
            self.assertEqual(console.validate(request),request['password'])
            for tty in ('/dev/pts/1','/dev/tty2'):
                with patch.object(console.os,'ttyname',return_value=tty), self.assertRaises(RuntimeError): console.validate(request)
            with patch.object(console,'logged_user',return_value='root'), self.assertRaises(RuntimeError): console.validate(request)

    def test_config_cannot_substitute_ssh_or_download_selector(self):
        request=self.request()
        with patch.object(console.sys,'platform','linux'), patch.object(console,'logged_user',return_value='pi'), patch.object(console.os,'ttyname',return_value='/dev/tty3',create=True):
            for flag in ('--supervised-ssh','--usb-port=x','--url=x','--sha256=x'):
                request['flags']=[flag]
                with self.assertRaises(RuntimeError): console.validate(request)

    def decode(self,payload):
        pieces=[]
        for line in payload.splitlines():
            if line.startswith('STRING AP_AUTOPRINTER_CAPSULE='):
                pieces.append(re.search(r"'([A-Za-z0-9+/=]+)'$",line).group(1))
                self.assertLess(len(line),512)
        self.assertTrue(pieces)
        return zlib.decompress(base64.b64decode(''.join(pieces))).decode()

    def test_console_flipper_switches_logs_in_and_executes_without_manual_enter(self):
        payload=make_hid.console_payload('flipper','inventory','unit-test-credential')
        lines=payload.splitlines()
        self.assertIn('CTRL ALT F3',lines)
        self.assertIn('STRING pi',lines); self.assertIn('STRING unit-test-credential',lines)
        self.assertIn('STRING unset HISTFILE; set +o history',lines)
        self.assertGreater(lines.count('ENTER'),3)
        code=self.decode(payload); ast.parse(code,feature_version=(3,9))
        self.assertIn("'--inventory'",code); self.assertNotIn("'--apply'",code)
        self.assertIn(hashlib.sha256(console.bundled_source()).hexdigest(),code)
        self.assertIn("'expected_trigger'",code)
        self.assertNotIn('assert ',code)
        final=next(i for i,x in enumerate(lines) if x.startswith('STRING python3 -c '))
        self.assertEqual(lines[final+1],'ENTER')
        self.assertFalse(any(x=='ENTER' or x.startswith('STRING ') for x in lines[final+2:]))
        self.assertIn('; ap_rc=$?; unset AP_AUTOPRINTER_CAPSULE;',lines[final])
        self.assertTrue(lines[final].endswith('exit "$ap_rc"'))

    def test_placeholder_words_in_password_are_not_replaced(self):
        payload=make_hid.console_payload('flipper','audit','HELPER_URL HELPER_HASH REQUEST')
        self.assertIn("'password': 'HELPER_URL HELPER_HASH REQUEST'",self.decode(payload))

    def test_console_generation_rejects_control_input_and_flash_without_optin(self):
        for secret in ('','x\nENTER','x\tX','\u00e9'):
            with self.assertRaises(ValueError): make_hid.console_payload('flipper','inventory',secret)
        with self.assertRaises(ValueError): make_hid.console_payload('flipper','inventory','test',vt=7)
        with self.assertRaises(ValueError): make_hid.console_payload('flipper','inventory','test',login_wait_ms=0)
        with self.assertRaises(ValueError): make_hid.console_payload('flipper','board-flash','test',['--flash'])

    def test_digispark_automatic_login_is_one_shot_and_flash_stored(self):
        sketch=make_hid.console_payload('digispark','audit','unit-test-credential')
        self.assertIn('PROGMEM',sketch)
        self.assertIn('DigiKeyboard.sendKeyStroke(60, MOD_CONTROL_LEFT | MOD_ALT_LEFT);',sketch)
        self.assertIn('void loop() { DigiKeyboard.delay(1000); }',sketch)
        self.assertIn('sendKeyStroke(KEY_ENTER)',sketch)

    def test_cli_does_not_print_credential_or_encoded_command(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); secret=root/'secret'; secret.write_text('unit-test-credential\n')
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                make_hid.main(['--device','flipper','--console-login','--password-file',str(secret),'--output',str(root/'payload.txt')])
            self.assertNotIn('unit-test-credential',output.getvalue())
            self.assertNotIn('AP_AUTOPRINTER_CAPSULE',output.getvalue())

    def run_helper(self,root,tampered=False,changed=False):
        device=root/'1-1'; device.mkdir()
        identity={'idVendor':'1234','idProduct':'abcd','serial':'','busnum':'1','devnum':'2'}
        for k,v in identity.items(): (device/k).write_text(v)
        request=self.request()
        session={'port':'1-1','vid':'1234','pid':'abcd','serial':'','busnum':'1','devnum':'2'}
        request['expected_trigger']=dict(session,devnum='1') if changed else session
        data=b'#!/bin/sh\n# test-only launcher\n'
        request['launcher_sha256']='0'*64 if tampered else hashlib.sha256(data).hexdigest()
        response=io.BytesIO(data); response.geturl=lambda:'https://raw.githubusercontent.com/test'
        opener=Mock(); opener.open.return_value=response
        process=Mock(); process.poll.return_value=3; process.returncode=3
        directory=root/'download'; directory.mkdir()
        with patch.object(console,'validate',return_value='unit-test-credential'), patch.object(console.bootstrap,'hid_identity',return_value=(device,identity)), patch.object(console.tempfile,'mkdtemp',return_value=str(directory)), patch.object(console.shutil,'disk_usage',return_value=Mock(free=1024**3)), patch.object(console.urllib.request,'build_opener',return_value=opener), patch.object(console.subprocess,'Popen',return_value=process) as start, patch.object(console.os,'read',return_value=b''), contextlib.redirect_stdout(Mock()):
            rc=console.launch(request,root/'logs')
        return rc,start,process

    def test_expected_enumeration_or_launcher_tampering_never_authorizes_sudo(self):
        for tampered,changed in ((True,False),(False,True)):
            with tempfile.TemporaryDirectory() as t:
                rc,start,_=self.run_helper(Path(t),tampered,changed)
                self.assertEqual(rc,2); start.assert_not_called()

    def test_sudo_uses_private_pipe_and_passes_frozen_session_without_password_in_argv(self):
        with tempfile.TemporaryDirectory() as t:
            rc,start,process=self.run_helper(Path(t))
            self.assertEqual(rc,3)
            argv=start.call_args.args[0]
            self.assertEqual(argv[:3],['sudo','-S','-p'])
            self.assertNotIn('unit-test-credential',' '.join(argv))
            self.assertTrue(any(x.startswith('AUTOPRINTER_TRIGGER_SESSION=') for x in argv))
            process.stdin.write.assert_called_once_with(b'unit-test-credential\n')

    def test_root_downloader_rejects_a_stale_console_handover_before_network(self):
        with tempfile.TemporaryDirectory() as t:
            device=Path(t)/'1-1'; device.mkdir()
            identity={'idVendor':'1234','idProduct':'abcd','serial':'','busnum':'1','devnum':'2'}
            with patch.object(bootstrap.sys,'platform','linux'), patch.object(bootstrap,'usb_identity',return_value=(device,identity)), patch.dict(os.environ,{'AUTOPRINTER_TRIGGER_SESSION':json.dumps({'port':'1-1','devnum':'1'})}), patch.object(bootstrap.urllib.request,'build_opener') as network, self.assertRaises(RuntimeError):
                bootstrap.main(['--url','https://example.org/bundle.zip','--sha256','0'*64,'--usb-port','1-1'])
            network.assert_not_called()

    def test_downloadable_helper_is_deterministic_python39_and_has_no_service_install(self):
        first=console.bundled_source(); self.assertEqual(first,console.bundled_source())
        ast.parse(first.decode(),feature_version=(3,9))
        namespace={'__name__':'console_test'}
        with patch.dict(sys.modules): exec(compile(first,'<test>','exec'),namespace)
        self.assertTrue(callable(namespace['launch']))
        self.assertNotIn('systemctl enable',first.decode())
        self.assertNotIn('udevadm trigger',first.decode())


if __name__=='__main__': unittest.main()
