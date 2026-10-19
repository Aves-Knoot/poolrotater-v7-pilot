"""Protected standalone entry for a checksum-authorized stopped repair."""
import sys
sys.dont_write_bytecode=True
import argparse
import json
import os
from pathlib import Path
import re
import stat


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance',required=True)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--sha256',required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--start',action='store_true')
    parser.add_argument('--resume-agents',action='store_true')
    args=parser.parse_args()
    if (os.geteuid()!=0 or not re.fullmatch('[a-z][a-z0-9-]{0,15}',args.instance)
            or args.instance=='owner' or not re.fullmatch('[a-f0-9]{64}',args.sha256)):
        raise ValueError('repair_instance_refused')
    release=Path(__file__).absolute().parent.parent
    prefix=Path('/opt/poolrotater-v7-pilot')/args.instance
    # This entry is staged by the pinned launcher; candidate custody/inventory
    # is checked before importing any code or invoking its interpreter.
    def checked(path):
        for p in (path,*path.parents):
            s=p.lstat()
            if stat.S_ISLNK(s.st_mode)or s.st_uid!=0 or s.st_mode&0o022:
                raise ValueError('repair_code_refused')
        return path
    intent=json.loads(checked(prefix/'pilot-install.json').read_bytes())
    record=json.loads(checked(prefix/'candidates'/intent['installation_id']/(args.sha256+'.json')).read_bytes())
    if record['release']!=str(release) or record['bundle_sha256']!=args.sha256:
        raise ValueError('repair_candidate_binding_changed')
    import hashlib
    files={}
    for p in release.rglob('*'):
        checked(p)
        if p.is_dir():continue
        s=p.lstat()
        if not stat.S_ISREG(s.st_mode)or s.st_nlink!=1:raise ValueError('repair_code_refused')
        files[p.relative_to(release).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
    if record['release_snapshot']!={'files':files,'links':{}}:raise ValueError('repair_code_refused')
    sys.path.insert(0,str(release))
    from poolrotater_host.runtime_lifecycle import validate_runtime
    runtime=validate_runtime(record,prefix);python=runtime['environment']/'bin/python'
    if Path(sys.executable).absolute()!=python:
        os.execve(str(python),[str(python),'-I','-B',str(Path(__file__).absolute()),*sys.argv[1:]],
            {'PATH':'/usr/sbin:/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','MALLOC_ARENA_MAX':'2'})
    from poolrotater_host import stopped_update_repair as repair
    from poolrotater_host.provisioning import _write,protected
    report=protected(args.report)
    try:
        result=repair.run(args.instance,record,args.bundle,start=args.start,
                          resume_agents=args.resume_agents,report=report)
        result['reports']=str(report);_write(report/'result.json',result,private=True)
        print(json.dumps(result,sort_keys=True));return 0
    except Exception as error:
        from poolrotater_host.service_diagnostics import failure_details
        from poolrotater_host.updates.contracts import UpdateError
        code=getattr(error,'code',None)
        if isinstance(error,UpdateError) and re.fullmatch('repair_[a-z_]{1,80}',str(error)):
            code=str(error)
        if not (isinstance(code,str) and re.fullmatch('repair_[a-z_]{1,80}',code)):
            code='repair_operation_requires_review'
        result={'ok':False,'phase':'requires_review','failure_code':code,
            'native_failure':failure_details(error)['failure_code'],'reports':str(report),
            'next_action':'retain_state_and_reports; do_not_repeat_start'}
        _write(report/'result.json',result,private=True);print(json.dumps(result,sort_keys=True));return 2


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print('{"ok":false,"failure_code":"repair_protected_entry_refused","next_action":"retain_state; do_not_repeat_start"}')
        raise SystemExit(2)
