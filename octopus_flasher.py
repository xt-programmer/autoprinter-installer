"""Exact-target STM32 ROM DFU transaction. Never activates/updates a Klipper host."""
import json,re,struct
from pathlib import Path
import autoprinter as ap
import flash_policy as fp


def match_port(port):
    matches=[d for d in ap.usb_devices() if d['port']==port]
    if len(matches)!=1: raise fp.FlashError('Target USB port absent/ambiguous: '+port)
    return matches[0]


def serial_path(device):
    matches=[p for p in Path('/dev/serial/by-id').glob('*') if p.name.startswith('usb-Klipper_'+device['product']+'_') and '_'+device['serial']+'-' in p.name]
    if len(matches)!=1: raise fp.FlashError('Require exactly one stable Klipper serial path for the selected target')
    tty=matches[0].resolve().name
    parents=(Path('/sys/class/tty')/tty/'device').resolve().parents
    if next((p.name for p in parents if (p/'idVendor').is_file()),None)!=device['port']:
        raise fp.FlashError('TTY serial target is on a different physical USB port')
    return str(matches[0])


def eligibility(device,chip):
    fp.profile(chip)
    if device['vid']=='0483' and device['pid']=='df11' and device['serial']:
        return 'ROM_DFU_READY_PROFILE_MUST_BE_VERIFIED'
    if device['vid']=='1d50' and device['pid']=='614e' and device['manufacturer']=='Klipper' and device['product']==chip and device['serial']:
        return 'NORMAL_KLIPPER_CAN_ATTEMPT_BOOTLOADER_REQUEST'
    raise fp.FlashError('USB target is not the selected Octopus MCU; probe/unknown devices refuse')


class CombinedGuard:
    def __init__(self,parent,target): self.parent,self.target=parent,target; self.cancelled=target.cancelled
    def check(self): self.parent.check(); self.target.check()


def bootloader_request(runner,path):
    # No controlling tty; ignore the expected hangup when USB re-enumerates.
    code='import os,signal,fcntl,termios,struct; signal.signal(signal.SIGHUP,signal.SIG_IGN); fd=os.open('+repr(path)+',os.O_RDONLY|os.O_NOCTTY); fcntl.ioctl(fd,termios.TIOCMBIS,struct.pack("I",termios.TIOCM_DTR)); t=termios.tcgetattr(fd); t[4]=t[5]=termios.B1200; termios.tcsetattr(fd,termios.TCSANOW,t); fcntl.ioctl(fd,termios.TIOCMBIC,struct.pack("I",termios.TIOCM_DTR)); os.close(fd)'
    return runner.run(['python3','-B','-c',code],timeout=10,check=False)[0]


