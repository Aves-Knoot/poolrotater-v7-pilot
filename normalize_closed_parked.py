"""Root maintenance: normalize only fully stopped, positively closed rows.

No provider calls, ownership clearing, room creation, credential reads or settings
changes. Intended for a controller stopped after quarantining a failed room.
"""
import argparse,json,os,sys
from pathlib import Path


def closed_status_changes(rows):
    """A diagnostic phase cannot override positive closure or hide uncertainty."""
    if not isinstance(rows,dict) or not rows or len(rows)>4:
        raise ValueError('bounded closed room journal required')
    if any(not isinstance(r,dict) or r.get('phase')not in ('parked','stopped','recovery_required')
           or r.get('operation')!='closed' or r.get('room_id')is not None
           or r.get('channel_id')is not None or r.get('ownership_unknown')is not False
           for r in rows.values()):
        raise ValueError('only positively closed rows may be normalized')
    return [alias for alias,row in rows.items()if row['phase']!='stopped']


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
    from poolrotater_host.pilot_installation import administrative_binding,require_no_service_processes,_quiescent
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
    from poolrotater_host.reconciliation import _journal
    from poolrotater_host.provisioning import _write
    import uuid
    with file_lease(prefix/'deployment-admission.lock'),_quiescent(target['root'],uid):
        repeated_target,_,repeated_bound=administrative_binding(args.instance)
        if repeated_target!=target or repeated_bound!=bound:raise ValueError('runtime binding changed')
        # Repeat the process check under the owner inode. A concurrently
        # starting service cannot acquire ownership while this check runs.
        require_no_service_processes(uid)
        configuration=_control(target['root']/'config/host.json',uid,private=True)
        path=target['root']/'state/rolling/ownership.json'
        value=_journal(_control(path,uid,private=True),{r['alias']for r in configuration['rooms']})
        changed=closed_status_changes(value['rooms'])
        if changed:
            # Preserve the original failed-candidate diagnostics before local
            # status normalization. No ID/operation/unknown flag is cleared.
            history=prefix/'closed-status-history';history.mkdir(mode=0o700,exist_ok=True)
            for p in (history,*history.parents):
                st=p.lstat()
                if stat.S_ISLNK(st.st_mode)or st.st_uid!=0 or st.st_mode&0o022:
                    raise ValueError('protected history required')
            _write(history/(str(uuid.uuid4())+'.json'),{'format':1,
                'installation_id':bound['installation_id'],'generation':bound['generation'],
                'reason':'explicit_stopped_positive_closure_status_normalization',
                'before':value,'changed_aliases':changed})
        with _identity(uid),file_lease(target['root']/'state/provider-authority-pacing.lock'):
            if _control(path,uid,private=True)!=value:raise ValueError('ownership changed during review')
            registry=PrivateRegistry(Registry(path))
            for alias in changed:registry.record(alias,phase='stopped',pid=None,error=None)
    print(json.dumps({'ok':True,'closed_status_normalized':changed,'ownership_cleared':False,'provider_requests':False,'services_started':False}))


if __name__=='__main__':
    try:main()
    except Exception:
        print('{"ok":false,"error":"closed_status_normalization_refused"}',file=sys.stderr)
        raise SystemExit(2)
