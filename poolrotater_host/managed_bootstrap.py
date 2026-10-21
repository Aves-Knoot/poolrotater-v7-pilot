"""Fixed root entry for managed-host reconciliation; no remote argv or shell."""
import sys
sys.dont_write_bytecode=True
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat


def protected(path):
    for item in (path,*path.parents):
        info=item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('unprotected_managed_code')
    return path


def read(path):
    protected(path);info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>2*1024**2:
        raise ValueError('invalid_managed_registration')
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise ValueError('duplicate_managed_field')
            result[key]=value
        return result
    return json.loads(path.read_bytes(),object_pairs_hook=pairs,
                      parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--instance',required=True)
    args=parser.parse_args()
    if os.geteuid()!=0 or not re.fullmatch('[a-z][a-z0-9-]{0,15}',args.instance):
        raise ValueError('managed_administrator_required')
    prefix=protected(Path('/opt/poolrotater-v7-pilot')/args.instance)
    entry=protected(Path(__file__).absolute())
    if entry!=prefix/'managed-bootstrap.py':raise ValueError('managed_entry_changed')
    config=read(prefix/'managed/configuration.json')
    if (set(config)!={'format','installation_id','generation','service_uid','launcher_sha256'}
            or type(config['format']) is not int or config['format']!=1
            or hashlib.sha256(entry.read_bytes()).hexdigest()!=config['launcher_sha256']):
        raise ValueError('managed_launcher_changed')
    installation=config['installation_id']
    if type(installation) is not str or not re.fullmatch('[a-f0-9-]{36}',installation):raise ValueError('managed_binding_changed')
    record=read(prefix/'installations'/(installation+'.json'))
    if (any(record.get(k)!=config[k] for k in ('installation_id','generation','service_uid'))
            or record.get('prefix')!=str(prefix) or record.get('execution') not in ('pilot-live','pilot-candidate')):
        raise ValueError('managed_binding_changed')
    release=protected(Path(record['release']))
    if release.parent!=prefix/'releases':raise ValueError('managed_release_changed')
    files={}
    for path in release.rglob('*'):
        protected(path);info=path.lstat()
        if stat.S_ISDIR(info.st_mode):continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('managed_code_changed')
        with path.open('rb') as stream:files[path.relative_to(release).as_posix()]=hashlib.file_digest(stream,'sha256').hexdigest()
    if record['release_snapshot']!={'files':files,'links':{}}:raise ValueError('managed_code_changed')
    sys.path.insert(0,str(release))
    from poolrotater_host.runtime_lifecycle import validate_runtime
    runtime=validate_runtime(record,prefix);python=runtime['environment']/'bin/python'
    if Path(sys.executable).absolute()!=python:
        command=[str(python),'-I','-B',str(entry),'--instance',args.instance]
        os.chdir('/tmp')
        os.execve(command[0],command,{'PATH':'/usr/sbin:/usr/bin:/bin','LANG':'C.UTF-8',
                                    'PYTHONDONTWRITEBYTECODE':'1','MALLOC_ARENA_MAX':'2'})
    from poolrotater_host.managed_host.operations import tick
    print(json.dumps(tick(args.instance),sort_keys=True))


if __name__=='__main__':
    try:main()
    except Exception:
        print('{"ok":false,"phase":"requires-review","error":"managed_bootstrap_refused"}')
        raise SystemExit(2)
