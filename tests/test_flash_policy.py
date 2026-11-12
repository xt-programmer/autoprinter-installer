import contextlib,io,json,struct,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import flash_policy as fp
import octopus_flasher as flashing
import autoprinter as ap
from test_safety import Guard


class FlashPolicyTests(unittest.TestCase):
    def test_requested_profiles_use_real_hex_kconfig_symbols(self):
        for chip,boot,clock,symbol in [('stm32f446xx',32,12,'8000'),('stm32f429xx',32,8,'8000'),('stm32h723xx',128,25,'20000')]:
            p=fp.profile(chip)
            self.assertEqual((p['boot_kib'],p['crystal_mhz']),(boot,clock))
            self.assertIn('CONFIG_STM32_FLASH_START_'+symbol+'=y',fp.config_seed(chip))
            self.assertIn('CONFIG_STM32_USB_PA11_PA12=y',fp.config_seed(chip))

    def test_shipped_artifacts_validate_all_three_actual_builds(self):
        root=Path(fp.__file__).parent/'firmware-bundles'
        for chip in fp.PROFILES:
            metadata,p,dictionary=fp.artifact(root/chip)
            self.assertEqual(metadata['source_commit'],'29d4ec2d6d840ee839333306003a400da186d41d')
            self.assertEqual(dictionary['config']['MCU'],chip)

    def normalized(self,chip):
        p=fp.profile(chip)
        return fp.config_seed(chip)+'CONFIG_MCU="'+chip+'"\nCONFIG_USBSERIAL=y\nCONFIG_FLASH_APPLICATION_ADDRESS=0x%x\nCONFIG_USB_SERIAL_NUMBER_CHIPID=y\nCONFIG_INITIAL_PINS=""\n'%p['address']

    def test_wrong_clock_offset_usb_startup_outputs_refuse(self):
        text=self.normalized('stm32h723xx')
        fp.validate_config(text,'stm32h723xx')
        for old,new in [('REF_25M','REF_8M'),('START_20000','START_131072'),('STM32_USB_PA11_PA12','STM32_SERIAL_USART1'),('INITIAL_PINS=""','INITIAL_PINS="!PA2"')]:
            with self.assertRaises(fp.FlashError): fp.validate_config(text.replace(old,new),'stm32h723xx')

    def test_shared_vid_pid_cartographer_never_eligible(self):
        d={'vid':'1d50','pid':'614e','serial':'1234','manufacturer':'Cartographer','product':'stm32g431xx'}
        with self.assertRaises(fp.FlashError): flashing.eligibility(d,'stm32h723xx')
        d.update(manufacturer='Klipper',product='stm32h723xx')
        self.assertTrue(flashing.eligibility(d,'stm32h723xx').startswith('NORMAL_'))

    def test_exact_target_dfu_selectors_have_no_mass_erase_or_vid_only(self):
        args=fp.dfu_args('5-1','ABC123')
        self.assertIn(',0483:df11',args); self.assertIn('5-1',args); self.assertIn(',ABC123',args)
        self.assertNotIn('mass-erase',' '.join(args)); self.assertNotIn('unprotect',' '.join(args))
        with self.assertRaises(fp.FlashError): fp.dfu_args('any','123')

    def test_flash_memory_alignment_and_permissions(self):
        line='name="@Internal Flash /0x08000000/08*128Kg"'
        self.assertEqual(fp.validate_range(line,0x08020000,70000),1024*1024)
        for start,length in [(0x08010000,70000),(0x08020000,2*1024*1024)]:
            with self.assertRaises(fp.FlashError): fp.validate_range(line,start,length)
        with self.assertRaises(fp.FlashError): fp.validate_range(line.replace('Kg','Ka'),0x08020000,70000)

    def test_healthy_host_held_for_every_unverified_outcome(self):
        for state in ['NOT_REQUESTED','INELIGIBLE','BUILD_FAILED','BOOTLOADER_FAILED','WRITE_FAILED','READBACK_FAILED','NO_ENUMERATION']:
            self.assertEqual(fp.host_action('usable',state),'KEEP_EXISTING_KLIPPER')
        self.assertEqual(fp.host_action('usable','VERIFIED'),'UPDATE_TO_VERIFIED_FIRMWARE_SOURCE')
        for health in ['missing','corrupt']:
            self.assertEqual(fp.host_action(health,'WRITE_FAILED'),'REPAIR_SAME_REVISION_OR_MATCH_MCU_FIRST')

    def test_artifact_checksums_vectors_and_dictionary(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); chip='stm32h723xx'; address=fp.profile(chip)['address']
            (root/'klipper.bin').write_bytes(struct.pack('<II',0x20020000,address+257)+bytes(2048))
            (root/'klipper.dict').write_text(json.dumps({'config':{'MCU':chip}}))
            (root/'build.config').write_text(self.normalized(chip))
            metadata={'schema':1,'chip':chip,'source_commit':'a'*40,'sha256':{n:fp.sha((root/n).read_bytes()) for n in ['klipper.bin','klipper.dict','build.config']}}
            (root/'firmware-manifest.json').write_text(json.dumps(metadata))
            fp.artifact(root)
            (root/'klipper.bin').write_bytes(b'tampered')
            with self.assertRaises(fp.FlashError): fp.artifact(root)

    def installer(self,health='usable',flash=True):
        args=['--apply','--maintenance-confirmed']+(['--flash','--enable-flashing-capability'] if flash else [])
        inst=ap.Installer(ap.arguments(args),{},Guard())
        inst.report.update(klipper_host_health={'state':health},flash_state='ELIGIBLE_UNPROVEN')
        inst.guard.cancelled.wait=Mock()
        for name in ['validate_layout','validate_manifest','ethernet_check','idle_check','setup_state','stop_stack','backup','install_packages','configure_wifi','trim_services']:
            setattr(inst,name,Mock())
        inst.prepare_klipper=Mock(return_value={'klipper':Path('/isolated/source'),'klippy-env':Path('/isolated/env')})
        inst.stage_stack=Mock(return_value={}); inst.activate_stack=Mock(); inst.flash=Mock()
        return inst

    def run_apply(self,inst):
        with patch.object(ap.os,'geteuid',return_value=0,create=True),patch.object(ap,'supported_host'),patch.object(ap,'check_free'),contextlib.redirect_stdout(io.StringIO()):
            inst.apply({})

    def test_failed_flash_cannot_activate_any_candidate_or_new_config(self):
        inst=self.installer()
        inst.flash.side_effect=ap.Refuse('simulated DFU write failure')
        self.run_apply(inst)
        inst.stage_stack.assert_not_called(); inst.activate_stack.assert_not_called()
        inst.configure_wifi.assert_not_called()
        self.assertEqual(inst.report['status'],'FLASH_FAILED_HOST_PRESERVED')
        self.assertEqual(inst.report['klipper_host_action'],'KEEP_EXISTING_KLIPPER')

    def test_missing_corrupt_host_repair_is_separate_from_failed_candidate(self):
        for health in ['missing','corrupt']:
            inst=self.installer(health=health); inst.flash.side_effect=ap.Refuse('flash failed')
            inst.repair_klipper_only=Mock(return_value=True)
            self.run_apply(inst)
            inst.repair_klipper_only.assert_called_once()
            inst.stage_stack.assert_not_called(); inst.activate_stack.assert_not_called()
            self.assertEqual(inst.report['status'],'FLASH_FAILED_HOST_REPAIRED_NOT_READY')

    def test_no_verified_receipt_cannot_activate_candidate(self):
        inst=self.installer(); inst.activate_path=Mock()
        self.run_apply(inst)
        inst.activate_path.assert_not_called()
        self.assertEqual(inst.report['status'],'FLASH_FAILED_HOST_PRESERVED')

    def test_verified_host_promoted_before_other_stack_staging(self):
        inst=self.installer(); inst.activate_path=Mock()
        def verified(source): inst.report.update(flash_state='VERIFIED',klipper_host_action='UPDATE_TO_VERIFIED_FIRMWARE_SOURCE')
        inst.flash.side_effect=verified
        inst.stage_stack.side_effect=ap.Refuse('later optional dependency failed')
        with self.assertRaises(ap.Refuse): self.run_apply(inst)
        self.assertTrue(inst.report['klipper_host_promoted'])
        self.assertEqual(inst.activate_path.call_count,2)

    def test_wait_tolerates_sysfs_before_udev_serial_link(self):
        guard=Guard(); guard.cancelled.wait=Mock()
        txn=flashing.Transaction(guard,Mock(),Path('/unused'),Mock(),Mock())
        with patch.object(flashing,'match_port',return_value={'port':'5-1'}),patch.object(flashing,'serial_path',side_effect=[fp.FlashError('udev not ready'),'/dev/serial/by-id/ready']):
            self.assertEqual(txn.wait('5-1',lambda d:bool(flashing.serial_path(d)),1),{'port':'5-1'})


