import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import peripherals as p
import peripheral_history as ph
import autoprinter as ap


class AccessoryPolicyTests(unittest.TestCase):
    def test_camera_eoi_allows_only_bounded_zero_padding(self):
        frame=b'\xff\xd8frame\xff\xd9'
        for pad in (b'',b'\x00',b'\x00'*16): self.assertTrue(p.complete_jpeg(frame+pad))
        for bad in (frame+b'\x00'*17,frame+b'X',frame[:-2],b'not-a-jpeg\xff\xd9'):
            self.assertFalse(p.complete_jpeg(bad))

    def snapshot(self):
        return {'usb':[], 'usb_webcam':[], 'cartographer':[]}

    def test_no_positive_detection_skips_both_conditional_installers(self):
        result=p.plan(self.snapshot(),{'schema':1,'devices':{}})
        self.assertEqual(result['install'],{'usb_webcam':False,'cartographer':False,'KlipperScreen':True})

    def test_history_latches_after_unplug_for_both_types(self):
        snapshot=self.snapshot()
        snapshot.update(usb_webcam=[{'product':'USB Camera'}],cartographer=[{'product':'Cartographer'}])
        history=ph.merge({'schema':1,'devices':{}},snapshot)
        self.assertTrue(all(p.plan(self.snapshot(),history)['install'].values()))

    def test_uvc_journal_is_camera_only(self):
        result=p.plan(self.snapshot(),{'schema':1,'devices':{}},'uvcvideo: Found UVC 1.00 device C270 (046d:0825)')
        self.assertTrue(result['install']['usb_webcam'])
        self.assertFalse(result['install']['cartographer'])

    def test_cartographer_skip_never_runs_installer_or_creates_source(self):
        setup=p.Setup(p.arguments([]),Mock(),{'install':{'cartographer':False}})
        setup.source=Mock(); setup.run=Mock()
        setup.cartographer()
        setup.source.assert_not_called(); setup.run.assert_not_called()
        self.assertEqual(setup.report['cartographer'],'SKIPPED_NO_POSITIVE_DETECTION')

    def test_camera_skip_never_runs_installer(self):
        setup=p.Setup(p.arguments([]),Mock(),{'install':{'usb_webcam':False}})
        setup.source=Mock(); setup.run=Mock()
        setup.crowsnest()
        setup.source.assert_not_called(); setup.run.assert_not_called()

    def test_unplug_preserves_managed_camera_and_frontend_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)
            (source/'resources').mkdir()
            (source/'resources/moonraker_update.txt').write_text('[update_manager crowsnest]\n')
            setup=p.Setup(p.arguments([]),Mock(),{'install':{'usb_webcam':True},'current':self.snapshot(),'preserve_existing_camera_config':True})
            setup.source=Mock(return_value=source); setup.run=Mock(); setup.write=Mock()
            setup.original_include='[webcam USB Camera]\nstream_url: /webcam/?action=stream\n\n[update_manager crowsnest]\n'
            with patch.object(Path,'exists',return_value=True),patch.object(Path,'is_file',return_value=True): setup.crowsnest()
            setup.write.assert_not_called()
            self.assertTrue(any('[webcam USB Camera]' in text for text in setup.sections))

    def test_base_installer_preserves_native_port_80_site(self):
        from test_safety import Guard
        inst=ap.Installer(ap.arguments([]),{},Guard())
        inst.command=Mock(); inst.change=Mock()
        native=Mock()
        native.exists.return_value=True
        native.read_text.return_value='server { listen 80; root /home/pi/mainsail; location /websocket {} location /webcam/ {} }'
        with patch.object(ap,'Path',return_value=native),patch.object(ap,'atomic_write') as write:
            inst.install_nginx()
        write.assert_not_called()
        inst.command.assert_any_call('nginx','-t',timeout=15)

    def test_base_installer_rejects_unknown_port_80_site(self):
        from test_safety import Guard
        inst=ap.Installer(ap.arguments([]),{},Guard())
        inst.command=Mock()
        native=Mock(); native.exists.return_value=True; native.read_text.return_value='server {listen 80; root /private;}'
        with patch.object(ap,'Path',return_value=native),self.assertRaises(ap.Refuse): inst.install_nginx()
        inst.command.assert_not_called()

    def test_historical_only_camera_never_guesses_video0(self):
        self.assertIsNone(p.camera_configuration(self.snapshot()))

    def test_camera_needs_single_stable_capture_path(self):
        snapshot=self.snapshot()
        snapshot['usb_webcam']=[{'nodes':[{'device':'/dev/video0'}]}]
        with self.assertRaises(ap.Refuse): p.camera_configuration(snapshot)
        snapshot['usb_webcam'][0]['nodes']=[{'device':'/dev/v4l/by-id/cam-video-index0'}]
        text=p.camera_configuration(snapshot)
        self.assertIn('device: /dev/v4l/by-id/cam-video-index0',text)
        snapshot['usb_webcam'].append(snapshot['usb_webcam'][0])
        with self.assertRaises(ap.Refuse): p.camera_configuration(snapshot)

    def test_legacy_scanner_save_config_refuses_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)
            for value in ('[scanner]\n','#*# [scanner model default]\n'):
                (config/'printer.cfg').write_text(value)
                with self.assertRaises(ap.Refuse): p.refuse_legacy_cartographer(config)

    def test_accessory_mode_has_no_flash_or_network_control_flags(self):
        for flag in ('--flash','--enable-flashing-capability','--enable-wifi'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): p.arguments([flag])

    def test_default_audit_has_no_guard_installs_or_history_writes(self):
        snapshot=self.snapshot()
        with patch.object(p.platform,'system',return_value='Linux'), patch.object(ph,'current',return_value=snapshot), patch.object(ph,'load',return_value={'schema':1,'devices':{}}), patch('subprocess.run',return_value=Mock(stdout='')), patch.object(ph,'record') as record, patch.object(p,'Setup') as setup, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(p.main([]),0)
        record.assert_not_called(); setup.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['status'],'AUDIT_ONLY')

    def test_bad_history_entry_refuses_instead_of_forgetting(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'history.json'
            file.write_text(json.dumps({'schema':1,'devices':{'cartographer':{'seen':'true','observations':[]}}}))
            with self.assertRaises(ValueError): ph.load(file)


if __name__=='__main__': unittest.main()
