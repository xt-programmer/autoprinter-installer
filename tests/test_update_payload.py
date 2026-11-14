import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import sys
import unittest
from unittest.mock import Mock,patch

import autoprinter as ap
import flash_policy as fp
import octopus_flasher as of
import paired_update as paired
import update_payload as payload
import update_storage as storage
from update_transaction import UpdateTransaction
from test_paired_update import PairedTransactionTests,write
from test_safety import Guard


class StorageTests(unittest.TestCase):
    def tree(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        data=Path(temp.name); (data/'gcodes').mkdir(); (data/'config').mkdir()
        for name in ['printer.cfg','printer-20261110_100000.cfg','printer-20261111_100000.cfg','printer-20261112_100000.cfg','settings500.cfg','printer-custom.cfg']:
            (data/'config'/name).write_text('# keep or dated backup\n')
        for name in ['old.gcode','current.gcode','young.gcode','unrelated.txt']:
            path=data/'gcodes'/name; path.write_text('G1 X10\n')
            if name!='young.gcode': os.utime(path,(time.time()-49*3600,time.time()-49*3600))
        return data

    def test_only_old_gcode_and_superseded_config_backups_deleted(self):
        data=self.tree(); plan=storage.cleanup_plan(data,[data/'gcodes/current.gcode'])
        self.assertEqual({Path(p['path']).name for p in plan},{'old.gcode','printer-20261110_100000.cfg','printer-20261111_100000.cfg'})
        receipts=[]; storage.apply_cleanup(plan,Guard(),receipts.append)
        for name in ['printer.cfg','printer-20261112_100000.cfg','settings500.cfg','printer-custom.cfg']: self.assertTrue((data/'config'/name).exists())
        self.assertTrue((data/'gcodes/current.gcode').exists()); self.assertTrue((data/'gcodes/young.gcode').exists())
        self.assertEqual(len(receipts),6)

    def test_active_config_include_even_old_dated_backup_is_protected(self):
        data=self.tree(); (data/'config/printer.cfg').write_text('[include printer-20261110_100000.cfg]\n')
        plan=storage.cleanup_plan(data)
        self.assertNotIn('printer-20261110_100000.cfg',{Path(p['path']).name for p in plan})

    def test_modified_cleanup_file_is_not_deleted(self):
        data=self.tree(); plan=storage.cleanup_plan(data)
        first=Path(plan[0]['path']); first.write_text('new upload contents')
        with self.assertRaises(fp.FlashError): storage.apply_cleanup(plan,Guard(),Mock())
        self.assertTrue(first.exists())

    def test_no_user_file_cleanup_when_space_is_already_sufficient(self):
        data=self.tree()
        with patch.object(storage.shutil,'disk_usage',return_value=Mock(free=4*ap.GIB)),patch.object(storage,'apply_cleanup') as remove:
            self.assertEqual(storage.ensure_space([data],data,Guard(),Mock(),Mock()),0)
        remove.assert_not_called(); self.assertTrue((data/'gcodes/old.gcode').exists())

    def test_still_full_after_cleanup_refuses_before_update(self):
        data=self.tree()
        with patch.object(storage.shutil,'disk_usage',return_value=Mock(free=100)),self.assertRaisesRegex(fp.FlashError,'Insufficient'):
            storage.ensure_space([data],data,Guard(),Mock(),Mock())
        self.assertTrue((data/'config/printer.cfg').exists())

    def test_unknown_current_job_can_disable_gcode_deletion(self):
        data=self.tree(); plan=storage.cleanup_plan(data,include_gcodes=False)
        self.assertFalse(any(entry['reason'].startswith('gcode') for entry in plan))

    def test_cleanup_stops_as_soon_as_enough_space_exists(self):
        data=self.tree(); plan=storage.cleanup_plan(data); receipts=[]; stopped=[False]
        def record(entry):
            receipts.append(entry)
            if entry['event']=='DELETED': stopped[0]=True
        storage.apply_cleanup(plan,Guard(),record,stop_when=lambda:stopped[0])
        self.assertEqual(sum(e['event']=='DELETED' for e in receipts),1)
        self.assertTrue((data/'gcodes/old.gcode').exists())

    def test_new_include_reference_between_plan_and_delete_is_preserved(self):
        data=self.tree(); plan=storage.cleanup_plan(data)
        (data/'config/printer.cfg').write_text('[include printer-20261110_100000.cfg]\n')
        storage.apply_cleanup(plan,Guard(),Mock())
        self.assertTrue((data/'config/printer-20261110_100000.cfg').exists())

    @unittest.skipIf(os.name=='nt','POSIX symbolic links')
    def test_symlink_files_and_directories_never_authorize_external_deletion(self):
        data=self.tree(); outside=data/'outside'; outside.mkdir(); path=outside/'external.gcode'; path.write_text('keep')
        os.utime(path,(0,0)); (data/'gcodes/link.gcode').symlink_to(path); (data/'gcodes/linkdir').symlink_to(outside,target_is_directory=True)
        plan=storage.cleanup_plan(data)
        self.assertFalse(any('link' in p['path'] for p in plan)); self.assertTrue(path.exists())

    def test_scratch_cleanup_refuses_outside_or_root(self):
        data=self.tree()
        for path in (data,data.parent/'different'):
            with self.assertRaises(fp.FlashError): storage.remove_scratch(path,data,Guard())


class UpdateTransactionTests(unittest.TestCase):
    rig=PairedTransactionTests.rig
    def update_rig(self):
        old,bundle,normal,model,before,calls,reset=self.rig()
        txn=UpdateTransaction(old.guard,old.runner,old.state,old.inactive,old.identify,old.reset_board,notify=Mock())
        txn.wait=old.wait
        return txn,bundle,normal,model,before,calls,reset

    def fail_downloads(self,txn,model,count):
        run=txn.runner.run.side_effect; failed=[0]
        def command(args,**kwargs):
            result=run(args,**kwargs)
            strings=list(map(str,args))
            if '--download' in strings and not strings[-1].endswith('recovery-application.bin') and failed[0]<count:
                failed[0]+=1
                memory=bytearray(model['memory']); memory[131072+300]^=1; model['memory']=bytes(memory)
                raise ap.Refuse('simulated partial USB download failure')
            return result
        txn.runner.run.side_effect=command

    def test_preparation_proves_dfu_and_full_backup_before_any_build_or_write(self):
        txn,bundle,normal,model,before,calls,reset=self.update_rig()
        txn.prepare(normal,'stm32h723xx')
        self.assertEqual(txn.result['state'],'BACKED_UP'); self.assertEqual(model['downloads'],0)
        self.assertEqual((txn.state/'flash-before.bin').read_bytes(),before)
        txn.program(bundle)
        self.assertEqual(txn.result['state'],'VERIFIED'); self.assertEqual(model['downloads'],1)

    def test_dfu_entry_failure_retries_once_and_reports_first_error(self):
        txn,bundle,normal,model,before,calls,reset=self.update_rig()
        original=of.bootloader_request.side_effect; attempts=[0]
        def request(*args):
            attempts[0]+=1
            if attempts[0]==2: return original(*args)
            return 1
        with patch.object(of,'bootloader_request',side_effect=request): txn.prepare(normal,'stm32h723xx')
        self.assertEqual(attempts[0],2); self.assertEqual(sum(call.args[0]=='error' for call in txn.notify.call_args_list),1)
        self.assertEqual(model['downloads'],0)

    def test_two_failed_dfu_entries_abort_without_flash(self):
        txn,bundle,normal,model,before,calls,reset=self.update_rig(); model['dfu_fail']=True
        with self.assertRaisesRegex(fp.FlashError,'after 2 attempts'): txn.prepare(normal,'stm32h723xx')
        self.assertEqual(model['downloads'],0); self.assertEqual(sum(call.args[0]=='error' for call in txn.notify.call_args_list),2)

    def test_failed_flash_retries_once_without_overwriting_original_backup(self):
        txn,bundle,normal,model,before,calls,reset=self.update_rig()
        txn.prepare(normal,'stm32h723xx'); self.fail_downloads(txn,model,1); txn.program(bundle)
        self.assertEqual(model['downloads'],2); self.assertEqual(txn.result['update_attempts'],2)
        self.assertEqual((txn.state/'flash-before.bin').read_bytes(),before)
        self.assertEqual(txn.result['state'],'VERIFIED')
        with self.assertRaises(fp.FlashError): txn.program(bundle)

    def test_two_partial_flash_failures_restore_old_application_without_host_promotion(self):
        txn,bundle,normal,model,before,calls,reset=self.update_rig()
        txn.prepare(normal,'stm32h723xx'); self.fail_downloads(txn,model,2)
        with self.assertRaisesRegex(fp.FlashError,'Two flash attempts'): txn.program(bundle)
        self.assertEqual(txn.result['state'],'PROGRAM_FAILED'); self.assertEqual(model['downloads'],2)
        paired.restore_board(txn,lambda path,dictionary:reset(path),allow_partial=True)
        self.assertEqual(model['downloads'],3) # two update attempts + one restorative write
        self.assertEqual(model['memory'],before); self.assertEqual(txn.result['state'],'PREVIOUS_FIRMWARE_RESTORED')

    def test_build_failure_can_leave_dfu_with_original_firmware_without_rewriting_it(self):
        txn,bundle,normal,model,before,calls,reset=self.update_rig(); txn.prepare(normal,'stm32h723xx')
        paired.restore_board(txn,lambda path,dictionary:reset(path),allow_partial=True)
        self.assertEqual(model['downloads'],0); self.assertEqual(model['memory'],before)

    def test_bootloader_damage_is_never_retried_or_hidden_by_application_recovery(self):
        txn,bundle,normal,model,before,calls,reset=self.update_rig(); txn.prepare(normal,'stm32h723xx')
        raw=bytearray(model['memory']); raw[100]^=1; model['memory']=bytes(raw)
        with self.assertRaisesRegex(fp.FlashError,'Bootloader'): txn.program(bundle)
        self.assertEqual(model['downloads'],0)
        with self.assertRaisesRegex(fp.FlashError,'Bootloader'): paired.restore_board(txn,Mock(),allow_partial=True)
        self.assertEqual(model['downloads'],0)

    def test_recovery_ignores_changing_usb_devnum_but_not_identity_or_flash_map(self):
        txn,bundle,normal,model,before,calls,reset=self.update_rig(); txn.prepare(normal,'stm32h723xx'); txn.program(bundle)
        original=txn.runner.run.side_effect
        def command(args,**kwargs):
            code,out=original(args,**kwargs)
            if '--list' in args: out=out.replace('Found DFU:','Found DFU: devnum=999,')
            return code,out
        txn.runner.run.side_effect=command
        paired.restore_board(txn,lambda path,dictionary:reset(path),allow_partial=True)
        self.assertEqual(model['memory'],before)


class LatestAndDisplayTests(unittest.TestCase):
    def test_build_wrong_revision_refuses_before_make(self):
        runner=Mock(); runner.run.return_value=(0,'b'*40)
        with self.assertRaises(fp.FlashError): payload.build(runner,Guard(),Path('/source'),'stm32h723xx',Path('/output'),'a'*40)
        self.assertFalse(any(call.args[0][0]=='make' for call in runner.run.call_args_list))
    def test_unverified_or_wrong_revision_never_opens_host_promotion_gate(self):
        good={'state':'VERIFIED','source_commit':'a'*40,'reset_boot_verified':True}
        payload.verified_gate(good,'a'*40)
        for result in [dict(good,state='WRITE_STARTED'),dict(good,source_commit='b'*40),dict(good,reset_boot_verified=False)]:
            with self.assertRaises(fp.FlashError): payload.verified_gate(result,'a'*40)
    def test_latest_resolved_once_to_exact_official_master_commit(self):
        runner=Mock(); runner.run.return_value=(0,'a'*40+'\trefs/heads/master')
        self.assertEqual(payload.resolve_revision(runner),'a'*40)
        self.assertIn(payload.UPSTREAM,runner.run.call_args.args[0])
        runner.reset_mock(); self.assertEqual(payload.resolve_revision(runner,'b'*40),'b'*40); runner.run.assert_not_called()

    def test_ambiguous_or_missing_board_is_an_error_not_a_guess(self):
        d={'port':'5-1','serial':'123','vid':'1d50','pid':'614e','product':'stm32h723xx','manufacturer':'Klipper'}
        with patch.object(ap,'usb_devices',return_value=[d]): self.assertEqual(payload.target()[1],'stm32h723xx')
        with patch.object(ap,'usb_devices',return_value=[d,dict(d,port='6-1',serial='456')]),self.assertRaises(fp.FlashError): payload.target()
        with patch.object(ap,'usb_devices',return_value=[]),self.assertRaises(fp.FlashError): payload.target()

    def test_green_and_red_status_are_visible_through_the_console_pipe(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out): payload.screen('success','matching version'); payload.screen('error','flash failed')
        self.assertIn('\033[1;42;30m',out.getvalue()); self.assertIn('\033[1;41;97m',out.getvalue())


class PayloadWiringTests(unittest.TestCase):
    def exercise(self,unverified=False,protocol=False):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root=Path(temp.name); home=root/'home/pi'; home.mkdir(parents=True)
        data=home/'printer_data'; data.mkdir(); states=root/'states'; states.mkdir()
        host=home/'klipper'; (host/'klippy').mkdir(parents=True); (host/'klippy/klippy.py').write_text('old')
        env=home/'klippy-env'; (env/'bin').mkdir(parents=True); (env/'pyvenv.cfg').write_text('old environment')
        guard=Guard(); guard.cancelled.wait=Mock(); events=[]
        service=Mock(was_active=False,preexisting_protocol_error=False,stopped=False)
        service.stop.side_effect=lambda:events.append('stop')
        def resume_error(): events.append('resume'); raise fp.ProtocolError('MCU Protocol error')
        service.resume.side_effect=resume_error if protocol else lambda:events.append('resume') or 'PRESERVED_INACTIVE'
        dictionary={'version':'new','config':{'MCU':'stm32h723xx'}}
        normal={'port':'5-1','serial':'NORMAL','vid':'1d50','pid':'614e','manufacturer':'Klipper','product':'stm32h723xx','path':str(root/'usb'),'busnum':'5','devnum':'4'}
        (root/'usb').mkdir()
        for name,field in [('idVendor','vid'),('idProduct','pid'),('serial','serial'),('busnum','busnum'),('devnum','devnum')]:
            (root/'usb'/name).write_text(normal[field])
        txn=Mock(); txn.result={'state':'BACKED_UP','dfu_usb':normal}
        txn.prepare.side_effect=lambda *args:events.append('DFU_BACKUP')
        def program(directory):
            events.append('program')
            txn.result.update(state='WRITE_STARTED' if unverified else 'VERIFIED',source_commit='b'*40,normal_usb=normal,reset_boot_verified=not unverified)
            return txn.result
        txn.program.side_effect=program
        def source(runner,work,revision):
            events.append('stage_source'); path=work/'klipper'; (path/'klippy').mkdir(parents=True); (path/'klippy/klippy.py').write_text('new'); return path
        def environment(runner,guard,env,work,source):
            path=work/'klippy-env'; (path/'bin').mkdir(parents=True); (path/'pyvenv.cfg').write_text('new environment'); return path
        def build(runner,guard,source,chip,output,revision):
            events.append('build'); output.mkdir()
            return {'source_commit':revision},fp.profile(chip),dictionary
        def command(args,**kwargs):
            args=list(map(str,args))
            if args[0]=='git':
                if 'rev-parse' in args: return 0,('a' if (host/'klippy/klippy.py').read_text()=='old' else 'b')*40
                if 'describe' in args: return 0,'old-version'
            if args[0]=='du': return 0,'1000\told-env'
            if args[0]=='systemctl' and 'is-active' in args: return 0,'inactive'
            if any(s.endswith('mcu_identify.py') for s in args): return 0,'MCU_IDENTIFY_JSON '+json.dumps(dictionary)
            if 'freeze' in args: return 0,'package==1'
            if args[:2]==['make','clean']: events.append('clean')
            return 0,''
        runner=Mock(); runner.run.side_effect=command
        def restored(txn,service,host,retained,report,reset_old,**kwargs):
            events.append('restore'); report['pair_restored']=True
        native_promote=payload.promote
        def promote(*args): events.append('promote'); return native_promote(*args)
        patches=[patch.object(payload,'HOME',home),patch.object(payload,'DATA',data),patch.object(payload,'STATE_ROOT',states),patch.object(payload,'LOCK_PATH',root/'lock'),
                 patch.object(payload,'target',return_value=(normal,'stm32h723xx')),patch.object(payload,'SSHGuard',return_value=guard),patch.object(payload,'screen',side_effect=lambda kind,message:events.append(kind)),
                 patch.object(ap.os,'geteuid',return_value=0,create=True),patch.object(ap.os,'O_NOFOLLOW',0,create=True),patch.object(payload.signal,'signal'),patch.dict(sys.modules,{'fcntl':Mock(LOCK_EX=1,LOCK_NB=4)}),
                 patch.object(ap,'atomic_write',side_effect=write),patch.object(ap,'inspect_host',return_value={'os':{'ID':'debian'}}),patch.object(ap,'supported_host'),patch.object(ap,'check_free'),patch.object(ap,'Runner',return_value=runner),
                 patch.object(paired,'Service',return_value=service),patch.object(payload.storage,'ensure_space',return_value=0),patch.object(payload,'resolve_revision',return_value='b'*40),patch.object(payload,'build_tools',return_value=[]),
                 patch.object(payload,'stage_source',side_effect=source),patch.object(payload,'stage_environment',side_effect=environment),patch.object(payload,'build',side_effect=build),patch.object(payload,'UpdateTransaction',return_value=txn),
                 patch.object(of,'serial_path',return_value='/dev/test'),patch.object(of,'match_port',return_value=normal),patch.object(paired,'restore_pair',side_effect=restored),patch.object(payload,'promote',side_effect=promote),patch.object(payload,'download_workspace',return_value=None)]
        with contextlib.ExitStack() as stack:
            for item in patches: stack.enter_context(item)
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                code=payload.main(['--klipper-update-only','--apply','--enable-flashing-capability','--maintenance-confirmed','--actuator-power-disconnected','--board-confirmed-octopus','--allow-bootloader-request','--supervised-ssh','--ssh-session-pid','1'])
        reports=list(states.glob('*/report.json'))
        self.assertEqual(len(reports),1,output.getvalue())
        return code,events,json.loads(reports[0].read_text()),txn

    def test_dfu_then_build_then_program_then_promotion_cleanup_and_green(self):
        code,events,report,txn=self.exercise()
        self.assertEqual(code,0,report); self.assertLess(events.index('DFU_BACKUP'),events.index('build'))
        self.assertLess(events.index('program'),events.index('promote')); self.assertLess(events.index('clean'),events.index('success'))
        self.assertEqual(events.count('promote'),2); self.assertTrue(report['update_complete'])

    def test_unverified_receipt_never_promotes_source_or_environment_or_shows_green(self):
        code,events,report,txn=self.exercise(unverified=True)
        self.assertEqual(code,2); self.assertNotIn('promote',events); self.assertNotIn('success',events); self.assertIn('restore',events)

    def test_persistent_protocol_error_gets_one_sync_pass_then_saved_pair_recovery(self):
        code,events,report,txn=self.exercise(protocol=True)
        self.assertEqual(code,2); self.assertTrue(report.get('protocol_sync_attempted'),report); self.assertTrue(report['pair_restored'])
        self.assertEqual(txn.program.call_count,1) # already-matching selected MCU is not blindly reflashed
        self.assertEqual(events.count('resume'),2)
        self.assertNotIn('success',events); self.assertEqual(events.count('restore'),1)


if __name__=='__main__': unittest.main()
