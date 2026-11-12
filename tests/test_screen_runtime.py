"""Check native launch adoption and failures that import/pip checks cannot catch."""
import pathlib,sys,tempfile,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import screen_runtime as sr
import peripherals as p
import autoprinter as ap
from test_safety import Guard

UNIT='''[Service]
User=pi
WorkingDirectory=/home/pi/KlipperScreen
Environment="KS_XCLIENT=/home/pi/.KlipperScreen-env/bin/python /home/pi/KlipperScreen/screen.py" BACKEND=X
ExecStart="/home/pi/KlipperScreen/scripts/KlipperScreen-start.sh"
ExecStartPost=+chvt 7
TTYPath=/dev/tty7
'''
RUNNING='ActiveState=active\nSubState=running\nMainPID=123\nNRestarts=0\n'
GOOD_LOG='Screen resolution: 1024x600\nCurrent panel hierarchy: splash_screen\nKlipper: disconnected\n'


class ScreenRuntimeTests(unittest.TestCase):
    def test_native_launch_drops_gui_caps_only_and_pins_same_vt(self):
        result=sr.native_x11_override(UNIT,UNIT,['[Service]\nExecCondition=/bin/true\n'])
        self.assertIn('xinit /usr/bin/setpriv --inh-caps=-all --ambient-caps=-all',result)
        self.assertIn('-- /usr/bin/X :0 vt7 -keeptty -nolisten tcp',result)
        self.assertIn('StartLimitBurst=3',result)
        self.assertNotIn('GLYCIN_DISABLE_SANDBOX',result)
        self.assertNotIn('NoNewPrivileges=',result)  # Preserve Xorg's normal wrapper.

    def test_operator_backend_launcher_and_override_are_preserved_by_refusal(self):
        for text in (UNIT.replace('BACKEND=X','BACKEND=W'),UNIT.replace('User=pi','User=root')):
            with self.assertRaises(ValueError): sr.native_x11_override(text,UNIT)
        with self.assertRaises(ValueError): sr.native_x11_override(UNIT,UNIT,custom_launcher=True)
        with self.assertRaises(ValueError): sr.native_x11_override(UNIT,UNIT,['[Service]\nExecStart=/operator/custom\n'])

    def test_managed_dropin_is_idempotent_but_local_edits_refuse(self):
        result=sr.x11_override()
        self.assertEqual(sr.native_x11_override(UNIT,UNIT,[result]),result)
        with self.assertRaises(ValueError): sr.native_x11_override(UNIT,UNIT,[result.replace('vt7','vt2')])

    def test_active_alone_does_not_hide_restart_loop_or_changed_process(self):
        self.assertEqual(sr.check_service(RUNNING),123)
        for data in (RUNNING.replace('NRestarts=0','NRestarts=1'),RUNNING.replace('MainPID=123','MainPID=0'),RUNNING.replace('active\n','failed\n'), 'ActiveState=active\n'):
            with self.assertRaises(ValueError): sr.check_service(data)
        with self.assertRaises(ValueError): sr.check_service(RUNNING,124)

    def test_fresh_render_failure_refuses_and_klipper_disconnect_is_not_gui_failure(self):
        self.assertTrue(sr.check_startup_log(GOOD_LOG))
        for text in (GOOD_LOG+'Failed to load image',GOOD_LOG+'Uncaught exception', 'Screen resolution: 1024x600\n'):
            with self.assertRaises(ValueError): sr.check_startup_log(text)

    def setup_screen(self):
        setup=p.Setup(p.arguments([]),Guard(),{})
        setup.guard.cancelled=Mock()
        setup.guard.cancelled.is_set.return_value=False
        setup.guard.cancelled.wait=Mock()
        setup.run=Mock(return_value=(0,RUNNING))
        return setup

    def test_headless_is_explicitly_pending_not_a_physical_success(self):
        setup=self.setup_screen()
        with patch.object(p.Path,'glob',return_value=[]): setup.verify_screen(None)
        self.assertEqual(setup.report['screen_runtime']['state'],'HEADLESS_PHYSICAL_VALIDATION_PENDING')
        setup.run.assert_not_called()

    def test_runtime_success_requires_stable_service_and_fresh_panel_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=pathlib.Path(tmp)
            log=home/'printer_data/logs/KlipperScreen.log'; log.parent.mkdir(parents=True)
            old='Uncaught exception from old run\n'; log.write_text(old,encoding='utf-8')
            checkpoint=(log.stat().st_ino,log.stat().st_size)
            with log.open('a',encoding='utf-8',newline='\n') as f: f.write(GOOD_LOG)
            setup=self.setup_screen()
            with patch.object(p,'HOME',home),patch.object(p.Path,'glob',return_value=[Mock()]),patch.object(p.ap,'read',return_value='connected'):
                setup.verify_screen(checkpoint)
            self.assertEqual(setup.report['screen_runtime']['state'],'GUI_STARTED_STABLE')
            self.assertEqual(setup.run.call_count,11)
            self.assertEqual(setup.guard.cancelled.wait.call_count,10)

    def test_restart_during_runtime_check_stops_only_screen_and_refuses(self):
        setup=self.setup_screen()
        setup.run.side_effect=[(0,RUNNING),(0,RUNNING.replace('NRestarts=0','NRestarts=1')),(0,'')]
        with patch.object(p.Path,'glob',return_value=[Mock()]),patch.object(p.ap,'read',return_value='connected'),self.assertRaises(ap.Refuse):
            setup.verify_screen(None)
        setup.run.assert_called_with('systemctl','stop','KlipperScreen')

    def test_usb_cancellation_never_triggers_extra_stop(self):
        setup=self.setup_screen(); setup.guard.check=Mock(side_effect=ap.Removed('removed'))
        with patch.object(p.Path,'glob',return_value=[Mock()]),patch.object(p.ap,'read',return_value='connected'),self.assertRaises(ap.Removed):
            setup.verify_screen(None)
        setup.run.assert_not_called()

    def test_full_readiness_observes_stability_without_resetting_past_restart_history(self):
        guard=Guard(); guard.cancelled=Mock()
        guard.cancelled.is_set.return_value=False
        inst=ap.Installer(ap.arguments([]),{},guard)
        inst.command=Mock(return_value=(0,RUNNING.replace('NRestarts=0','NRestarts=2')))
        inst.verify_screen_service()
        self.assertEqual(inst.report['screen_runtime']['prior_restarts'],2)
        self.assertEqual(inst.report['screen_runtime']['observed_restarts'],0)
        inst.command.side_effect=[(0,RUNNING),(0,RUNNING.replace('MainPID=123','MainPID=124'))]
        with self.assertRaises(ap.Refuse): inst.verify_screen_service()


if __name__=='__main__': unittest.main()
