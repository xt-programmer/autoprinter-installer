"""Trigger selection and typed-command policy; never sends keyboard events."""
import contextlib
import io
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bootstrap
import make_hid
import test_download_and_flags as downloader_tests


class HIDTests(unittest.TestCase):
    def setUp(self):
        # NTFS cannot represent Linux's colon-containing sysfs interface names.
        self.separator = '_' if os.name == 'nt' else ':'
        if os.name == 'nt':
            original = Path.glob
            replacement = patch.object(Path,'glob',lambda root, pattern: original(root,pattern.replace(':','_')))
            replacement.start(); self.addCleanup(replacement.stop)

    def device(self, root, port, serial='', cls='03', product='Supervised launcher'):
        directory = root / port
        directory.mkdir()
        for key, value in {'idVendor':'1234','idProduct':'abcd','serial':serial,
                           'busnum':'1','devnum':'4','product':product}.items():
            (directory / key).write_text(value)
        interface = root / (port + self.separator + '1.0')
        interface.mkdir()
        (interface / 'bInterfaceClass').write_text(cls)
        return directory

    def test_unique_hid_resolves_port_with_serial_and_product(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.device(root,'1-1','TARGET')
            self.device(root,'1-2','OTHER')
            found, identity = bootstrap.hid_identity('1234','abcd','TARGET','Supervised launcher',root)
            self.assertEqual(found.name,'1-1')
            self.assertEqual(identity['devnum'],'4')

    def test_missing_ambiguous_non_hid_or_wrong_product_refuses(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            for cls, product in [('ff','Supervised launcher'),('03','Wrong product')]:
                directory=self.device(root,'1-1',cls=cls,product=product)
                with self.assertRaises(RuntimeError): bootstrap.hid_identity('1234','abcd',product='Supervised launcher',root=root)
                import shutil
                shutil.rmtree(directory); shutil.rmtree(root/('1-1'+self.separator+'1.0'))
            self.device(root,'1-1'); self.device(root,'1-2')
            with self.assertRaises(RuntimeError): bootstrap.hid_identity('1234','abcd',root=root)

    def test_partial_hid_selector_refuses_before_download(self):
        with patch.object(bootstrap.sys,'platform','linux'), patch.object(bootstrap.urllib.request,'build_opener') as download, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bootstrap.main(['--url','https://example.org/a.zip','--sha256','0'*64,'--hid-vid','1234'])
        download.assert_not_called()

    def test_hid_cannot_override_an_explicit_port(self):
        with patch.object(bootstrap.sys,'platform','linux'), patch.object(bootstrap,'hid_identity') as resolve, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bootstrap.main(['--url','https://example.org/a.zip','--sha256','0'*64,'--hid-vid','1234','--hid-pid','abcd','--usb-port','1-1'])
        resolve.assert_not_called()

    def test_resolved_hid_hands_frozen_enumeration_to_child_without_apply(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            directory=self.device(root,'1-1')
            resolved=bootstrap.usb_identity('1-1',root=root)
            output=root/'download'; output.mkdir()
            with patch.object(bootstrap,'hid_identity',return_value=resolved):
                result=downloader_tests.DownloadTests().invoke(output,['--hid-vid','1234','--hid-pid','abcd'])
            command=result.call_args.args[1]
            self.assertIn('1-1',command); self.assertIn('--usb-port',command)
            self.assertNotIn('--inventory',command); self.assertNotIn('--apply',command)

    def test_default_flipper_is_inventory_and_leaves_enter_to_operator(self):
        payload, typed=make_hid.render('flipper')
        self.assertTrue(payload.startswith('ID 1234:abcd '))
        self.assertNotIn('\nENTER\n',payload)
        self.assertIn('--inventory',typed); self.assertNotIn('--apply',typed)
        self.assertIn('sudo -n',typed); self.assertIn('pipefail',typed)

    def test_explicit_execute_sends_enter_once_and_flashing_is_never_implicit(self):
        payload, typed=make_hid.render('flipper','audit',execute=True)
        self.assertEqual(payload.splitlines().count('ENTER'),1)
        self.assertIn('--hid-product',typed)
        self.assertNotIn('--flash',typed); self.assertNotIn('--apply',typed)
        with self.assertRaises(ValueError): make_hid.render('flipper','audit',['--apply'])
        with self.assertRaises(ValueError): make_hid.render('flipper','board-flash',['--flash'])

    def test_arguments_remain_shell_literals_and_cannot_change_selector(self):
        value='/local/machine with spaces;touch UNWANTED.json'
        _,typed=make_hid.render('flipper','full',['--config',value])
        shell=shlex.split(typed.split('; ap_rc=',1)[0])
        pipeline=shlex.split(shell[4])
        self.assertEqual(pipeline[pipeline.index('--config')+1],value)
        for value in ['--hid-vid=ffff','--usb-port','--supervised-ssh','--url=x']:
            with self.assertRaises(ValueError): make_hid.render('flipper','full',[value])
        with self.assertRaises(ValueError): make_hid.render('flipper','full',['\nENTER'])

    def test_digispark_uses_flash_storage_and_polls_without_repeating(self):
        sketch, typed=make_hid.render('digispark','audit')
        self.assertIn('PROGMEM',sketch); self.assertIn('pgm_read_byte',sketch)
        self.assertIn('void loop() { DigiKeyboard.delay(1000); }',sketch)
        self.assertNotIn('sendKeyStroke(KEY_ENTER)',sketch)
        self.assertIn('27db',typed)


if __name__=='__main__': unittest.main()
