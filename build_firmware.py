#!/usr/bin/env python3
"""Build an isolated source checkout using reviewed menuconfig-equivalent values."""
import argparse,json,os,shutil,subprocess
from pathlib import Path
import flash_policy as fp


def build(source,chip,output):
    source,output=Path(source).resolve(),Path(output).resolve()
    p=fp.profile(chip)
    def run(*args): subprocess.run(args,cwd=source,check=True,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',LC_ALL='C'))
    run('make','clean')
    (source/'.config').write_text(fp.config_seed(chip))
    run('make','olddefconfig')
    fp.validate_config((source/'.config').read_text(),chip)
    run('make','-j2')
    output.mkdir(parents=True,exist_ok=False)
    for src,dest in [('out/klipper.bin','klipper.bin'),('out/klipper.dict','klipper.dict'),('.config','build.config')]:
        shutil.copyfile(source/src,output/dest)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
    data={'schema':1,'chip':chip,'profile':p,'source_commit':commit,
          'compiler':subprocess.check_output(['arm-none-eabi-gcc','--version'],text=True).splitlines()[0],
          'sha256':{name:fp.sha((output/name).read_bytes()) for name in ['klipper.bin','klipper.dict','build.config']}}
    (output/'firmware-manifest.json').write_text(json.dumps(data,indent=2))
    fp.artifact(output)
    return data


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True); parser.add_argument('--chip',choices=fp.PROFILES,required=True)
    parser.add_argument('--output',type=Path,required=True); args=parser.parse_args()
    print(json.dumps(build(args.source,args.chip,args.output),indent=2))
