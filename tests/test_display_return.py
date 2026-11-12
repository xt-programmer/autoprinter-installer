"""Display return never starts services and is guarded independently in root."""
from pathlib import Path
import json,sys,tempfile,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import display_return as dr


class DisplayReturnTests(unittest.TestCase):
    def setup_tree(self,root,vt=7,tty_nr=0):
        usb=root/'usb'; device=usb/'1-1'; device.mkdir(parents=True)
        session={'port':'1-1','vid':'1234','pid':'abcd','serial':'','busnum':'1','devnum':'3'}
        for field,key in [('vid','idVendor'),('pid','idProduct'),('serial','serial'),('busnum','busnum'),('devnum','devnum')]:
            (device/key).write_text(session[field])
        proc=root/'proc'
        self.process(proc,10,'xinit',['/usr/bin/xinit'],children='11')
        self.process(proc,11,'Xorg',['/usr/lib/xorg/Xorg',':0']+(['vt'+str(vt)] if vt else []),tty_nr=tty_nr)
        props='ActiveState=active\nSubState=running\nMainPID=10\nTTYPath=/dev/tty7\n'
        def runner(argv,**kwargs):
            return Mock(returncode=0,stdout=props if argv[0]=='systemctl' else '',stderr='')
        return session,usb,proc,Mock(side_effect=runner)

    def process(self,proc,pid,comm,argv,children='',tty_nr=0):
        directory=proc/str(pid); (directory/'task'/str(pid)).mkdir(parents=True)
        (directory/'task'/str(pid)/'children').write_text(children)
        (directory/'comm').write_text(comm)
        (directory/'cmdline').write_bytes(b'\0'.join(x.encode() for x in argv)+b'\0')
        fields=['S','1','1','1',str(tty_nr)]+['0']*14+['12345']+['0']*8
        (directory/'stat').write_text(str(pid)+' ('+comm+') '+' '.join(fields))

    def test_completed_job_returns_to_actual_display_server_vt(self):
        with tempfile.TemporaryDirectory() as tmp:
            session,usb,proc,run=self.setup_tree(Path(tmp))
            with patch.object(Path,'is_char_device',return_value=True):
                rc,result=dr.restore(session,usb,proc,Path(tmp),run)
            self.assertEqual(rc,0); self.assertEqual(result['vt'],7)
            self.assertEqual(run.call_args.args[0],['/usr/bin/chvt','7'])
            self.assertFalse(any('start' in c.args[0] or 'restart' in c.args[0] for c in run.call_args_list))

    def test_old_auto_selected_xorg_vt_is_derived_from_process_not_unit_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            session,usb,proc,run=self.setup_tree(Path(tmp),vt=None,tty_nr=1026)
            with patch.object(Path,'is_char_device',return_value=True):
                rc,result=dr.restore(session,usb,proc,Path(tmp),run)
            self.assertEqual(rc,0); self.assertEqual(result['vt'],2)

    def test_absent_or_reenumerated_trigger_never_queries_or_switches(self):
        with tempfile.TemporaryDirectory() as tmp:
            session,usb,proc,run=self.setup_tree(Path(tmp))
            (usb/'1-1/devnum').write_text('4')
            self.assertEqual(dr.restore(session,usb,proc,Path(tmp),run)[0],130)
            run.assert_not_called()
            session['port']='../../etc'
            self.assertEqual(dr.restore(session,usb,proc,Path(tmp),run)[0],130)

    def test_stopped_gui_is_not_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            session,usb,proc,_=self.setup_tree(Path(tmp))
            run=Mock(return_value=Mock(returncode=0,stdout='ActiveState=inactive\nSubState=dead\nMainPID=0\n',stderr=''))
            rc,result=dr.restore(session,usb,proc,Path(tmp),run)
            self.assertEqual(rc,3)
            self.assertEqual(result['status'],'SKIPPED_KLIPPERSCREEN_NOT_RUNNING')
            self.assertEqual(run.call_count,1)

    def test_service_change_between_inspection_and_switch_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            session,usb,proc,run=self.setup_tree(Path(tmp))
            first=run(['systemctl']).stdout
            run.reset_mock(); run.side_effect=[Mock(returncode=0,stdout=first),Mock(returncode=0,stdout=first.replace('MainPID=10','MainPID=12'))]
            with patch.object(Path,'is_char_device',return_value=True), self.assertRaises(RuntimeError):
                dr.restore(session,usb,proc,Path(tmp),run)
            self.assertEqual(run.call_count,2)

    def test_usb_change_during_service_inspection_refuses_before_chvt(self):
        with tempfile.TemporaryDirectory() as tmp:
            session,usb,proc,run=self.setup_tree(Path(tmp))
            state=run(['systemctl']); run.reset_mock()
            def inspect(argv,**kwargs):
                if run.call_count==2: (usb/'1-1/devnum').write_text('4')
                return state
            run.side_effect=inspect
            with patch.object(Path,'is_char_device',return_value=True):
                self.assertEqual(dr.restore(session,usb,proc,Path(tmp),run)[0],130)
            self.assertEqual(run.call_count,2)

    def test_ambiguous_or_unknown_display_terminal_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            session,usb,proc,run=self.setup_tree(Path(tmp))
            self.process(proc,12,'Xorg',['/usr/lib/xorg/Xorg','vt2'])
            (proc/'10/task/10/children').write_text('11 12')
            with self.assertRaises(RuntimeError): dr.restore(session,usb,proc,Path(tmp),run)
            (proc/'10/task/10/children').write_text('')
            with self.assertRaises(RuntimeError): dr.restore(session,usb,proc,Path(tmp),run)


if __name__=='__main__': unittest.main()
