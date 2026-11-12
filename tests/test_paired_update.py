import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import autoprinter as ap
import board_flash
import flash_policy as fp
import make_hid
import mcu_identify
import octopus_flasher as of
import paired_update as paired
from test_safety import Guard


def write(path,data,guard,mode=0o600):
    guard.check(); Path(path).write_bytes(data.encode() if isinstance(data,str) else data)


class PairedTransactionTests(unittest.TestCase):
    def rig(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root=Path(temp.name); state=root/'state'; state.mkdir(); bundle=root/'bundle'; bundle.mkdir()
        p=fp.profile('stm32h723xx'); offset=p['address']-0x08000000
        old=bytearray(b'\x55'*1024*1024)
        struct.pack_into('<II',old,0,0x20020000,0x08000101)
        struct.pack_into('<II',old,offset,0x20020000,p['address']+257)
        image=struct.pack('<II',0x20020000,p['address']+257)+b'\xaa'*2048
        (bundle/'klipper.bin').write_bytes(image)
        dictionary={'version':'new','config':{'MCU':p['chip']}}
        original={'version':'old','config':{'MCU':p['chip']}}
        normal={'port':'5-1','serial':'NORMAL','devnum':'4','vid':'1d50','pid':'614e','manufacturer':'Klipper','product':p['chip']}
        dfu=dict(normal,vid='0483',pid='df11',serial='ROM123')
        line='Found DFU: path="5-1", alt=0, name="@Internal Flash /0x08000000/08*128Kg", serial="ROM123"'
        model={'memory':bytes(old),'device':normal,'downloads':0,'drift':False,'dfu_fail':False}
        calls=[]
        def run(args,**kwargs):
            args=list(map(str,args)); calls.append(args)
            if '--list' in args: return 0,line
            if '--upload' in args:
                if model['drift'] and model['downloads']==1:
                    data=bytearray(model['memory']); data[-1]^=1; model['memory']=bytes(data)
                Path(args[args.index('--upload')+1]).write_bytes(model['memory'])
            if '--download' in args:
                data=Path(args[args.index('--download')+1]).read_bytes()
                end=offset+((len(data)+131071)//131072)*131072
                memory=bytearray(model['memory']); memory[offset:end]=b'\xff'*(end-offset)
                memory[offset:offset+len(data)]=data; model['memory']=bytes(memory); model['downloads']+=1
            if any(':leave' in arg for arg in args):
                model['device']=dict(normal,devnum=str(int(model['device']['devnum'])+1))
            return 0,''
        runner=Mock(); runner.run.side_effect=run
        guard=Guard(); target=Mock(); target.start.return_value=target; target.cancelled=threading.Event()
        txn=of.Transaction(guard,runner,state,Mock(),lambda path:original if model['memory']==bytes(old) else dictionary)
        def reset(path): model['device']=dict(model['device'],devnum=str(int(model['device']['devnum'])+1))
        txn.reset_board=reset
        def request(runner,path):
            if not model['dfu_fail']: model['device']=dict(dfu,devnum=str(int(model['device']['devnum'])+1))
            return 0
        def wait(port,predicate,seconds):
            guard.check()
            if not predicate(model['device']): raise fp.FlashError('simulated missing target mode')
            return dict(model['device'])
        txn.wait=wait
        patches=[patch.object(ap,'atomic_write',side_effect=write),patch.object(fp,'artifact',return_value=({'source_commit':'a'*40},p,dictionary)),
                 patch.object(of,'match_port',side_effect=lambda port:dict(model['device'])),
                 patch.object(of,'serial_path',return_value='/dev/selected'),
                 patch.object(of,'bootloader_request',side_effect=request),
                 patch.object(ap,'USBGuard',return_value=target),patch.object(ap,'Runner',return_value=runner)]
        for item in patches: item.start(); self.addCleanup(item.stop)
        return txn,bundle,normal,model,bytes(old),calls,reset

    def test_dfu_failure_has_no_board_write_or_verified_host_gate(self):
        txn,bundle,normal,model,old,calls,reset=self.rig(); model['dfu_fail']=True
        with self.assertRaises(fp.FlashError): txn.execute(bundle,normal,allow_request=True,require_recovery=True)
        self.assertEqual(model['memory'],old); self.assertEqual(model['downloads'],0)
        self.assertNotEqual(fp.host_action('usable',txn.result['state']),'UPDATE_TO_VERIFIED_FIRMWARE_SOURCE')

    def test_protocol_failure_restores_exact_old_flash_and_bootloader_with_one_recovery_write(self):
        txn,bundle,normal,model,old,calls,reset=self.rig()
        txn.execute(bundle,normal,allow_request=True,require_recovery=True)
        self.assertEqual(model['downloads'],1); self.assertNotEqual(model['memory'],old)
        paired.restore_board(txn,lambda path,dictionary:reset(path))
        self.assertEqual(model['memory'],old); self.assertEqual(model['downloads'],2)
        self.assertEqual(txn.result['state'],'PREVIOUS_FIRMWARE_RESTORED')
        self.assertEqual(txn.result['recovery']['write_attempts'],1)
        for call in calls:
            if '--download' in call: self.assertEqual(call[call.index('--dfuse-address')+1],'0x8020000')
        with self.assertRaises(fp.FlashError): paired.restore_board(txn,Mock())
        self.assertEqual(model['downloads'],2)

    def test_changed_flash_refuses_recovery_download(self):
        txn,bundle,normal,model,old,calls,reset=self.rig()
        txn.execute(bundle,normal,allow_request=True,require_recovery=True); model['drift']=True
        with self.assertRaisesRegex(fp.FlashError,'changed since'): paired.restore_board(txn,Mock())
        self.assertEqual(model['downloads'],1)

    def test_corrupt_backup_refuses_even_bootloader_request(self):
        txn,bundle,normal,model,old,calls,reset=self.rig()
        txn.execute(bundle,normal,allow_request=True,require_recovery=True)
        (txn.state/'flash-before.bin').write_bytes(b'corrupt')
        with patch.object(of,'bootloader_request') as request,self.assertRaises(fp.FlashError): paired.restore_board(txn,Mock())
        request.assert_not_called(); self.assertEqual(model['downloads'],1)

    def test_removed_trigger_never_authorizes_recovery(self):
        txn,bundle,normal,model,old,calls,reset=self.rig()
        txn.execute(bundle,normal,allow_request=True,require_recovery=True)
        with patch.object(txn.guard,'check',side_effect=ap.Removed('unplugged')),self.assertRaises(ap.Removed): paired.restore_board(txn,Mock())
        self.assertEqual(model['downloads'],1)

    def test_initially_dfu_or_invalid_old_application_refuses_paired_update(self):
        txn,bundle,normal,model,old,calls,reset=self.rig()
        with self.assertRaisesRegex(fp.FlashError,'normal Klipper identity'):
            txn.execute(bundle,dict(normal,vid='0483',pid='df11'),allow_request=True,require_recovery=True)
        raw=bytearray(old); raw[131072:131080]=b'\xff'*8; model['memory']=bytes(raw)
        with self.assertRaisesRegex(fp.FlashError,'prior application'):
            txn.execute(bundle,normal,allow_request=True,require_recovery=True)
        self.assertEqual(model['downloads'],0)

    def test_changed_board_serial_refuses_recovery(self):
        txn,bundle,normal,model,old,calls,reset=self.rig()
        txn.execute(bundle,normal,allow_request=True,require_recovery=True)
        model['device']['serial']='OTHER'
        with self.assertRaisesRegex(fp.FlashError,'same normal-mode'): paired.restore_board(txn,Mock())
        self.assertEqual(model['downloads'],1)

    def test_protocol_mismatch_is_typed_and_still_keeps_host_gate_closed(self):
        txn,bundle,normal,model,old,calls,reset=self.rig()
        original=txn.identify
        txn.identify=lambda path:original(path) if not model['downloads'] else {'version':'unexpected'}
        with self.assertRaises(fp.ProtocolError): txn.execute(bundle,normal,allow_request=True,require_recovery=True)
        self.assertEqual(txn.result['state'],'READBACK_VERIFIED')
        self.assertEqual(fp.host_action('usable',txn.result['state']),'KEEP_EXISTING_KLIPPER')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        item=patch.object(ap,'atomic_write',side_effect=write); item.start(); self.addCleanup(item.stop)

    def service(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root=Path(temp.name); log=root/'klippy.log'; log.write_text('MCU Protocol error: historical\n')
        service=paired.Service(Mock(),Guard(),root,log); service.was_active=True
        props={'ActiveState':'active','SubState':'running','MainPID':'123','User':'pi','ExecStart':'{ argv[]=/home/pi/klippy-env/bin/python /home/pi/klipper/klippy/klippy.py /home/pi/printer_data/config/printer.cfg -l /home/pi/printer_data/logs/klippy.log ; }'}
        service.properties=Mock(return_value=props)
        clock=[0]
        def tick(): clock[0]+=1; return clock[0]
        self.clock=tick
        service.guard.cancelled.wait=Mock()
        return service,log

    def resume(self,service,info):
        with patch.object(paired.time,'monotonic',side_effect=self.clock),patch.object(ap,'url_json',return_value=info),patch.object(ap,'printer_snapshot',return_value=({},{})),patch.object(ap,'safe_idle'):
            return service.resume()

    def test_old_log_protocol_errors_do_not_trigger_downgrade(self):
        service,log=self.service()
        self.assertEqual(self.resume(service,{'state':'ready','process_id':123}),'READY_IDLE')

    def test_fresh_log_protocol_error_triggers_typed_recovery(self):
        service,log=self.service()
        service.runner.run.side_effect=lambda *a,**k:log.write_text(log.read_text()+'MCU Protocol error: current\n')
        with self.assertRaises(fp.ProtocolError): self.resume(service,{'state':'error','process_id':123})

    def test_current_process_api_protocol_error_triggers_recovery(self):
        service,log=self.service()
        with self.assertRaises(fp.ProtocolError): self.resume(service,{'state':'error','process_id':123,'state_message':'MCU Protocol error'})

    def test_cached_old_process_does_not_prove_success_or_trigger_downgrade(self):
        service,log=self.service()
        with self.assertRaisesRegex(fp.FlashError,'verification window'):
            self.resume(service,{'state':'error','process_id':99,'state_message':'MCU Protocol error'})

    def test_inactive_service_stays_inactive(self):
        service,log=self.service(); service.was_active=False
        self.assertEqual(service.resume(),'PRESERVED_INACTIVE'); service.runner.run.assert_not_called()

    def test_preflight_refuses_active_print_before_stop(self):
        service,log=self.service()
        with patch.object(ap,'printer_snapshot',side_effect=ap.Refuse('print active')),self.assertRaises(ap.Refuse): service.preflight()
        service.runner.run.assert_not_called()

    def test_board_restore_failure_never_swaps_back_only_the_host(self):
        service,log=self.service(); root=log.parent
        service.stop_for_recovery=Mock()
        host=root/'klipper'; retained=root/'retained'
        for path,content in [(host,'new'),(retained,'old')]:
            (path/'klippy').mkdir(parents=True); (path/'klippy/klippy.py').write_text(content)
        txn=Mock(); txn.guard=Guard(); txn.state=root
        txn.runner.run.side_effect=[(0,'a'*40),(0,'')]
        report={'host_commit_before':'a'*40,'host_entry_sha256_before':fp.sha(b'old')}
        with patch.object(paired,'restore_board',side_effect=fp.FlashError('DFU failed')),self.assertRaisesRegex(fp.FlashError,'DFU failed'):
            paired.restore_pair(txn,service,host,retained,report,Mock())
        self.assertEqual((host/'klippy/klippy.py').read_text(),'new')
        self.assertNotIn('pair_restored',report)

    def test_verified_board_restore_then_old_host_restores_and_retains_failed_candidate(self):
        service,log=self.service(); root=log.parent; service.resume=Mock(return_value='READY_IDLE')
        service.stop_for_recovery=Mock()
        host=root/'klipper'; retained=root/'retained'
        for path,content in [(host,'new'),(retained,'old')]:
            (path/'klippy').mkdir(parents=True); (path/'klippy/klippy.py').write_text(content)
        txn=Mock(); txn.guard=Guard(); txn.state=root/'state'; txn.state.mkdir()
        txn.runner.run.side_effect=[(0,'a'*40),(0,'')]
        report={'host_commit_before':'a'*40,'host_entry_sha256_before':fp.sha(b'old'),'host_updated':True}
        def restored(*args): self.assertEqual((host/'klippy/klippy.py').read_text(),'new')
        with patch.object(paired,'restore_board',side_effect=restored): paired.restore_pair(txn,service,host,retained,report,Mock())
        self.assertEqual((host/'klippy/klippy.py').read_text(),'old')
        self.assertEqual((Path(report['failed_host_retained'])/'klippy/klippy.py').read_text(),'new')
        self.assertTrue(report['pair_restored']); self.assertFalse(report['host_updated'])


class EntryTests(unittest.TestCase):
    def test_unknown_gcode_is_not_an_mcu_protocol_error(self):
        self.assertFalse(paired.protocol_error('Unknown command: "TIMELAPSE_TAKE_FRAME"'))
        self.assertTrue(paired.protocol_error('MCU Protocol error: Unknown command'))

    def test_external_plugin_bytes_are_preserved_and_changes_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); host=root/'old'; candidate=root/'new'; candidate.mkdir()
            path=host/'klippy/extras/cartographer.py'; path.parent.mkdir(parents=True)
            path.write_text('# installed plugin\n')
            records=paired.stage_plugins(host,candidate,set(),Guard())
            self.assertEqual((candidate/'klippy/extras/cartographer.py').read_bytes(),path.read_bytes())
            path.write_text('# changed\n')
            with self.assertRaises(fp.FlashError): paired.verify_plugins(host,records,Guard())

    def test_plugin_collision_refuses_before_overwriting_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); host=root/'old'; candidate=root/'new'
            for path in (host/'klippy/extras/x.py',candidate/'klippy/extras/x.py'):
                path.parent.mkdir(parents=True); path.write_text('original\n')
            with self.assertRaises(fp.FlashError): paired.stage_plugins(host,candidate,set(),Guard())
            self.assertEqual((candidate/'klippy/extras/x.py').read_text(),'original\n')

    def test_kiauh_environment_arguments_are_read_literally(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'klipper.env'
            args='/home/pi/klipper/klippy/klippy.py /home/pi/printer_data/config/printer.cfg -l /home/pi/printer_data/logs/klippy.log'
            path.write_text('KLIPPER_ARGS="'+args+'"\n')
            props={'User':'pi','ExecStart':'{ argv[]=/home/pi/klippy-env/bin/python $KLIPPER_ARGS ; }','EnvironmentFiles':str(path)+' (ignore_errors=no)'}
            self.assertEqual(paired.native_launch(props,path)[1],'/home/pi/klipper/klippy/klippy.py')
            path.write_text('KLIPPER_ARGS="$(touch danger)"\n')
            with self.assertRaises(fp.FlashError): paired.native_launch(props,path)

    def test_local_flag_routes_only_to_board_entrypoint(self):
        with patch.object(board_flash,'main',return_value=0) as entry:
            self.assertEqual(ap.main(['--klipper-update-only','--chip','stm32h723xx']),0)
        entry.assert_called_once_with(['--klipper-update-only','--chip','stm32h723xx'])

    def test_hid_update_mode_requires_capability_and_isolation_on_apply(self):
        flags,_,_=make_hid.invocation('klipper-update-only','flipper')
        self.assertIn('--klipper-update-only',flags); self.assertNotIn('--apply',flags)
        with self.assertRaises(ValueError): make_hid.invocation('klipper-update-only','flipper',['--apply','--maintenance-confirmed'])
        flags,_,_=make_hid.invocation('klipper-update-only','flipper',['--apply','--maintenance-confirmed','--enable-flashing-capability','--actuator-power-disconnected','--board-confirmed-octopus','--allow-bootloader-request'])
        self.assertIn('--apply',flags)

    def test_core_protocol_formats_are_read_from_host_source_without_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'mcu.py').write_text('def _send_get_config(self):\n self.mcu.lookup_query_command("get_config", "config count=%u")\n')
            parser=Mock(); mcu_identify.check_host_protocol(root,parser)
            self.assertIn(unittest.mock.call('config count=%u'),parser.lookup_command.call_args_list)


if __name__=='__main__': unittest.main()