class Transaction:
    def __init__(self,guard,runner,state,inactive,identify,reset_board=None):
        self.guard,self.runner,self.state,self.inactive,self.identify=guard,runner,Path(state),inactive,identify
        self.result={'state':'NOT_ATTEMPTED','host_updated':False,'firmware_write_started':False}
        self.reset_board=reset_board

    def record(self):
        ap.atomic_write(self.state/'flash-result.json',json.dumps(self.result,indent=2),self.guard)

    def wait(self,port,predicate,seconds):
        import time
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            self.guard.check()
            try:
                device=match_port(port)
                if predicate(device): return device
            except fp.FlashError: pass
            self.guard.cancelled.wait(.1)
        raise fp.FlashError('USB target did not return in the required mode; no retry/write will be attempted')

    def execute(self,directory,device,expected_normal_serial=None,allow_request=False,require_recovery=False):
        metadata,p,dictionary=fp.artifact(directory); root=Path(directory)
        mode=eligibility(device,p['chip']); port=device['port']
        self.inactive(); self.guard.check()
        self.result.update(state='ELIGIBLE_NOT_YET_PROVEN',profile=p,source_commit=metadata['source_commit'],initial_usb=device)
        self.record()
        if require_recovery and not mode.startswith('NORMAL_'):
            raise fp.FlashError('Paired update requires normal Klipper identity before DFU; use board-flash for unflashed/initially DFU targets')
        if mode.startswith('NORMAL_'):
            if not allow_request: raise fp.FlashError('Normal-mode transition needs explicit bootloader-request opt-in')
            path=serial_path(device); expected_normal_serial=device['serial']
            self.result['before_identity']=self.identify(path)
            if self.result['before_identity']['config']['MCU']!=p['chip']: raise fp.FlashError('Live MCU identity contradicts build profile')
            self.record(); self.inactive(); self.guard.check()
            # The documented Klipper virtual-serial bootloader request. No flash command is sent here.
            code_result=bootloader_request(self.runner,path)
            self.result['bootloader_request_exit']=code_result
            device=self.wait(port,lambda d:(d['vid'],d['pid'])==('0483','df11') and bool(d['serial']),20)
        self.result.update(state='DFU_ENTERED',dfu_usb=device)
        self.record()
        target=ap.USBGuard(device).start()
        dfu_runner=ap.Runner(CombinedGuard(self.guard,target),self.runner.emit)
        args=fp.dfu_args(port,device['serial'])
        def run(extra,timeout=120):
            self.inactive(); return dfu_runner.run(args+extra,timeout=timeout)
        try:
            _,listing=run(['--list'],15)
            selected=[line for line in listing.splitlines() if 'alt=0' in line and ('serial="'+device['serial']+'"') in line and ('path="'+port+'"') in line]
            if len(selected)!=1: raise fp.FlashError('Internal flash alternate is not uniquely bound to target serial/port')
            size=fp.validate_range(selected[0],p['address'],(root/'klipper.bin').stat().st_size)
            self.result['dfu_memory_descriptor']=selected[0]
            before=self.state/'flash-before.bin'
            run(['--dfuse-address','0x08000000:'+str(size),'--upload',str(before)])
            raw=before.read_bytes()
            if len(raw)!=size: raise fp.FlashError('Incomplete flash backup; refusing write')
            sp,reset=struct.unpack_from('<II',raw)
            if not 0x20000000<=sp<=0x24080000 or not reset&1 or not 0x08000000<=reset-1<p['address']:
                raise fp.FlashError('No valid existing bootloader vector; offset profile cannot make a blank MCU cold-boot')
            if require_recovery:
                offset=p['address']-0x08000000
                sp,reset=struct.unpack_from('<II',raw,offset)
                if not 0x20000000<=sp<=0x24080000 or not reset&1 or not p['address']<=reset-1<0x08000000+size:
                    raise fp.FlashError('No valid prior application at the selected offset; cannot promise paired recovery')
            self.result.update(state='BACKED_UP',backup_sha256=fp.sha(raw),backup_bytes=size)
            self.record(); self.inactive(); target.check(); self.guard.check()
            self.result.update(state='WRITE_STARTED',firmware_write_started=True)
            self.record()
            run(['--dfuse-address','0x%x'%p['address'],'--download',str(root/'klipper.bin')],180)
            after=self.state/'flash-after.bin'
            run(['--dfuse-address','0x08000000:'+str(size),'--upload',str(after)])
            actual=after.read_bytes(); binary=(root/'klipper.bin').read_bytes(); offset=p['address']-0x08000000
            if len(actual)!=size or actual[offset:offset+len(binary)]!=binary: raise fp.FlashError('Firmware read-back verification failed')
            sectors=fp.flash_layout(selected[0]); end=max(b for a,b,mode in sectors if b>p['address'] and a<p['address']+len(binary))-0x08000000
            if actual[:offset]!=raw[:offset] or actual[end:]!=raw[end:]: raise fp.FlashError('Bootloader or flash outside application erase range changed')
            self.result.update(state='READBACK_VERIFIED',readback_sha256=fp.sha(actual),bootloader_preserved=True,erase_end=end+0x08000000)
            self.record()
        finally: target.close()
        self.guard.check(); self.inactive()
        # DfuSe command mode: set application address and leave, without downloading again.
        leave_code,_=self.runner.run(args+['--dfuse-address','0x%x:leave'%p['address']],timeout=15,check=False)
        normal=self.wait(port,lambda d:d['vid']=='1d50' and d['pid']=='614e' and d['manufacturer']=='Klipper' and d['product']==p['chip'] and (not expected_normal_serial or d['serial']==expected_normal_serial) and bool(serial_path(d)),45)
        self.result['observed_normal_usb']=normal
        identity=self.identify(serial_path(normal))
        if identity!=dictionary: raise fp.ProtocolError('Running firmware dictionary differs from the built image')
        if not self.reset_board: raise fp.FlashError('Require verified MCU reset/boot persistence before permitting a host update')
        self.reset_board(serial_path(normal))
        normal=self.wait(port,lambda d:d['vid']=='1d50' and d['pid']=='614e' and d['manufacturer']=='Klipper' and d['product']==p['chip'] and d['serial']==normal['serial'] and d['devnum']!=normal['devnum'] and bool(serial_path(d)),45)
        identity=self.identify(serial_path(normal))
        if identity!=dictionary: raise fp.ProtocolError('Firmware did not survive an MCU reset with the selected bootloader offset')
        self.result['reset_boot_verified']=True
        self.result.update(state='VERIFIED',normal_usb=normal,after_identity=identity,leave_exit=leave_code)
        self.record(); return self.result
