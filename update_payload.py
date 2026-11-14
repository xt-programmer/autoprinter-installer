#!/usr/bin/env python3
"""Insertion-only latest Klipper/Octopus update, cleanup and bounded recovery."""
import argparse
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import time
import tempfile
import threading

import autoprinter as ap
import flash_policy as fp
import octopus_flasher as of
import paired_update as paired
import update_storage as storage
from update_transaction import UpdateTransaction
from host_setup import SSHGuard

UPSTREAM='https://github.com/Klipper3d/klipper.git'
HOME=Path('/home/pi')
DATA=HOME/'printer_data'
STATE_ROOT=Path('/var/lib/autoprinter')
LOCK_PATH=Path('/run/autoprinter.lock')


def screen(kind,message):
    color={'error':'41;97','success':'42;30','info':'44;97','warning':'43;30'}[kind]
    print('\n\033[1;'+color+'m  AUTOPRINTER: '+kind.upper()+'  \033[0m\n\033[1m'+str(message)+'\033[0m\n',flush=True)


def target(port=None,serial=None,chip=None):
    devices=[of.match_port(port)] if port else ap.usb_devices()
    eligible=[]
    for device in devices:
        inferred=device.get('product')
        if serial and device.get('serial')!=serial: continue
        selected=chip or inferred
        if selected not in fp.PROFILES: continue
        try: mode=of.eligibility(device,selected)
        except fp.FlashError: continue
        if mode.startswith('NORMAL_'): eligible.append((device,selected))
    if len(eligible)!=1: raise fp.FlashError('Require exactly one visible supported normal Klipper Octopus; supply exact target selectors if ambiguous')
    return eligible[0]


def resolve_revision(runner,pin=None):
    if pin: return ap.require_digest(pin,40)
    _,out=runner.run(['git','ls-remote',UPSTREAM,'refs/heads/master'],timeout=60)
    lines=[line.split() for line in out.splitlines() if line.strip()]
    if len(lines)!=1 or len(lines[0])!=2 or lines[0][1]!='refs/heads/master': raise fp.FlashError('Cannot uniquely resolve latest official Klipper master')
    return ap.require_digest(lines[0][0],40)


def stage_source(runner,work,revision):
    candidate=work/'klipper'
    runner.run(['git','init',candidate],user='pi')
    runner.run(['git','-C',candidate,'remote','add','origin',UPSTREAM],user='pi')
    runner.run(['git','-C',candidate,'fetch','--depth=1','origin',revision],user='pi',timeout=600)
    runner.run(['git','-C',candidate,'checkout','-B','master',revision],user='pi')
    _,actual=runner.run(['git','-C',candidate,'rev-parse','HEAD'],user='pi')
    if actual!=revision: raise fp.FlashError('Staged source differs from frozen latest revision')
    runner.run(['git','-C',candidate,'update-ref','refs/remotes/origin/master',revision],user='pi')
    runner.run(['git','-C',candidate,'branch','--set-upstream-to=origin/master','master'],user='pi')
    return candidate


def build_tools(runner):
    packages=['gcc-arm-none-eabi','binutils-arm-none-eabi','libnewlib-arm-none-eabi','build-essential','python3-dev','python3-venv','libffi-dev','dfu-util']
    missing=[]
    for package in packages:
        code,out=runner.run(['dpkg-query','-W','-f=${Status}',package],check=False,timeout=15)
        if code or out!='install ok installed': missing.append(package)
    if missing:
        runner.run(['apt-get','update'],timeout=600)
        runner.run(['apt-get','--yes','--no-install-recommends','--no-upgrade','install']+missing,timeout=1800)
    return missing


def rewrite_scripts(env,old,new,guard):
    for path in (env/'bin').iterdir():
        guard.check()
        if path.is_symlink() or not path.is_file() or path.stat().st_size>1024*1024: continue
        raw=path.read_bytes()
        if b'\x00' in raw or str(old).encode() not in raw: continue
        ap.atomic_write(path,raw.replace(str(old).encode(),str(new).encode()),guard,path.stat().st_mode&0o777)


