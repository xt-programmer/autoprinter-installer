"""Bounded paired downgrade and Klipper-only service lifecycle.

No recovery after cancellation, transport/write errors or unverified read-back.
Only the application sectors touched by this transaction may be restored.
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import time

import autoprinter as ap
import flash_policy as fp
import octopus_flasher as of


def protocol_error(message):
    return bool(re.search(r'\bMCU Protocol error\b|\bCommand format mismatch\b',message,re.I))


def native_launch(props,env_path=Path('/home/pi/printer_data/systemd/klipper.env')):
    """Read literal KIAUH environment assignments; never source shell content."""
    match=re.search(r'argv\[\]=(.*?) ;',props.get('ExecStart',''))
    if props.get('User')!='pi' or not match: raise fp.FlashError('Require native pi Klipper service')
    args=shlex.split(match[1])
    if not args or args[0] not in ('/home/pi/klippy-env/bin/python','/home/pi/klippy-env/bin/python3'):
        raise fp.FlashError('Service uses a different Python environment')
    if args[1:]==['$KLIPPER_ARGS']:
        if props.get('EnvironmentFiles')!=str(env_path)+' (ignore_errors=no)' or env_path.is_symlink():
            raise fp.FlashError('Unsupported Klipper environment file layout')
        values=[]
        for line in env_path.read_text(encoding='utf-8').splitlines():
            if line.strip().startswith('KLIPPER_ARGS='):
                fields=shlex.split(line.strip())
                if len(fields)!=1: raise fp.FlashError('Unsupported KLIPPER_ARGS assignment')
                values.append(fields[0].split('=',1)[1])
        if len(values)!=1 or '$' in values[0] or '`' in values[0]: raise fp.FlashError('Require literal unique KLIPPER_ARGS')
        args=args[:1]+shlex.split(values[0])
    if len(args)<3 or args[1]!='/home/pi/klipper/klippy/klippy.py' or args[2]!='/home/pi/printer_data/config/printer.cfg':
        raise fp.FlashError('Service uses a different source/config layout')
    if '-l' not in args or args[args.index('-l')+1:args.index('-l')+2]!=['/home/pi/printer_data/logs/klippy.log']:
        raise fp.FlashError('Service uses a different log path')
    return args


def stage_plugins(host,candidate,tracked,guard):
    """Retain installed external modules; do not install/update a plugin or dependency."""
    saved=[]
    for sub in ('extras','plugins'):
        directory=host/'klippy'/sub
        if not directory.exists(): continue
        for path in directory.rglob('*'):
            if path.suffix not in ('.py','.so') or '__pycache__' in path.parts: continue
            relative=path.relative_to(host).as_posix()
            if relative in tracked: continue
            guard.check()
            dest=candidate/relative
            if not path.is_file() or path.stat().st_size>4*ap.MIB:
                raise fp.FlashError('External plugin missing/oversized: '+relative)
            if dest.exists() or dest.is_symlink(): raise fp.FlashError('External plugin collides with candidate source: '+relative)
            record={'relative':relative,'sha256':ap.sha256(path,guard)}
            dest.parent.mkdir(parents=True,exist_ok=True)
            if path.is_symlink():
                resolved=path.resolve(strict=True)
                if ap.contained(host,resolved): raise fp.FlashError('External plugin link depends on the host tree being replaced')
                record['link_target']=str(resolved)
                dest.symlink_to(resolved)
            else: shutil.copy2(path,dest)
            saved.append(record)
    verify_plugins(host,saved,guard); verify_plugins(candidate,saved,guard)
    return saved


def verify_plugins(root,records,guard):
    for record in records:
        path=root/record['relative']
        if not ap.contained(root,path) and not record.get('link_target'):
            raise fp.FlashError('Plugin path escaped source tree')
        if ap.sha256(path,guard)!=record['sha256'] or (record.get('link_target') and str(path.resolve())!=record['link_target']):
            raise fp.FlashError('Preserved plugin changed during transaction: '+record['relative'])


def restore_board(txn,reset_old):
    """One restorative download, followed by full backup equality and old identity."""
    txn.guard.check(); txn.inactive()
    r=txn.result
    if r.get('state') not in ('READBACK_VERIFIED','VERIFIED') or r.get('recovery',{}).get('write_attempts',0):
        raise fp.FlashError('Recovery is bounded to one application restoration after a verified update read-back')
    if not r.get('readback_sha256') or not r.get('before_identity'):
        raise fp.FlashError('No verified prior application/dictionary and new read-back for paired recovery')
    before=(txn.state/'flash-before.bin').read_bytes()
    after=(txn.state/'flash-after.bin').read_bytes()
    if fp.sha(before)!=r['backup_sha256'] or fp.sha(after)!=r['readback_sha256'] or len(before)!=r['backup_bytes']:
        raise fp.FlashError('Recovery backup/read-back integrity failure')
    original=r['initial_usb']; dfu=r['dfu_usb']; p=r['profile']
    device=of.match_port(original['port'])
    if any(device.get(k)!=original[k] for k in ('port','vid','pid','serial','manufacturer','product')):
        raise fp.FlashError('Recovery requires the same normal-mode board identity')
    offset=p['address']-0x08000000; end=r['erase_end']-0x08000000
    fp.validate_range(r['dfu_memory_descriptor'],p['address'],end-offset)
    if not offset<end<=len(before) or before[:offset]!=after[:offset] or before[end:]!=after[end:]:
        raise fp.FlashError('Recovery sector boundaries/bootloader preservation failed')
    recovery=r['recovery']={'state':'REQUESTING_DFU','write_attempts':0}
    txn.record()
    recovery['bootloader_request_exit']=of.bootloader_request(txn.runner,of.serial_path(device))
    device=txn.wait(original['port'],lambda d:d['vid']=='0483' and d['pid']=='df11' and d['serial']==dfu['serial'],20)
    target=ap.USBGuard(device).start()
    runner=ap.Runner(of.CombinedGuard(txn.guard,target),txn.runner.emit)
    args=fp.dfu_args(device['port'],device['serial'])
    def run(extra,timeout=120):
        txn.inactive(); return runner.run(args+extra,timeout=timeout)
    try:
        _,listing=run(['--list'],15)
        selected=[s for s in listing.splitlines() if 'alt=0' in s and ('serial="'+device['serial']+'"') in s and ('path="'+device['port']+'"') in s]
        if selected!=[r['dfu_memory_descriptor']]:
            raise fp.FlashError('Recovery DFU map/identity differs from original')
        current=txn.state/'recovery-before.bin'
        run(['--dfuse-address','0x08000000:'+str(len(before)),'--upload',str(current)])
        if current.read_bytes()!=after:
            raise fp.FlashError('Board flash changed since verified update; no restorative write permitted')
        image=txn.state/'recovery-application.bin'
        ap.atomic_write(image,before[offset:end],txn.guard)
        recovery.update(state='WRITE_STARTED',write_attempts=1,image_sha256=fp.sha(before[offset:end]))
        txn.record()
        run(['--dfuse-address','0x%x'%p['address'],'--download',str(image)],180)
        restored=txn.state/'recovery-after.bin'
        run(['--dfuse-address','0x08000000:'+str(len(before)),'--upload',str(restored)])
        if restored.read_bytes()!=before:
            raise fp.FlashError('Restored flash does not equal the entire original backup')
        recovery.update(state='READBACK_VERIFIED',sha256=fp.sha(before)); txn.record()
    finally:
        target.close()
    txn.guard.check(); txn.inactive()
    txn.runner.run(args+['--dfuse-address','0x%x:leave'%p['address']],timeout=15,check=False)
    def normal(d):
        return all(d.get(k)==original[k] for k in ('vid','pid','serial','manufacturer','product')) and bool(of.serial_path(d))
    device=txn.wait(original['port'],normal,45)
    if txn.identify(of.serial_path(device))!=r['before_identity']:
        raise fp.FlashError('Restored firmware live dictionary differs from original')
    reset_old(of.serial_path(device),r['before_identity'])
    device=txn.wait(original['port'],lambda d:normal(d) and d['devnum']!=device['devnum'],45)
    if txn.identify(of.serial_path(device))!=r['before_identity']:
        raise fp.FlashError('Restored firmware dictionary failed reset verification')
    recovery.update(state='VERIFIED',normal_usb=device,reset_boot_verified=True)
    r['state']='PREVIOUS_FIRMWARE_RESTORED'; txn.record()


class Service:
    def __init__(self,runner,guard,state,log=Path('/home/pi/printer_data/logs/klippy.log')):
        self.runner,self.guard,self.state,self.log=runner,guard,Path(state),Path(log)
        self.was_active=False

    def properties(self):
        _,out=self.runner.run(['systemctl','show','klipper.service','--property=ActiveState,SubState,MainPID,NRestarts,ExecStart,User,EnvironmentFiles'],timeout=15)
        return dict(line.split('=',1) for line in out.splitlines() if '=' in line)

    def preflight(self):
        props=self.properties(); active=props.get('ActiveState')
        native_launch(props)
        if active not in ('active','inactive'): raise fp.FlashError('Klipper must be healthy active or inactive, not failed/transitioning')
        self.was_active=active=='active'
        if self.was_active: ap.safe_idle(ap.printer_snapshot('http://127.0.0.1:7125',os.environ.get('AUTOPRINTER_MOONRAKER_KEY'))[0])
        ap.atomic_write(self.state/'SERVICE_BEFORE.json',json.dumps({'was_active':self.was_active}),self.guard)

    def stop(self):
        if self.was_active:
            ap.safe_idle(ap.printer_snapshot('http://127.0.0.1:7125',os.environ.get('AUTOPRINTER_MOONRAKER_KEY'))[0])
            self.stop_for_recovery()

    def stop_for_recovery(self):
        self.runner.run(['systemctl','stop','klipper.service'],timeout=30)
        if self.properties().get('ActiveState')!='inactive': raise fp.FlashError('Klipper failed to stop')

    def resume(self,timeout=60):
        if not self.was_active: return 'PRESERVED_INACTIVE'
        try:
            stat=self.log.stat(); cursor=(stat.st_dev,stat.st_ino,stat.st_size)
        except FileNotFoundError: cursor=None
        self.runner.run(['systemctl','start','klipper.service'],timeout=30)
        deadline=time.monotonic()+timeout; ready_since=None; ready_pid=None
        while time.monotonic()<deadline:
            self.guard.check()
            try:
                with self.log.open('rb') as stream:
                    stat=os.fstat(stream.fileno())
                    start=cursor[2] if cursor and (stat.st_dev,stat.st_ino)==cursor[:2] and stat.st_size>=cursor[2] else 0
                    if stat.st_size-start>8*ap.MIB: raise fp.FlashError('Fresh startup log exceeds verification bound')
                    stream.seek(start); text=stream.read(8*ap.MIB).decode(errors='replace')
                if protocol_error(text): raise fp.ProtocolError('Fresh Klipper startup log reports an MCU protocol mismatch')
            except FileNotFoundError: pass
            props=self.properties()
            if props.get('ActiveState') not in ('active','activating'):
                raise fp.FlashError('Updated Klipper service failed; retained pair requires review')
            try:
                info=ap.url_json('http://127.0.0.1:7125/printer/info',os.environ.get('AUTOPRINTER_MOONRAKER_KEY'))
            except (OSError,ValueError,ap.Refuse): info={}
            pid=props.get('MainPID')
            # Exclude Moonraker's cached response from the old Klipper process.
            same_process=pid not in (None,'0') and str(info.get('process_id'))==pid
            if same_process and protocol_error(str(info.get('state_message',''))):
                raise fp.ProtocolError('Current Klipper process reports an MCU protocol mismatch')
            if same_process and info.get('state')=='ready' and props.get('SubState')=='running':
                if ready_pid!=pid: ready_since=time.monotonic(); ready_pid=pid
                if time.monotonic()-ready_since>=5:
                    ap.safe_idle(ap.printer_snapshot('http://127.0.0.1:7125',os.environ.get('AUTOPRINTER_MOONRAKER_KEY'))[0])
                    return 'READY_IDLE'
            else: ready_since=ready_pid=None
            self.guard.cancelled.wait(.5)
        raise fp.FlashError('Klipper did not become ready in the startup verification window')


def restore_pair(txn,service,host,retained,report,reset_old):
    """Board first; never report host-only restoration as a restored pair."""
    txn.guard.check(); service.stop_for_recovery()
    old=retained if retained is not None else host
    if old.is_symlink() or not old.is_dir() or ap.sha256(old/'klippy/klippy.py',txn.guard)!=report['host_entry_sha256_before']:
        raise fp.FlashError('Retained host integrity check failed before firmware recovery')
    _,commit=txn.runner.run(['git','-C',old,'rev-parse','HEAD'],user='pi',env={'GIT_OPTIONAL_LOCKS':'0'})
    _,dirty=txn.runner.run(['git','-C',old,'status','--porcelain','--untracked-files=no'],user='pi',env={'GIT_OPTIONAL_LOCKS':'0'})
    if commit!=report['host_commit_before'] or dirty:
        raise fp.FlashError('Retained host revision/working tree changed; recovery refused')
    verify_plugins(old,report.get('preserved_plugins',[]),txn.guard)
    restore_board(txn,reset_old)
    if retained is not None:
        if not retained.is_dir() or retained.is_symlink() or host.is_symlink():
            raise fp.FlashError('Retained native host directory unavailable for recovery')
        # Same /home filesystem, preserving both source trees without copying.
        failed=host.with_name('klipper.failed-'+txn.state.name)
        if failed.exists(): raise fp.FlashError('Recovery destination already exists')
        ap.atomic_write(txn.state/'HOST_RECOVERY_INTENT.json',json.dumps({'host':str(host),'retained':str(retained),'failed':str(failed)}),txn.guard)
        txn.guard.check(); host.rename(failed)
        try:
            txn.guard.check(); retained.rename(host)
        except Exception:
            if not txn.guard.cancelled.is_set() and not host.exists(): failed.rename(host)
            raise
        report.update(host_updated=False,failed_host_retained=str(failed))
    report.update(pair_restored=True,host_commit_after=report['host_commit_before'])
    report['service_after_recovery']=service.resume()
    report['status']='PROTOCOL_ERROR_PREVIOUS_PAIR_RESTORED'
