"""Pure planning and guarded file replacement for the two operator-supplied 500 sets.

No MCU/network/service I/O. Version numbers come only from the first three lines.
"""
import hashlib
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parent/'config_sets'
NAMES=('macros500.cfg','settings500.cfg')
PROFILES=('auto','cartographer','manual-z')


class ConfigError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def version(data):
    text=b'\n'.join(data.splitlines()[:3]).decode('utf-8-sig') if isinstance(data,bytes) else '\n'.join(data.splitlines()[:3])
    values=re.findall(r'^\s*#+\s*Version\s*:\s*(\d+(?:\.\d+)*)\s*#*\s*$',text,re.M|re.I)
    if len(values)!=1:
        raise ConfigError('Expected exactly one numeric Version header in the first three lines')
    return values[0]


def compare(left,right):
    a,b=tuple(map(int,left.split('.'))),tuple(map(int,right.split('.')))
    n=max(len(a),len(b)); a+= (0,)*(n-len(a)); b+=(0,)*(n-len(b))
    return (a>b)-(a<b)


def templates(profile,root=None):
    root=Path(root) if root else ROOT
    manifest=json.loads((root/'manifest.json').read_text())
    if manifest.get('schema')!=1 or profile not in ('cartographer','manual-z'):
        raise ConfigError('Unknown config-set schema/profile')
    spec=manifest['profiles'][profile]
    if set(spec['files'])!=set(NAMES): raise ConfigError('Config set must contain exactly the two managed .cfg files')
    result={}
    for name in NAMES:
        path=root/profile/name
        if path.is_symlink() or not path.is_file(): raise ConfigError('Unsafe/missing template '+str(path))
        data=path.read_bytes()
        if digest(data)!=spec['files'][name] or version(data)!=spec['version']:
            raise ConfigError('Template checksum/version mismatch: '+str(path))
        result[name]={'data':data,'sha256':digest(data),'version':spec['version']}
    return result


def graph(config):
    config=Path(config).resolve()
    allowed=(config,config.parent.parent/'mainsail-config')
    result={'sections':{},'files':[],'missing_includes':[]}
    total=0; visits=0
    def walk(path,stack):
        nonlocal total,visits
        resolved=path.resolve()
        if not any(resolved==root or root in resolved.parents for root in allowed):
            raise ConfigError('Config include leaves the supported config/mainsail-config trees: '+str(path))
        if resolved in stack: raise ConfigError('Cyclic config include: '+str(path))
        if not resolved.is_file():
            result['missing_includes'].append(str(path)); return
        visits+=1
        if visits>256 or resolved.stat().st_size>2*1024*1024: raise ConfigError('Config include graph exceeds limits')
        data=resolved.read_bytes(); total+=len(data)
        if total>4*1024*1024: raise ConfigError('Config include graph exceeds 4 MiB')
        result['files'].append(str(path))
        section=None
        for raw in data.decode('utf-8-sig').splitlines():
            line=raw.split('#',1)[0].split(';',1)[0].strip()
            if not line: continue
            header=re.fullmatch(r'\[([^\]]+)\]',line)
            if header:
                section=header[1].strip().lower()
                if section.startswith('include '):
                    pattern=header[1].strip()[8:].strip()
                    if not pattern or Path(pattern).is_absolute(): raise ConfigError('Unsupported absolute/empty include')
                    matches=sorted(path.parent.glob(pattern))
                    if not matches: result['missing_includes'].append(str(path.parent/pattern))
                    for child in matches: walk(child,stack+(resolved,))
                    section=None
                else: result['sections'].setdefault(section,{})
            elif section:
                option=re.match(r'([A-Za-z_][A-Za-z_0-9]*)\s*[:=]\s*(.*)$',line)
                if option: result['sections'][section][option[1].lower()]=option[2].strip()
    walk(config/'printer.cfg',())
    return result


def choose(config,requested='auto',current_cartographer=False):
    if requested not in PROFILES: raise ConfigError('printer_profile must be auto, cartographer or manual-z')
    tree=graph(config); sections=tree['sections']
    endstop=sections.get('stepper_z',{}).get('endstop_pin','').strip()
    carto='cartographer' in sections and 'mcu cartographer' in sections
    if any(name=='scanner' or name.startswith('scanner ') for name in sections):
        raise ConfigError('Legacy scanner configuration needs manual migration')
    detected=None
    if carto and endstop=='probe:z_virtual_endstop': detected='cartographer'
    elif not carto and re.fullmatch(r'[!^~]*(?:[A-Za-z_]\w*:)?(?:P[A-Z]\d+|gpio\d+)',endstop): detected='manual-z'
    elif carto or endstop:
        raise ConfigError('Conflicting/unsupported Z-probe configuration; no automatic profile switch')
    elif current_cartographer and not (Path(config)/'printer.cfg').exists(): detected='cartographer'
    if requested!='auto' and detected and requested!=detected:
        raise ConfigError('Selected profile contradicts the active printer Z configuration')
    selected=detected if requested=='auto' else requested
    if not selected:
        raise ConfigError('Cannot infer printer type; supply --printer-profile cartographer or manual-z after review')
    return selected,{'profile':selected,'selection':'active Z configuration' if endstop else 'explicit selection' if requested!='auto' else 'currently detected named Cartographer',
                     'z_endstop':endstop,'config_files_read':tree['files'],'missing_includes':tree['missing_includes']}


