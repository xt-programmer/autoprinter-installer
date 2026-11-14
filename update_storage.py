"""Confined, receipt-based cleanup for the operator-authorized update payload."""
import os
from pathlib import Path
import re
import shutil
import stat
import time
import glob

import autoprinter as ap
import flash_policy as fp


def regular(path,root):
    if root.is_symlink() or path.is_symlink() or not ap.contained(root,path): return None
    relative=path.relative_to(root)
    if any((root/Path(*relative.parts[:i])).is_symlink() for i in range(1,len(relative.parts))): return None
    s=path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_dev!=root.stat().st_dev: return None
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)


def active_configs(root):
    root=Path(root); pending=[root/'printer.cfg']; seen=set()
    while pending:
        path=pending.pop()
        resolved=path.resolve()
        if resolved in seen or not path.is_file() or not ap.contained(root,path): continue
        seen.add(resolved)
        if len(seen)>256 or path.stat().st_size>2*ap.MIB: raise fp.FlashError('Config include graph exceeds cleanup verification bound')
        for line in path.read_text(encoding='utf-8',errors='replace').splitlines():
            match=re.fullmatch(r'\s*\[include\s+([^\]]+)\]\s*(?:[#;].*)?',line,re.I)
            if match:
                pending.extend(Path(p) for p in glob.glob(str(path.parent/match[1].strip())))
    return {str(p) for p in seen}


def cleanup_plan(data,protected=(),now=None,include_gcodes=True):
    data=Path(data); now=time.time() if now is None else now
    protected={str(Path(p).resolve()) for p in protected}
    plan=[]
    gcodes=data/'gcodes'
    if include_gcodes and gcodes.is_dir() and not gcodes.is_symlink():
        for directory,dirs,files in os.walk(gcodes,followlinks=False):
            dirs[:]=[n for n in dirs if not (Path(directory)/n).is_symlink()]
            for name in files:
                path=Path(directory)/name
                if path.suffix.lower() not in ('.gcode','.gco','.g'): continue
                identity=regular(path,gcodes)
                if identity and identity[3]/1e9<=now-48*3600 and str(path.resolve()) not in protected:
                    plan.append({'path':str(path),'root':str(gcodes),'identity':identity,'reason':'gcode_at_least_48_hours_old'})
    config=data/'config'
    if config.is_dir() and not config.is_symlink():
        protected.update(active_configs(config))
        backups=sorted((p for p in config.iterdir() if re.fullmatch(r'printer-20\d{6}_\d{6}\.cfg',p.name) and regular(p,config)),key=lambda p:p.name,reverse=True)
        # printer.cfg and the latest dated SAVE_CONFIG backup are always retained.
        for path in backups[1:]:
            if str(path.resolve()) not in protected:
                plan.append({'path':str(path),'root':str(config),'identity':regular(path,config),'reason':'older_dated_printer_config_backup'})
    return sorted(plan,key=lambda e:(e['reason'].startswith('gcode'),e['identity'][3]))


def apply_cleanup(plan,guard,record,stop_when=None,skip_when=None):
    freed=0
    for entry in plan:
        if stop_when and stop_when(): break
        guard.check(); path=Path(entry['path']); root=Path(entry['root'])
        if skip_when and skip_when(entry): continue
        if entry['reason']=='older_dated_printer_config_backup':
            dated=sorted(p.name for p in root.iterdir() if re.fullmatch(r'printer-20\d{6}_\d{6}\.cfg',p.name) and regular(p,root))
            if str(path.resolve()) in active_configs(root) or not dated or path.name==dated[-1]:
                record(dict(entry,event='SKIPPED_NOW_PROTECTED')); continue
        if regular(path,root)!=tuple(entry['identity']): raise fp.FlashError('Cleanup file changed; refused: '+str(path))
        # Persist intent before deletion; no traversal through a substituted parent.
        record(dict(entry,event='DELETE_INTENT'))
        guard.check()
        if regular(path,root)!=tuple(entry['identity']): raise fp.FlashError('Cleanup target changed before unlink')
        if os.name=='posix' and os.unlink in os.supports_dir_fd:
            fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:
                if not ap.contained(root,Path('/proc/self/fd')/str(fd)): raise fp.FlashError('Cleanup directory moved outside its allowed root')
                s=os.stat(path.name,dir_fd=fd,follow_symlinks=False)
                if (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)!=tuple(entry['identity']): raise fp.FlashError('Cleanup target changed after opening its directory')
                guard.check(); os.unlink(path.name,dir_fd=fd)
            finally: os.close(fd)
        else: path.unlink()
        freed+=entry['identity'][2]
        record(dict(entry,event='DELETED'))
    return freed


def ensure_space(paths,data,guard,runner,record,protected=(),minimum=3*ap.GIB,include_gcodes=True):
    def enough(): return all(shutil.disk_usage(p).free>=minimum for p in paths)
    if enough(): return 0
    runner.run(['apt-get','clean'],timeout=60)
    if enough(): return 0
    plan=cleanup_plan(data,protected,include_gcodes=include_gcodes)
    def low_devices(): return {os.stat(p).st_dev for p in paths if shutil.disk_usage(p).free<minimum}
    freed=apply_cleanup(plan,guard,record,stop_when=enough,skip_when=lambda entry:entry['identity'][0] not in low_devices())
    if not enough(): raise fp.FlashError('Insufficient microSD space after authorized cleanup; need %d MiB free'%(minimum//ap.MIB))
    return freed


def remove_scratch(path,owned_root,guard):
    """Only this invocation's explicitly owned temporary tree; never retention paths."""
    path,owned_root=Path(path),Path(owned_root)
    guard.check()
    if path.is_symlink() or owned_root.is_symlink() or path==owned_root or not ap.contained(owned_root,path):
        raise fp.FlashError('Scratch cleanup escaped the owned transaction workspace')
    if path.exists(): shutil.rmtree(path)
