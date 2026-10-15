"""Root maintenance: normalize only fully stopped, already-closed parked rows.

No provider calls, ownership clearing, room creation, credential reads or settings
changes. Intended for a controller stopped after quarantining a failed room.
"""
import argparse,json,os,sys
from pathlib import Path


def main():
    if os.geteuid()!=0:raise ValueError('administrator required')
    parser=argparse.ArgumentParser();parser.add_argument('--instance',required=True);parser.add_argument('--release',required=True,type=Path)
    args=parser.parse_args()
    # This is a separately reviewed root tool. Import only the exact installed
    # protected admin entry after verifying its root-owned registration and full
    # release inventory; never take executable modules from operator data.
    prefix=Path('/opt/poolrotater-v7-pilot')/args.instance
    import re,stat,hashlib
    if not re.fullmatch('[a-z][a-z0-9-]{0,15}',args.instance):raise ValueError('instance refused')
    for p in (prefix,*prefix.parents,args.release,*args.release.parents):
        s=p.lstat()
        if stat.S_ISLNK(s.st_mode)or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('protected paths required')
    intent=json.loads((prefix/'pilot-install.json').read_text());record=json.loads((prefix/'installations'/(intent['installation_id']+'.json')).read_text())
    if record['release']!=str(args.release)or args.release.parent!=prefix/'releases':raise ValueError('registered release required')
    actual={}
    for p in args.release.rglob('*'):
        s=p.lstat()
        if stat.S_ISLNK(s.st_mode)or s.st_uid!=0 or s.st_mode&0o022:raise ValueError('unprotected installed code')
        if p.is_file():
            if s.st_nlink!=1:raise ValueError('linked installed code')
            actual[p.relative_to(args.release).as_posix()]=hashlib.file_digest(p.open('rb'),'sha256').hexdigest()
    if record['release_snapshot']!={'files':actual,'links':{}}:raise ValueError('installed code changed')
    sys.path.insert(0,str(args.release))
    from poolrotater_host.pilot_installation import administrative_binding,require_no_service_processes
    from poolrotater_host.pilot_operations import _service_state
    from poolrotater_host.network.operations import _state
    from poolrotater_host.provisioning import _control
    from poolrotater_host.layout import file_lease
    from poolrotater_host.ratings.operations import _identity
    from rolling.state import Registry
    from poolrotater_host.runtime_registry import PrivateRegistry
    target,_,bound=administrative_binding(args.instance);uid=bound['service_uid']
    for value in (_service_state(args.instance),_state(args.instance)):
        if value['MainPID']or value['ActiveState']not in ('inactive','failed'):raise ValueError('all instance services must be stopped')
    require_no_service_processes(uid)
    with _identity(uid),file_lease(target['root']/'state/provider-authority-pacing.lock'):
        path=target['root']/'state/rolling/ownership.json'
        value=_control(path,uid,private=True)
        rows=value['rooms']
        if any(r.get('phase')not in ('parked','stopped')or r.get('operation')!='closed'or r.get('room_id')
               or r.get('ownership_unknown')is not False for r in rows.values()):
            raise ValueError('only positively closed rows may be normalized')
        registry=PrivateRegistry(Registry(path));changed=[]
        for alias,row in rows.items():
            if row['phase']=='parked':
                registry.record(alias,phase='stopped',pid=None,error=None);changed.append(alias)
    print(json.dumps({'ok':True,'closed_status_normalized':changed,'ownership_cleared':False,'provider_requests':False,'services_started':False}))


if __name__=='__main__':
    try:main()
    except Exception:
        print('{"ok":false,"error":"closed_status_normalization_refused"}',file=sys.stderr)
        raise SystemExit(2)