def fingerprint(path):
    path=Path(path)
    if path.is_symlink(): raise ConfigError('Managed config cannot be a symlink: '+str(path))
    if not path.exists(): return None
    if not path.is_file() or path.stat().st_size>2*1024*1024: raise ConfigError('Unsafe/oversized managed config: '+str(path))
    return digest(path.read_bytes())


def directory_is_linked(path):
    path=Path(path).absolute()
    return any(part.is_symlink() for part in (path,*path.parents))


def plan(config,requested='auto',current_cartographer=False):
    config=Path(config)
    if directory_is_linked(config): raise ConfigError('Config directory/ancestors cannot be symlinks')
    selected,evidence=choose(config,requested,current_cartographer)
    supplied=templates(selected)
    result={'profile':selected,'selection':evidence,'files':{},'changes_required':False,'blockers':[]}
    for name,source in supplied.items():
        path=config/name
        old=fingerprint(path)
        item={'before_sha256':old,'target_sha256':source['sha256'],'target_version':source['version']}
        if old is None: item['action']='install-missing'
        else:
            data=path.read_bytes()
            try: old_version=version(data)
            except (ConfigError,UnicodeError) as error:
                item.update(action='blocked-version',reason=str(error)); result['blockers'].append(name+': '+str(error))
            else:
                item['existing_version']=old_version
                relation=compare(old_version,source['version'])
                item['action']='replace-older' if relation<0 else 'keep-newer' if relation>0 else 'keep-identical' if old==source['sha256'] else 'keep-same-version-edits'
        if item['action'] in ('install-missing','replace-older'): result['changes_required']=True
        result['files'][name]=item
    return result


def validate(plan):
    if plan['blockers']: raise ConfigError('Config version review required: '+'; '.join(plan['blockers']))


def apply(config,planned,backup,guard,write,ownership=lambda path:None,before_write=lambda:None):
    """Caller keeps Klipper stopped. Back up all files, then journal atomic writes.

    A pair is not one filesystem transaction. Failure leaves services stopped;
    no automatic restart/rollback is attempted, and originals remain in backup.
    """
    validate(planned)
    config,backup=Path(config),Path(backup)
    sources=templates(planned['profile'])
    guard.check()
    if directory_is_linked(config): raise ConfigError('Config directory changed to a symlink')
    for name,item in planned['files'].items():
        if sources[name]['sha256']!=item['target_sha256']: raise ConfigError('Template changed after planning: '+name)
    # Recheck the chosen machine type and every target before the first mutation.
    choose(config,planned['profile'])
    for name,item in planned['files'].items():
        if fingerprint(config/name)!=item['before_sha256']: raise ConfigError('Config changed after planning: '+name)
    if not planned['changes_required']: return []
    before_write(); guard.check()
    backup.mkdir(parents=True,exist_ok=False,mode=0o700)
    for name,item in planned['files'].items():
        if item['before_sha256'] is not None:
            data=(config/name).read_bytes()
            if digest(data)!=item['before_sha256']: raise ConfigError('Config changed before backup: '+name)
            write(backup/name,data,0o600)
    journal={'plan':planned,'written':[],'status':'BACKED_UP'}
    write(backup/'transaction.json',json.dumps(journal,indent=2),0o600)
    changed=[]
    config.mkdir(parents=True,exist_ok=True)
    for name,item in planned['files'].items():
        if item['action'] not in ('install-missing','replace-older'): continue
        guard.check(); before_write()
        if directory_is_linked(config): raise ConfigError('Config directory changed before replacement')
        if fingerprint(config/name)!=item['before_sha256']: raise ConfigError('Config changed before replacement: '+name)
        write(config/name,sources[name]['data'],0o644)
        ownership(config/name)
        if fingerprint(config/name)!=item['target_sha256']: raise ConfigError('Config verification failed: '+name)
        changed.append(name); journal['written'].append(name); journal['status']='IN_PROGRESS'
        write(backup/'transaction.json',json.dumps(journal,indent=2),0o600)
    journal['status']='CONFIG_FILES_UPDATED_REVIEW_REQUIRED'
    write(backup/'transaction.json',json.dumps(journal,indent=2),0o600)
    return changed
