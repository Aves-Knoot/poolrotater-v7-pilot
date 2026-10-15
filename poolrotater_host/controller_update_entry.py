"""Root timer entry: verify the staged complete code inventory before imports."""
import sys
# -I ignores PYTHONDONTWRITEBYTECODE. Root can write through sealed mode bits,
# so disable caches before importing *any* staged administrative code.
sys.dont_write_bytecode = True
import hashlib
import json
import os
from pathlib import Path
import re
import stat


def protected(path):
    for item in (path,*path.parents):
        st=item.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid!=0 or st.st_mode&0o022:
            raise ValueError('unprotected controller update code')
    return path


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('instance')
    parser.add_argument('--operation-id')
    parser.add_argument('--decision',choices=('review','complete','cancel','rollback'))
    args=parser.parse_args()
    if (os.geteuid()!=0 or not re.fullmatch('[a-z][a-z0-9-]{0,15}',args.instance)
            or bool(args.operation_id)!=bool(args.decision)):
        raise ValueError('explicit controller update instance and recovery identity required')
    instance=args.instance;release=Path(__file__).absolute().parent.parent
    prefix=Path('/opt/poolrotater-v7-pilot')/instance
    if release.parent!=prefix/'releases':raise ValueError('foreign controller update entry')
    job=json.loads(protected(prefix/'controller-update.json').read_bytes())
    candidate=job['after']
    if candidate['release']!=str(release) or candidate['prefix']!=str(prefix):raise ValueError('update code identity changed')
    files={}
    for path in protected(release).rglob('*'):
        protected(path)
        if path.is_dir():continue
        st=path.lstat()
        if not stat.S_ISREG(st.st_mode)or st.st_nlink!=1:raise ValueError('invalid update source type')
        with path.open('rb')as source:files[path.relative_to(release).as_posix()]=hashlib.file_digest(source,'sha256').hexdigest()
    if {'files':files,'links':{}}!=candidate['release_snapshot']:raise ValueError('controller update source changed')
    sys.path.insert(0,str(release))
    from poolrotater_host.runtime_lifecycle import validate_runtime
    runtime=validate_runtime(candidate,prefix)
    python=runtime['environment']/'bin/python'
    if Path(sys.executable).absolute()!=python:
        extra=['--operation-id',args.operation_id,'--decision',args.decision]if args.decision else []
        os.execve(str(python),[str(python),'-I','-B',str(Path(__file__).absolute()),instance,*extra],
                  {'PATH':'/usr/sbin:/usr/bin:/bin','LANG':'C.UTF-8',
                   'PYTHONDONTWRITEBYTECODE':'1','MALLOC_ARENA_MAX':'2'})
    if args.decision:
        from poolrotater_host.updates.recovery import controller
        result=controller(instance,args.operation_id,args.decision)
    else:
        from poolrotater_host.controller_update import step
        result=step(instance)
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':
    try:main()
    except Exception:
        print('controller update held; retain state and inspect the protected queue',file=sys.stderr)
        raise SystemExit(2)