class TransactionTests(unittest.TestCase):
    installer=FlashPolicyTests.installer
    run_apply=FlashPolicyTests.run_apply
    def exercise(self,failure=None,normal_entry=False):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root=Path(temp.name); artifact=root/'artifact'; artifact.mkdir(); state=root/'state'; state.mkdir()
        p=fp.profile('stm32h723xx'); image=struct.pack('<II',0x20020000,p['address']+257)+bytes(2048)
        (artifact/'klipper.bin').write_bytes(image)
        dictionary={'version':'test-build','config':{'MCU':'stm32h723xx'}}
        metadata={'source_commit':'a'*40,'sha256':{}}
        before=bytearray(b'\xaa'*(1024*1024)); struct.pack_into('<II',before,0,0x20020000,0x08000101)
        if failure=='blankboot': before[:8]=b'\xff'*8
        after=bytearray(before); offset=p['address']-0x08000000; after[offset:offset+len(image)]=image
        if failure=='readback': after[offset+300]^=1
        if failure=='bootloader': after[100]^=1
        written=[False]; calls=[]
        device={'port':'5-1','vid':'0483','pid':'df11','serial':'ABC123','devnum':'3'}
        line='Found DFU: path="5-1", alt=0, name="@Internal Flash /0x08000000/08*128Kg", serial="ABC123"'
        def run(args,**kwargs):
            args=list(map(str,args)); calls.append(args)
            if '--list' in args: return 0,line
            if args[:3]==['python3','-B','-c']:
                self.assertIn('O_NOCTTY',args[3]); self.assertIn('SIGHUP',args[3])
                return -1,'simulated hangup despite completed DFU transition'
            if '--upload' in args:
                Path(args[args.index('--upload')+1]).write_bytes(after if written[0] else before)
            if '--download' in args:
                if failure=='write': raise ap.Refuse('simulated write error')
                written[0]=True
            return 0,''
        runner=Mock(); runner.run.side_effect=run
        guard=Guard(); target=Mock(); target.start.return_value=target; target.cancelled=threading.Event()
        identity=Mock(return_value={'wrong':'dictionary'} if failure=='identity' else dictionary)
        if failure=='reset-identity': identity.side_effect=[dictionary,{'wrong':'after reset'}]
        reset=Mock()
        txn=flashing.Transaction(guard,runner,state,Mock(),identity,reset)
        normal={'port':'5-1','serial':'NORMAL','devnum':'4'}
        txn.wait=Mock(side_effect=[normal,dict(normal,devnum='5')])
        if normal_entry:
            initial={'port':'5-1','serial':'NORMAL','devnum':'2','manufacturer':'Klipper','product':'stm32h723xx','vid':'1d50','pid':'614e'}
            txn.wait=Mock(side_effect=[device,normal,dict(normal,devnum='5')])
        else: initial=device
        def write(path,data,guard,mode=0o600):
            guard.check(); path.write_bytes(data.encode() if isinstance(data,str) else data)
        with patch.object(fp,'artifact',return_value=(metadata,p,dictionary)),patch.object(ap,'USBGuard',return_value=target),patch.object(ap,'Runner',return_value=runner),patch.object(ap,'atomic_write',side_effect=write),patch.object(flashing,'serial_path',return_value='/dev/serial/by-id/selected'):
            if failure:
                with self.assertRaises((ap.Refuse,fp.FlashError)): txn.execute(artifact,initial,allow_request=normal_entry)
            else: txn.execute(artifact,initial,allow_request=normal_entry)
        return txn,calls,reset

    def test_success_needs_readback_unchanged_bootloader_identity_and_reset(self):
        txn,calls,reset=self.exercise()
        self.assertEqual(txn.result['state'],'VERIFIED')
        self.assertTrue(txn.result['bootloader_preserved']); self.assertTrue(txn.result['reset_boot_verified'])
        self.assertEqual(sum('--download' in call for call in calls),1)
        self.assertEqual(sum('--upload' in call for call in calls),2)
        reset.assert_called_once()

    def test_write_readback_or_bootloader_failure_has_no_retry_or_leave(self):
        for failure in ['write','readback','bootloader']:
            txn,calls,reset=self.exercise(failure)
            self.assertNotEqual(txn.result['state'],'VERIFIED')
            self.assertEqual(sum('--download' in call for call in calls),1)
            self.assertFalse(any(':leave' in ' '.join(call) for call in calls))
            reset.assert_not_called()

    def test_wrong_live_dictionary_is_not_a_verified_flash(self):
        txn,calls,reset=self.exercise('identity')
        self.assertEqual(txn.result['state'],'READBACK_VERIFIED')
        reset.assert_not_called()

    def test_blank_bootloader_never_receives_application_only_write(self):
        txn,calls,reset=self.exercise('blankboot')
        self.assertFalse(txn.result['firmware_write_started'])
        self.assertFalse(any('--download' in call for call in calls))

    def test_post_reset_old_or_wrong_image_keeps_host_gate_closed(self):
        txn,calls,reset=self.exercise('reset-identity')
        self.assertNotEqual(txn.result['state'],'VERIFIED')
        reset.assert_called_once()

    def test_bootloader_request_exit_is_not_the_usb_transition_proof(self):
        txn,calls,reset=self.exercise(normal_entry=True)
        self.assertEqual(txn.result['bootloader_request_exit'],-1)
        self.assertEqual(txn.result['state'],'VERIFIED')

    def test_ineligible_flash_stops_before_packages_or_stack_stop(self):
        inst=self.installer(); inst.report['flash_state']='INELIGIBLE'
        self.run_apply(inst)
        inst.install_packages.assert_not_called(); inst.stop_stack.assert_not_called()
        inst.prepare_klipper.assert_not_called(); inst.activate_stack.assert_not_called()

    def test_cancelled_flash_is_not_a_retry_or_fallback_activation(self):
        inst=self.installer(); inst.flash.side_effect=ap.Removed('trigger removed')
        with self.assertRaises(ap.Removed): self.run_apply(inst)
        inst.stage_stack.assert_not_called(); inst.activate_stack.assert_not_called()

    def test_healthy_host_with_no_flash_gets_keep_decision(self):
        inst=self.installer(flash=False); self.run_apply(inst)
        inst.prepare_klipper.assert_not_called(); inst.flash.assert_not_called()
        self.assertEqual(inst.report['klipper_host_action'],'KEEP_EXISTING_KLIPPER')


if __name__=='__main__': unittest.main()