def stage_environment(runner,guard,env,work,source):
    candidate=work/'klippy-env'
    def copy(src,dst):
        guard.check()
        with open(src,'rb') as inp,open(dst,'xb') as out:
            while True:
                guard.check(); chunk=inp.read(1024*1024)
                if not chunk: break
                out.write(chunk)
        shutil.copystat(src,dst); return dst
    shutil.copytree(env,candidate,symlinks=True,ignore=shutil.ignore_patterns('__pycache__'),copy_function=copy)
    rewrite_scripts(candidate,env,candidate,guard)
    runner.run(['chown','-R','--no-dereference','pi:pi',candidate])
    runner.run([candidate/'bin/python','-m','pip','install','--no-cache-dir','-r',source/'scripts/klippy-requirements.txt'],user='pi',timeout=1200)
    runner.run([candidate/'bin/python','-m','pip','check'],user='pi',timeout=60)
    runner.run([candidate/'bin/python','-B',source/'klippy/klippy.py','--help'],user='pi',timeout=60)
    return candidate


def build(runner,guard,source,chip,output,revision):
    _,actual=runner.run(['git','-C',source,'rev-parse','HEAD'],user='pi')
    if actual!=revision: raise fp.FlashError('Source changed before firmware build')
    runner.run(['make','clean'],cwd=source,user='pi',timeout=60)
    ap.atomic_write(source/'.config',fp.config_seed(chip),guard,0o644)
    runner.run(['chown','pi:pi',source/'.config'])
    runner.run(['make','olddefconfig'],cwd=source,user='pi',timeout=60)
    fp.validate_config((source/'.config').read_text(),chip)
    runner.run(['make','-j2'],cwd=source,user='pi',timeout=1800)
    _,actual=runner.run(['git','-C',source,'rev-parse','HEAD'],user='pi')
    _,dirty=runner.run(['git','-C',source,'status','--porcelain','--untracked-files=no'],user='pi',env={'GIT_OPTIONAL_LOCKS':'0'})
    if actual!=revision or dirty: raise fp.FlashError('Source revision/contents changed during build')
    output.mkdir(mode=0o700)
    for src,dest in [('out/klipper.bin','klipper.bin'),('out/klipper.dict','klipper.dict'),('.config','build.config')]:
        shutil.copyfile(source/src,output/dest)
    _,compiler=runner.run(['arm-none-eabi-gcc','--version'],timeout=15)
    manifest={'schema':1,'chip':chip,'source_commit':revision,'compiler':compiler.splitlines()[0],
              'sha256':{name:fp.sha((output/name).read_bytes()) for name in ('klipper.bin','klipper.dict','build.config')}}
    ap.atomic_write(output/'firmware-manifest.json',json.dumps(manifest,indent=2),guard)
    return fp.artifact(output)


def promote(path,candidate,stamp,state,guard):
    retained=path.with_name(path.name+'.before-update-'+stamp)
    if retained.exists() or retained.is_symlink() or path.is_symlink() or candidate.is_symlink():
        raise fp.FlashError('Promotion paths are not unique native directories')
    ap.atomic_write(state/(path.name+'-promotion.json'),json.dumps({'path':str(path),'candidate':str(candidate),'retained':str(retained)}),guard)
    guard.check(); path.rename(retained)
    try:
        guard.check(); candidate.rename(path)
    except Exception:
        if not guard.cancelled.is_set() and not path.exists(): retained.rename(path)
        raise
    return retained


def return_unwritten(txn,service):
    """Failed DFU entry with original normal firmware still present: no flash write."""
    original=txn.result['initial_usb']; txn.guard.check()
    device=of.match_port(original['port'])
    if any(device.get(k)!=original[k] for k in ('vid','pid','serial','product','manufacturer')):
        raise fp.FlashError('Original board not present normally; cannot restore its running state')
    if txn.result.get('before_identity') and txn.identify(of.serial_path(device))!=txn.result['before_identity']:
        raise fp.FlashError('Original running firmware identity changed after failed DFU entry')
    if service.preexisting_protocol_error:
        service.runner.run(['systemctl','start','klipper.service'],timeout=30)
        return 'PREVIOUS_PROTOCOL_ERROR_STATE_RESTORED'
    return service.resume()


def download_workspace(module_path):
    temp=Path(tempfile.gettempdir()).resolve()
    for parent in Path(module_path).resolve().parents:
        if parent.parent==temp and parent.name.startswith('autoprinter-download-') and not parent.is_symlink() and (parent/'bundle.zip').is_file():
            return parent
    return None


