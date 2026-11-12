"""Octopus USB build/flash policy. No hardware I/O at import time."""
import hashlib
import json
from pathlib import Path
import re
import struct

PROFILES={
    'stm32f446xx':{'processor':'STM32F446','boot_kib':32,'crystal_mhz':12,'flash_symbol':'8000'},
    'stm32f429xx':{'processor':'STM32F429','boot_kib':32,'crystal_mhz':8,'flash_symbol':'8000'},
    'stm32h723xx':{'processor':'STM32H723','boot_kib':128,'crystal_mhz':25,'flash_symbol':'20000'},
}


class FlashError(ValueError): pass


class ProtocolError(FlashError):
    """A positive protocol/dictionary mismatch, not a transport/write failure."""


def profile(chip):
    if chip not in PROFILES: raise FlashError('Only verified Octopus F446/F429/H723 USB profiles are supported')
    return dict(PROFILES[chip],chip=chip,address=0x08000000+PROFILES[chip]['boot_kib']*1024)


def config_seed(chip):
    p=profile(chip)
    return '\n'.join(['CONFIG_LOW_LEVEL_OPTIONS=y','CONFIG_MACH_STM32=y',
                     'CONFIG_MACH_'+p['processor']+'=y','CONFIG_STM32_FLASH_START_'+p['flash_symbol']+'=y',
                     'CONFIG_STM32_CLOCK_REF_%dM=y'%p['crystal_mhz'],'CONFIG_STM32_USB_PA11_PA12=y'])+'\n'


def validate_config(text,chip):
    p=profile(chip); lines=set(text.splitlines())
    required=config_seed(chip).splitlines()+['CONFIG_MCU="'+chip+'"','CONFIG_USBSERIAL=y',
                'CONFIG_FLASH_APPLICATION_ADDRESS=0x%x'%p['address']]
    for line in required:
        if line not in lines: raise FlashError('Build profile mismatch: '+line)
    if 'CONFIG_USB_SERIAL_NUMBER_CHIPID=y' not in lines: raise FlashError('Require hardware-derived USB serial')
    if any(line.startswith('CONFIG_INITIAL_PINS=') and line!='CONFIG_INITIAL_PINS=""' for line in lines):
        raise FlashError('No configured startup GPIO outputs are permitted in this firmware profile')
    return p


def sha(data): return hashlib.sha256(data).hexdigest()


def artifact(directory):
    root=Path(directory)
    data=json.loads((root/'firmware-manifest.json').read_text())
    if data.get('schema')!=1 or not re.fullmatch('[0-9a-f]{40}',data.get('source_commit','')):
        raise FlashError('Firmware artifact must pin its exact Klipper commit')
    p=validate_config((root/'build.config').read_text(),data['chip'])
    for name in ('klipper.bin','klipper.dict','build.config'):
        if (root/name).is_symlink() or sha((root/name).read_bytes())!=data['sha256'][name]:
            raise FlashError('Firmware artifact checksum mismatch: '+name)
    binary=(root/'klipper.bin').read_bytes()
    if not 1024<len(binary)<128*1024: raise FlashError('Unexpected Octopus image size')
    sp,reset=struct.unpack_from('<II',binary)
    if not 0x20000000<=sp<=0x24080000 or not reset&1 or not p['address']<=reset-1<p['address']+len(binary):
        raise FlashError('Invalid application vector table/address')
    dictionary=json.loads((root/'klipper.dict').read_bytes())
    if dictionary['config'].get('MCU')!=p['chip']: raise FlashError('Firmware dictionary MCU mismatch')
    return data,p,dictionary


def flash_layout(line):
    match=re.search(r'@Internal Flash\s*/0x08000000/([^"\r\n]+)',line)
    if not match: raise FlashError('ROM DFU does not expose the expected internal flash map')
    sectors=[]; address=0x08000000
    for group in match[1].split(','):
        m=re.fullmatch(r'\s*(\d+)\*(\d+)([KMB])([a-g])\s*',group)
        if not m: raise FlashError('Unrecognized DfuSe flash map')
        count,size=int(m[1]),int(m[2])*{'B':1,'K':1024,'M':1024**2}[m[3]]
        if count>256 or size<=0: raise FlashError('Invalid flash map size')
        for _ in range(count): sectors.append((address,address+size,m[4])); address+=size
    if address-0x08000000>2*1024**2: raise FlashError('Unexpected STM32 flash size')
    return sectors


def validate_range(line,start,length):
    sectors=flash_layout(line)
    if start not in {s[0] for s in sectors} or length<=0 or start+length>sectors[-1][1]:
        raise FlashError('Image is outside flash or not aligned to an erase-sector boundary')
    if any(mode!='g' for a,b,mode in sectors if b>start and a<start+length):
        raise FlashError('DFU target range is not readable/erasable/writable')
    return sectors[-1][1]-0x08000000


def dfu_args(port,serial):
    if not re.fullmatch(r'\d+-\d+(?:\.\d+)*',port) or not re.fullmatch('[A-Za-z0-9]+',serial):
        raise FlashError('Require exact physical DFU port and alphanumeric serial')
    return ['dfu-util','--device',',0483:df11','--path',port,'--serial',','+serial,'--alt','0']


def host_action(health,flash_state):
    if flash_state=='VERIFIED': return 'UPDATE_TO_VERIFIED_FIRMWARE_SOURCE'
    if health in ('missing','corrupt'): return 'REPAIR_SAME_REVISION_OR_MATCH_MCU_FIRST'
    return 'KEEP_EXISTING_KLIPPER'
