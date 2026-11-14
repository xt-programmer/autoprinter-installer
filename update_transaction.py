"""Update-only DFU preparation, two bounded image attempts, and saved-image recovery."""
import struct
from pathlib import Path

import autoprinter as ap
import flash_policy as fp
import octopus_flasher as of


class UpdateTransaction(of.Transaction):
    def __init__(self,*args,notify=None,**kwargs):
        super().__init__(*args,**kwargs)
        self.notify=notify or (lambda kind,message:None)
        self.result['update_attempts']=0

    def enter(self,original,attempts=2,dfu_serial=None):
        """A disappearing target is awaited; only the same normal identity is requested."""
        last=None
        for attempt in range(attempts):
            self.guard.check(); self.inactive()
            try:
                try: device=of.match_port(original['port'])
                except fp.FlashError:
                    device=self.wait(original['port'],lambda d:(d['vid'],d['pid'])==('0483','df11') and (not dfu_serial or d['serial']==dfu_serial) or all(d.get(k)==original[k] for k in ('vid','pid','serial','product','manufacturer')),20)
                if (device['vid'],device['pid'])!=('0483','df11'):
                    if any(device.get(k)!=original[k] for k in ('vid','pid','serial','product','manufacturer')):
                        raise fp.FlashError('A different board appeared at the selected USB port')
                    of.bootloader_request(self.runner,of.serial_path(device))
                device=self.wait(original['port'],lambda d:d['vid']=='0483' and d['pid']=='df11' and bool(d['serial']) and (not dfu_serial or d['serial']==dfu_serial),20)
                self.result['dfu_entry_attempts']=self.result.get('dfu_entry_attempts',0)+attempt+1
                return device
            except (fp.FlashError,ap.Refuse,OSError) as error:
                self.guard.check(); last=error
                self.notify('error','DFU entry attempt %d/%d failed: %s'%(attempt+1,attempts,error))
        raise fp.FlashError('DFU entry failed after %d attempts: %s'%(attempts,last))

    def binding(self,device):
        target=ap.USBGuard(device).start()
        runner=ap.Runner(of.CombinedGuard(self.guard,target),self.runner.emit)
        args=fp.dfu_args(device['port'],device['serial'])
        return target,runner,args

    def descriptor(self,runner,args,device):
        _,listing=runner.run(args+['--list'],timeout=15)
        lines=[line for line in listing.splitlines() if 'alt=0' in line and ('serial="'+device['serial']+'"') in line and ('path="'+device['port']+'"') in line]
        if len(lines)!=1: raise fp.FlashError('Internal flash is not uniquely bound to this board')
        return lines[0]

    def prepare(self,device,chip):
        p=fp.profile(chip)
        if not of.eligibility(device,chip).startswith('NORMAL_'): raise fp.FlashError('Existing normal-mode firmware required for rollback baseline')
        self.inactive(); self.guard.check()
        self.result.update(state='IDENTIFYING',profile=p,initial_usb=device); self.record()
        identity=self.identify(of.serial_path(device))
        if identity.get('config',{}).get('MCU')!=chip: raise fp.FlashError('Live STM model contradicts selected profile')
        self.result.update(state='IDENTIFIED',profile=p,initial_usb=device,before_identity=identity)
        self.record()
        self.notify('info','Current MCU firmware: '+str(identity.get('version','unknown')))
        dfu=self.enter(device)
        self.result.update(state='DFU_ENTERED',dfu_usb=dfu); self.record()
        target,runner,args=self.binding(dfu)
        try:
            line=self.descriptor(runner,args,dfu)
            size=fp.validate_range(line,p['address'],1)
            before=self.state/'flash-before.bin'
            runner.run(args+['--dfuse-address','0x08000000:'+str(size),'--upload',before],timeout=120)
            raw=before.read_bytes(); offset=p['address']-0x08000000
            if len(raw)!=size: raise fp.FlashError('Incomplete original flash backup')
            for start,low,high in [(0,0x08000000,p['address']),(offset,p['address'],0x08000000+size)]:
                sp,reset=struct.unpack_from('<II',raw,start)
                if not 0x20000000<=sp<=0x24080000 or not reset&1 or not low<=reset-1<high:
                    raise fp.FlashError('Invalid existing bootloader/application vector; cannot ensure old firmware recovery')
            self.result.update(state='BACKED_UP',dfu_memory_descriptor=line,backup_bytes=size,backup_sha256=fp.sha(raw))
            self.record()
        finally: target.close()
        return dfu

    def program(self,directory):
        if self.result['update_attempts']>=2: raise fp.FlashError('Two total update image attempts already used')
        metadata,p,dictionary=fp.artifact(directory); root=Path(directory)
        if p!=self.result['profile']: raise fp.FlashError('Built firmware profile changed after board identification')
        before=(self.state/'flash-before.bin').read_bytes()
        if fp.sha(before)!=self.result['backup_sha256']: raise fp.FlashError('Original flash backup integrity failure')
        binary=(root/'klipper.bin').read_bytes(); offset=p['address']-0x08000000
        fp.validate_range(self.result['dfu_memory_descriptor'],p['address'],len(binary))
        end=max(b for a,b,mode in fp.flash_layout(self.result['dfu_memory_descriptor']) if b>p['address'] and a<p['address']+len(binary))-0x08000000
        self.result.update(erase_end=end+0x08000000,source_commit=metadata['source_commit'])
        last=None
        while self.result['update_attempts']<2:
            self.guard.check(); self.inactive()
            dfu=self.enter(self.result['initial_usb'],attempts=1,dfu_serial=self.result['dfu_usb']['serial'])
            target,runner,args=self.binding(dfu)
            try:
                line=self.descriptor(runner,args,dfu)
                if fp.flash_layout(line)!=fp.flash_layout(self.result['dfu_memory_descriptor']): raise fp.FlashError('DFU flash map changed')
                current=self.state/('attempt-%d-before.bin'%(self.result['update_attempts']+1))
                runner.run(args+['--dfuse-address','0x08000000:'+str(len(before)),'--upload',current],timeout=120)
                raw=current.read_bytes()
                if len(raw)!=len(before) or raw[:offset]!=before[:offset] or raw[end:]!=before[end:]:
                    raise fp.FlashError('Bootloader/untouched flash differs; refuse update or retry')
                self.result.update(state='WRITE_STARTED',firmware_write_started=True,update_attempts=self.result['update_attempts']+1)
                self.record()
                try:
                    runner.run(args+['--dfuse-address','0x%x'%p['address'],'--download',root/'klipper.bin'],timeout=180)
                    after=self.state/'flash-after.bin'
                    runner.run(args+['--dfuse-address','0x08000000:'+str(len(before)),'--upload',after],timeout=120)
                    raw=after.read_bytes()
                    if len(raw)!=len(before) or raw[:offset]!=before[:offset] or raw[end:]!=before[end:]:
                        raise fp.FlashError('Bootloader/untouched flash failed read-back')
                    if raw[offset:offset+len(binary)]!=binary: raise fp.FlashError('Application read-back differs from built image')
                    self.result.update(state='READBACK_VERIFIED',readback_sha256=fp.sha(raw),bootloader_preserved=True)
                    self.record(); last=None
                except (ap.Refuse,OSError,fp.FlashError) as error:
                    self.guard.check(); last=error
                    self.result.update(state='PROGRAM_FAILED',program_error=str(error)); self.record()
                    self.notify('error','Flash attempt %d/2 failed: %s'%(self.result['update_attempts'],error))
            finally: target.close()
            if last is None: break
        if last is not None: raise fp.FlashError('Two flash attempts failed: '+str(last))
        self.guard.check(); self.inactive()
        self.runner.run(args+['--dfuse-address','0x%x:leave'%p['address']],timeout=15,check=False)
        original=self.result['initial_usb']
        def normal(d):
            return all(d.get(k)==original[k] for k in ('vid','pid','serial','manufacturer','product')) and bool(of.serial_path(d))
        device=self.wait(original['port'],normal,45)
        self.result['observed_normal_usb']=device
        if self.identify(of.serial_path(device))!=dictionary: raise fp.ProtocolError('Running firmware dictionary differs from this build')
        if not self.reset_board: raise fp.FlashError('MCU reset verifier missing')
        self.reset_board(of.serial_path(device))
        device=self.wait(original['port'],lambda d:normal(d) and d['devnum']!=device['devnum'],45)
        if self.identify(of.serial_path(device))!=dictionary: raise fp.ProtocolError('Firmware dictionary changed after reset')
        self.result.update(state='VERIFIED',normal_usb=device,after_identity=dictionary,reset_boot_verified=True)
        self.record(); return self.result