def verified_gate(result,revision):
    if result.get('state')!='VERIFIED' or result.get('source_commit')!=revision or not result.get('reset_boot_verified'):
        raise fp.FlashError('Installed host promotion requires this frozen firmware to pass read-back, live identify and reset verification')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('--klipper-update-only',action='store_true')
    parser.add_argument('--chip',choices=list(fp.PROFILES)+['auto'],default='auto')
    parser.add_argument('--target-port'); parser.add_argument('--target-serial')
    parser.add_argument('--source-commit',help='optional exact upstream SHA; default resolves latest official master once')
    parser.add_argument('--bench-entry-space-mib',type=int,help='explicit supervised-SSH bench allowance, 2048..3072 MiB; normal default remains 3072')
    for flag in ('apply','enable-flashing-capability','maintenance-confirmed','actuator-power-disconnected','board-confirmed-octopus','allow-bootloader-request','supervised-ssh','flash','prepare-host-update'):
        parser.add_argument('--'+flag,action='store_true')
    parser.add_argument('--ssh-session-pid',type=int)
    for name in ('port','vid','pid','serial'): parser.add_argument('--usb-'+name)
    args=parser.parse_args(argv)
    if args.bench_entry_space_mib is not None and (not args.apply or not args.supervised_ssh or not 2048<=args.bench_entry_space_mib<=3072):
        parser.error('--bench-entry-space-mib requires apply + supervised SSH and a value from 2048 to 3072')
    entry_space=(args.bench_entry_space_mib or 3072)*ap.MIB
    guard=lock=log=service=txn=state=work=candidate=retained=old_private=None
    host=HOME/'klipper'; env=HOME/'klippy-env'
    report={'status':'AUDIT_ONLY','host_updated':False,'env_updated':False,'pair_restored':False,'environment_path':str(env)}
    report['entry_space_requirement_bytes']=entry_space
    if args.bench_entry_space_mib is not None: report['supervised_bench_entry_space_mib']=args.bench_entry_space_mib
    code=2
    try:
        device,chip=target(args.target_port,args.target_serial,None if args.chip=='auto' else args.chip)
        report.update(target=device,chip=chip,profile=fp.profile(chip))
        if not args.apply:
            runner=ap.Runner(type('AuditGuard',(),{'check':lambda s:None,'cancelled':threading.Event()})(),lambda m:None)
            _,report['host_commit_before']=runner.run(['git','-C',host,'rev-parse','HEAD'],timeout=15,user='pi' if getattr(os,'geteuid',lambda:-1)()==0 else None)
            report['free_bytes']=shutil.disk_usage(HOME).free
            report['target_source_commit']=resolve_revision(runner,args.source_commit)
            report['cleanup_plan']=storage.cleanup_plan(DATA)
            print(json.dumps(report,indent=2)); return 0
        if not all(getattr(args,name) for name in ('enable_flashing_capability','maintenance_confirmed','actuator_power_disconnected','board_confirmed_octopus','allow_bootloader_request')):
            raise fp.FlashError('Apply requires capability, maintenance, isolated actuator power, physical Octopus confirmation and bootloader-request flags')
        if os.geteuid()!=0: raise fp.FlashError('sudo required')
        hardware=ap.inspect_host(); ap.supported_host(dict(hardware,os={'ID':'debian','VERSION_ID':'12'}))
        if hardware.get('os',{}).get('ID') not in ('debian','ubuntu'): raise fp.FlashError('Build dependency provisioning supports Debian/Ubuntu only')
        if args.supervised_ssh:
            if not args.ssh_session_pid or args.usb_port: raise fp.FlashError('Supply the live SSH shell PID and no USB selector')
            guard=SSHGuard(args.ssh_session_pid)
        else:
            selector={name:getattr(args,'usb_'+name) for name in ('port','vid','pid','serial') if getattr(args,'usb_'+name) is not None}
            trigger=ap.select_usb(ap.usb_devices(),selector)
            inherited=os.environ.get('AUTOPRINTER_TRIGGER_SESSION')
            if inherited and any(trigger.get(k)!=v for k,v in json.loads(inherited).items()): raise ap.Removed('Trigger changed since download')
            if trigger['port']==device['port']: raise fp.FlashError('Board cannot supervise its own reset; use separate HID/SSH')
            guard=ap.USBGuard(trigger).start()
        def interrupted(signum,frame): guard.cancelled.set()
        signal.signal(signal.SIGINT,interrupted); signal.signal(signal.SIGTERM,interrupted)
        import fcntl
        lock=os.fdopen(os.open(LOCK_PATH,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600),'a')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        state=STATE_ROOT/('update-'+stamp); state.mkdir(parents=True,mode=0o700)
        report['state_directory']=str(state)
        log=(state/'run.log').open('a',buffering=1)
        def emit(text): log.write(text+'\n'); print(text[-1000:],flush=True)
        runner=ap.Runner(guard,emit)
        def save(): ap.atomic_write(state/'report.json',json.dumps(report,indent=2),guard)
        def record(entry):
            with (state/'cleanup.jsonl').open('a') as out: out.write(json.dumps(entry)+'\n'); out.flush(); os.fsync(out.fileno())
        def inactive():
            _,active=runner.run(['systemctl','is-active','klipper'],timeout=15,check=False)
            if active!='inactive': raise fp.FlashError('Klipper must remain inactive during MCU work')
        service=paired.Service(runner,guard,state); service.preflight(allow_protocol_failure=True)
        report['service_was_active']=service.was_active
        if any(path.is_symlink() or not path.is_dir() for path in (host,env,DATA)): raise fp.FlashError('Native Klipper source/environment/data directories required')
        _,report['host_commit_before']=runner.run(['git','-C',host,'rev-parse','HEAD'],user='pi')
        _,report['host_version_before']=runner.run(['git','-C',host,'describe','--always','--tags','--long'],user='pi')
        report['host_entry_sha256_before']=ap.sha256(host/'klippy/klippy.py',guard)
        _,dirty=runner.run(['git','-C',host,'status','--porcelain','--untracked-files=no'],user='pi',env={'GIT_OPTIONAL_LOCKS':'0'})
        if dirty: raise fp.FlashError('Tracked host source edits require review before replacement')
        runner.run([env/'bin/python','-m','pip','check'],timeout=60)
        _,freeze=runner.run([env/'bin/python','-m','pip','freeze','--all'],timeout=60)
        report['env_freeze_sha256_before']=fp.sha(freeze.encode())
        report['env_config_sha256_before']=ap.sha256(env/'pyvenv.cfg',guard)
        screen('info','Board visible: %s / %s. Host: %s'%(chip,device['serial'],report['host_version_before']))
        protected=[]
        if service.was_active and not service.preexisting_protocol_error:
            snapshot,_=ap.printer_snapshot('http://127.0.0.1:7125',os.environ.get('AUTOPRINTER_MOONRAKER_KEY'))
            filename=snapshot.get('print_stats',{}).get('filename')
            if filename: protected.append(DATA/'gcodes'/filename)
            file_path=snapshot.get('virtual_sdcard',{}).get('file_path')
            if file_path: protected.append(Path(file_path))
        service.stop(); inactive()
        report['free_bytes_before']=shutil.disk_usage(HOME).free
        if args.bench_entry_space_mib is not None:
            screen('warning','Supervised SSH bench entry allowance: %d MiB. Environment-copy and 500 MiB MCU reserves remain enforced.'%args.bench_entry_space_mib)
        screen('info','MicroSD free: %.1f MiB. Checking space and authorized cleanup.'%(report['free_bytes_before']/ap.MIB))
        report['cleanup_freed_bytes']=storage.ensure_space(['/','/var',HOME],DATA,guard,runner,record,protected,minimum=entry_space,include_gcodes=not service.preexisting_protocol_error)
        revision=resolve_revision(runner,args.source_commit); report['target_source_commit']=revision; save()
        screen('info','Latest Klipper frozen at '+revision+'. Preparing isolated host and toolchain.')
        report['new_build_packages']=build_tools(runner)
        work=HOME/'.autoprinter'/('update-'+stamp); work.mkdir(parents=True,mode=0o755); runner.run(['chown','pi:pi',work])
        candidate=stage_source(runner,work,revision)
        _,tracked=runner.run(['git','-C',host,'ls-files','klippy/extras','klippy/plugins'],user='pi',env={'GIT_OPTIONAL_LOCKS':'0'})
        report['preserved_plugins']=paired.stage_plugins(host,candidate,set(tracked.splitlines()),guard)
        runner.run(['chown','-R','--no-dereference','pi:pi',candidate/'klippy'])
        _,env_size=runner.run(['du','-sb',env],timeout=60)
        storage.ensure_space([HOME],DATA,guard,runner,record,protected,minimum=int(env_size.split()[0])+700*ap.MIB,include_gcodes=not service.preexisting_protocol_error)
        candidate_env=stage_environment(runner,guard,env,work,candidate)
        ap.check_free([state,HOME],500*ap.MIB)
        inactive()
        old_private=state/'identify-source'; old_private.mkdir()
        shutil.copytree(host/'klippy',old_private/'klippy'); (old_private/'lib').mkdir()
        if (host/'lib/fast-hash').exists(): shutil.copytree(host/'lib/fast-hash',old_private/'lib/fast-hash')
        def identify(path,source=old_private,python_env=None,reset_dict=None,core=False):
            inactive(); extra=[]
            if python_env is None: python_env=Path(report['env_retained']) if report.get('env_updated') else env
            if reset_dict is not None:
                saved=state/'reset-dictionary.json'; ap.atomic_write(saved,json.dumps(reset_dict),guard)
                extra=['--reset-after-identify','--expected-dictionary-file',saved]
            if core: extra+=['--check-host-protocol']
            result,out=runner.run([python_env/'bin/python','-B',Path(__file__).with_name('mcu_identify.py'),'--klippy',source/'klippy','--serial',path]+extra,timeout=30,check=False)
            if any(s.startswith('MCU_PROTOCOL_ERROR ') for s in out.splitlines()): raise fp.ProtocolError('MCU protocol parser error: '+out[-1000:])
            values=[json.loads(s.split(' ',1)[1]) for s in out.splitlines() if s.startswith('MCU_IDENTIFY_JSON ')]
            if result or len(values)!=1: raise fp.FlashError('MCU identify failed: '+out[-1000:])
            return values[0]
        dictionary=None
        txn=UpdateTransaction(guard,runner,state,inactive,identify,lambda path:identify(path,reset_dict=dictionary),notify=screen)
        report['transaction']=txn.result
        txn.prepare(device,chip)
        screen('info','DFU verified and original firmware backed up. Cleaning/configuring/building '+chip+'.')
        # DFU target disappearance during compilation cancels the build, not the trigger session.
        build_guard=ap.USBGuard(txn.result['dfu_usb']).start()
        try:
            build_runner=ap.Runner(of.CombinedGuard(guard,build_guard),emit)
            metadata,profile,dictionary=build(build_runner,guard,candidate,chip,state/'firmware',revision)
        finally: build_guard.close()
        screen('info','Build verified. Checking DFU again; at most two image attempts.')
        try: result=txn.program(state/'firmware')
        except fp.ProtocolError:
            screen('warning','Protocol mismatch during firmware verification. One synchronization pass using the same frozen build.')
            result=txn.program(state/'firmware')
            report['protocol_sync_attempted']=True
        verified_gate(result,revision)
        if identify(of.serial_path(result['normal_usb']),source=candidate,python_env=candidate_env,core=True)!=dictionary:
            raise fp.ProtocolError('Candidate host cannot identify its exact built firmware')
        paired.verify_plugins(host,report['preserved_plugins'],guard); paired.verify_plugins(candidate,report['preserved_plugins'],guard)
        _,current=runner.run(['git','-C',host,'rev-parse','HEAD'],user='pi')
        if current!=report['host_commit_before'] or ap.sha256(host/'klippy/klippy.py',guard)!=report['host_entry_sha256_before']: raise fp.FlashError('Installed host changed during transaction')
        rewrite_scripts(candidate_env,candidate_env,env,guard)
        retained=promote(host,candidate,stamp,state,guard); report.update(host_updated=True,host_retained=str(retained)); save()
        retained_env=promote(env,candidate_env,stamp,state,guard); report.update(env_updated=True,env_retained=str(retained_env),host_commit_after=revision); save()
        try: report['service_after_update']=service.resume()
        except fp.ProtocolError:
            report['protocol_sync_attempted']=True
            screen('warning','Protocol error: checking selected MCU/host alignment and restarting Klipper once.')
            service.stop_for_recovery()
            live=identify(of.serial_path(of.match_port(device['port'])))
            if live!=dictionary: txn.program(state/'firmware')
            _,actual=runner.run(['git','-C',host,'rev-parse','HEAD'],user='pi')
            if actual!=revision: raise fp.ProtocolError('Host changed away from frozen firmware revision')
            report['service_after_update']=service.resume()
        report.update(status='UPDATE_VERIFIED_READY_IDLE' if service.was_active else 'UPDATE_VERIFIED_KLIPPER_PRESERVED_INACTIVE',flash_state=txn.result['state'])
        report['update_complete']=True
        runner.run(['make','clean'],cwd=host,user='pi',timeout=60)
        runner.run(['apt-get','clean'],timeout=60)
        storage.remove_scratch(old_private,state,guard); old_private=None
        storage.remove_scratch(work,work.parent,guard); work=None
        download=download_workspace(__file__)
        if download: storage.remove_scratch(download,download.parent,guard)
        report['free_bytes_after']=shutil.disk_usage(HOME).free; save()
        screen('success','Klipper + board match '+revision[:12]+'. '+('Ready and idle.' if service.was_active else 'Update verified; Klipper remains inactive.'))
        guard.cancelled.wait(5); guard.check(); code=0
    except (ap.Refuse,OSError,ValueError,KeyError,TypeError) as error:
        report.update(status='UPDATE_ABORTED',error=str(error))
        if guard and guard.cancelled.is_set(): code=130
        elif report.get('update_complete'):
            report['status']='UPDATE_VERIFIED_CLEANUP_INCOMPLETE'
        elif service and not txn and service.stopped:
            try:
                if service.preexisting_protocol_error:
                    service.runner.run(['systemctl','start','klipper.service'],timeout=30)
                    report['service_after_abort']='PREVIOUS_PROTOCOL_ERROR_STATE_RESTORED'
                else: report['service_after_abort']=service.resume()
                report['status']='PREPARATION_FAILED_OLD_PAIR_UNCHANGED'
            except (ap.Refuse,OSError,ValueError,KeyError,TypeError) as restore:
                report['service_restore_error']=str(restore)
        elif service and txn and txn.result.get('state') in ('IDENTIFYING','IDENTIFIED'):
            try:
                report['service_after_abort']=return_unwritten(txn,service)
                report['status']='DFU_FAILED_OLD_FIRMWARE_AND_HOST_UNCHANGED'
            except (ap.Refuse,OSError,ValueError,KeyError,TypeError) as restore:
                report['service_restore_error']=str(restore)
        elif service and txn and txn.result.get('state') in ('BACKED_UP','WRITE_STARTED','PROGRAM_FAILED','READBACK_VERIFIED','VERIFIED'):
            screen('error','Update failed. Attempting saved firmware/host restoration: '+str(error))
            try:
                paired.restore_pair(txn,service,host,retained,report,lambda path,old:identify(path,reset_dict=old),allow_partial=True)
                report['status']='UPDATE_FAILED_PREVIOUS_PAIR_RESTORED'
            except (ap.Refuse,OSError,ValueError,KeyError,TypeError) as recovery:
                report.update(status='RECOVERY_INCOMPLETE',recovery_error=str(recovery))
                if not guard.cancelled.is_set():
                    with contextlib.suppress(ap.Refuse,OSError): service.stop_for_recovery()
        if guard and not guard.cancelled.is_set():
            if txn: report['flash_state']=txn.result['state']
            if state:
                with contextlib.suppress(ap.Refuse,OSError): save()
            screen('error',report['status']+': '+str(error)+'\n'+('Previous firmware and host restored.' if report.get('pair_restored') else 'Inspect the saved report before reuse.'))
            guard.cancelled.wait(8)
        else:
            screen('error',str(error))
            if args.apply and not guard: time.sleep(8)
        print(json.dumps(report,indent=2),flush=True)
    finally:
        if guard and not guard.cancelled.is_set() and state:
            # Keep firmware/flash/retained host/environment recovery material.
            for scratch,owner in [(old_private,state),(work,work.parent if work else state)]:
                if scratch:
                    with contextlib.suppress(ap.Refuse,OSError): storage.remove_scratch(scratch,owner,guard)
        if log: log.close()
        if lock: lock.close()
        if isinstance(guard,ap.USBGuard): guard.close()
        if guard is None or not guard.cancelled.is_set():
            download=download_workspace(__file__)
            if download:
                scratch_guard=guard or type('ScratchGuard',(),{'check':lambda self:None})()
                with contextlib.suppress(ap.Refuse,OSError): storage.remove_scratch(download,download.parent,scratch_guard)
    return code


if __name__=='__main__': sys.exit(main())
