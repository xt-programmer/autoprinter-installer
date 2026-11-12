#!/usr/bin/env python3
"""Supervised exact-target Octopus flash from a hash-verified built artifact.

Default is an eligibility audit. No firmware or host change is implied by an
eligible decision. Only VERIFIED read-back + live identify permits host promotion.
"""
import argparse,contextlib,datetime,json,os,re,shutil,signal
from pathlib import Path
import autoprinter as ap
import flash_policy as fp
import octopus_flasher as of
from host_setup import SSHGuard


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('--firmware-bundle',type=Path,help='external reviewed bundle; default is the release-pinned bundled image for --chip')
    parser.add_argument('--expected-firmware-sha256',help='required for an external firmware bundle')
    parser.add_argument('--target-port',required=True); parser.add_argument('--target-serial',required=True)
    parser.add_argument('--chip',choices=fp.PROFILES,required=True)
    for flag in ['apply','flash','enable-flashing-capability','maintenance-confirmed','actuator-power-disconnected','board-confirmed-octopus','allow-bootloader-request','prepare-host-update','supervised-ssh']:
        parser.add_argument('--'+flag,action='store_true')
    parser.add_argument('--ssh-session-pid',type=int)
    for field in ['port','vid','pid','serial']: parser.add_argument('--usb-'+field)
    args=parser.parse_args(argv)
    guard=lock=log=None; state=None
    report={'status':'AUDIT_ONLY','flash_state':'NOT_ATTEMPTED','host_updated':False}
    try:
        bundled=args.firmware_bundle is None
        if bundled: args.firmware_bundle=Path(__file__).parent/'firmware-bundles'/args.chip
        metadata,profile,dictionary=fp.artifact(args.firmware_bundle)
        expected=args.expected_firmware_sha256 or (metadata['sha256']['klipper.bin'] if bundled else '')
        if metadata['chip']!=args.chip or metadata['sha256']['klipper.bin']!=ap.require_digest(expected):
            raise fp.FlashError('Artifact chip/hash differs from the explicitly reviewed image')
        device=of.match_port(args.target_port)
        if device['serial']!=args.target_serial: raise fp.FlashError('Target serial changed')
        report.update(eligibility=of.eligibility(device,args.chip),profile=profile,source_commit=metadata['source_commit'],initial_usb=device)
        if not args.apply:
            print(json.dumps(report,indent=2)); return 0
        if not all([args.flash,args.enable_flashing_capability,args.maintenance_confirmed,args.actuator_power_disconnected,args.board_confirmed_octopus]):
            raise fp.FlashError('Apply requires --flash, capability, maintenance, disconnected actuator power and confirmed Octopus flags')
        if os.geteuid()!=0: raise fp.FlashError('sudo required')
        hardware=ap.inspect_host()
        ap.supported_host(dict(hardware,os={'ID':'debian','VERSION_ID':'12'}))
        if args.supervised_ssh:
            if not args.ssh_session_pid or args.usb_port: raise fp.FlashError('SSH guard needs its live pi shell PID, without a USB trigger')
            guard=SSHGuard(args.ssh_session_pid)
        else:
            selector={k:getattr(args,'usb_'+k) for k in ['port','vid','pid','serial'] if getattr(args,'usb_'+k) is not None}
            trigger=ap.select_usb(ap.usb_devices(),selector)
            inherited=os.environ.get('AUTOPRINTER_TRIGGER_SESSION')
            if inherited and any(trigger.get(k)!=v for k,v in json.loads(inherited).items()): raise ap.Removed('USB trigger changed during download')
            if trigger['port']==device['port']: raise fp.FlashError('Trigger must be separate from the target being reset')
            guard=ap.USBGuard(trigger).start()
        def interrupted(signum,frame): guard.cancelled.set()
        signal.signal(signal.SIGINT,interrupted); signal.signal(signal.SIGTERM,interrupted)
        import fcntl
        lock=os.fdopen(os.open('/run/autoprinter.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600),'a')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        ap.check_free(['/','/var','/home/pi'])
        if not shutil.which('dfu-util'): raise fp.FlashError('dfu-util missing; provision it separately before this artifact-only transaction')
        guard.check()
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')
        state=Path('/var/lib/autoprinter')/('board-flash-'+stamp)
        state.mkdir(parents=True,mode=0o700); report['state_directory']=str(state)
        log=(state/'run.log').open('a',buffering=1)
        def emit(message): log.write(message+'\n'); print(message[-1000:],flush=True)
        runner=ap.Runner(guard,emit)
        def inactive():
            _,active=runner.run(['systemctl','is-active','klipper'],timeout=15,check=False)
            if active!='inactive': raise fp.FlashError('Klipper must be inactive before build/DFU; no automatic service stop')
        inactive()
        host=Path('/home/pi/klipper'); env=Path('/home/pi/klippy-env')
        _,before_commit=runner.run(['runuser','-u','pi','--','git','-C',host,'rev-parse','HEAD'])
        report['host_commit_before']=before_commit
        before_entry_hash=ap.sha256(host/'klippy/klippy.py',guard)
        runner.run([env/'bin/python','-B',host/'klippy/klippy.py','--help'],timeout=30)
        runner.run([env/'bin/python','-m','pip','check'],timeout=30)
        candidate=None
        if args.prepare_host_update:
            directory=Path('/home/pi/.autoprinter')/('flash-'+stamp)
            directory.mkdir(parents=True,mode=0o755)
            runner.run(['chown','pi:pi',directory])
            candidate=directory/'klipper'
            runner.run(['git','clone','--no-checkout','--filter=blob:none','https://github.com/Klipper3d/klipper.git',candidate],user='pi',env={'HOME':'/home/pi'},timeout=600)
            runner.run(['git','-C',candidate,'checkout','--detach',metadata['source_commit']],user='pi',timeout=600)
            _,candidate_commit=runner.run(['git','-C',candidate,'rev-parse','HEAD'],user='pi')
            if candidate_commit!=metadata['source_commit']: raise fp.FlashError('Staged host revision mismatch')
            if (candidate/'scripts/klippy-requirements.txt').read_bytes()!=(host/'scripts/klippy-requirements.txt').read_bytes():
                raise fp.FlashError('Requirements changed: stage a separately validated environment before host promotion')
            _,dirty=runner.run(['git','-C',host,'status','--porcelain','--untracked-files=no'],user='pi',env={'GIT_OPTIONAL_LOCKS':'0'})
            if dirty: raise fp.FlashError('Existing tracked host edits must be preserved/reviewed before promotion')
            for sub in ['extras','plugins']:
                directory=host/'klippy'/sub
                if directory.exists():
                    for path in directory.rglob('*'):
                        if path.is_file() and path.suffix in ('.py','.so') and '__pycache__' not in path.parts and not (candidate/'klippy'/sub/path.relative_to(directory)).exists():
                            raise fp.FlashError('External plugin requires reviewed transfer before host promotion: '+str(path))
            runner.run([env/'bin/python','-B',candidate/'klippy/klippy.py','--help'],timeout=30)
            report['staged_host']=str(candidate)
        ap.check_free([state],500*ap.MIB)
        private=state/'identify-source'; private.mkdir()
        shutil.copytree(host/'klippy',private/'klippy')
        (private/'lib').mkdir()
        if (host/'lib/fast-hash').exists(): shutil.copytree(host/'lib/fast-hash',private/'lib/fast-hash')
        def identify(path,reset=False):
            inactive()
            extra=['--reset-after-identify','--expected-dictionary-sha256',metadata['sha256']['klipper.dict']] if reset else []
            _,out=runner.run([env/'bin/python','-B',Path(__file__).with_name('mcu_identify.py'),'--klippy',private/'klippy','--serial',path]+extra,timeout=25)
            values=[json.loads(line.split(' ',1)[1]) for line in out.splitlines() if line.startswith('MCU_IDENTIFY_JSON ')]
            if len(values)!=1: raise fp.FlashError('No unique live MCU identity received')
            return values[0]
        txn=of.Transaction(guard,runner,state,inactive,identify,lambda path:identify(path,reset=True))
        report['transaction']=txn.result
        result=txn.execute(args.firmware_bundle,device,allow_request=args.allow_bootloader_request)
        report['flash_state']=result['state']
        if result['state']!='VERIFIED': raise fp.FlashError('Host promotion requires verified firmware')
        inactive(); guard.check()
        _,current=runner.run(['runuser','-u','pi','--','git','-C',host,'rev-parse','HEAD'])
        if current!=before_commit or ap.sha256(host/'klippy/klippy.py',guard)!=before_entry_hash:
            raise fp.FlashError('Installed host changed during the flash transaction')
        if candidate is not None:
            runner.run(['git','-C',candidate,'checkout','-B','master',metadata['source_commit']],user='pi')
            runner.run(['git','-C',candidate,'branch','--set-upstream-to=origin/master','master'],user='pi')
            shutil.copyfile(args.firmware_bundle/'build.config',candidate/'.config')
            runner.run(['chown','pi:pi',candidate/'.config'])
            retained=host.with_name('klipper.before-flash-'+stamp)
            intent={'old':str(host),'retained':str(retained),'candidate':str(candidate),'verified_source':metadata['source_commit']}
            ap.atomic_write(state/'HOST_PROMOTION_INTENT.json',json.dumps(intent,indent=2),guard)
            guard.check(); host.rename(retained)
            try:
                guard.check(); candidate.rename(host)
            except Exception:
                if not guard.cancelled.is_set() and not host.exists(): retained.rename(host)
                raise
            report.update(host_updated=True,host_retained=str(retained),host_commit_after=metadata['source_commit'])
        else: report['host_commit_after']=before_commit
        report['status']='BOARD_AND_HOST_UPDATED_NEEDS_COMMISSIONING' if report['host_updated'] else 'BOARD_VERIFIED_HOST_PRESERVED'
        ap.atomic_write(state/'report.json',json.dumps(report,indent=2),guard)
        print('RESULT '+report['status']+'\nREPORT '+str(state/'report.json')); return 0
    except (ap.Refuse,OSError,ValueError,KeyError,TypeError) as error:
        report.update(status='HOST_PROMOTED_NEEDS_REVIEW' if report.get('host_updated') else 'STOPPED_HOST_NOT_UPDATED',error=str(error))
        if state and guard and not guard.cancelled.is_set():
            with contextlib.suppress(OSError,ap.Refuse): ap.atomic_write(state/'report.json',json.dumps(report,indent=2),guard)
        print(json.dumps(report,indent=2)); return 2
    finally:
        if log: log.close()
        if lock: lock.close()
        if isinstance(guard,ap.USBGuard): guard.close()


if __name__=='__main__': raise SystemExit(main())
