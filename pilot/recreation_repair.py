#!/usr/bin/python3.12
"""Run the exact verified recreation repair kit against one stopped community instance."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import traceback

EXPECTED_VERSION='v7.3.8'


def failure_code(error):
    # Only deliberately public local repair codes cross the console boundary.
    value=str(error)
    if type(error).__name__ in ('ConfigError','RepairError')and re.fullmatch(r'(repair|report)_[a-z0-9_]{1,72}',value):
        return value
    if isinstance(error,OSError):return 'repair_local_io_failed'
    if (type(error).__name__=='ConfigError' and str(error)==
            'install the hash-pinned optional catalog verification dependencies'):
        return 'repair_registered_dependencies_unavailable'
    return 'repair_requires_review'


def failure_detail(error):
    """Static code locations only; never exception messages, locals or responses."""
    result=[];seen=set()
    while error is not None and id(error) not in seen and len(result)<4:
        seen.add(id(error));frames=[]
        for item in traceback.extract_tb(error.__traceback__):
            path=Path(item.filename)
            if (('poolrotater_host' in path.parts or 'pilot' in path.parts
                    or path.name in ('recreation_repair.py','repair_and_start.py'))
                    and re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,95}\.py',path.name)
                    and re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,95}',item.name)):
                frames.append({'file':path.name,'function':item.name,'line':item.lineno})
        allowed={'ConfigError','RepairError','AuthError','NetworkError','PackageNotFoundError',
                 'ModuleNotFoundError','OSError','FileNotFoundError','TimeoutError',
                 'TypeError','ValueError','RuntimeError'}
        kind=type(error).__name__
        result.append({'type':kind if kind in allowed else 'Exception',
                       'failure_code':failure_code(error),'frames':frames[-8:]})
        error=error.__cause__ or error.__context__
    return result


def registered_environment(instance, release, primitives):
    """Resolve the interpreter only after full installed code/environment checks."""
    from poolrotater_host.pilot_installation import administrative_binding
    _,_,record=administrative_binding(instance)
    accepted={release['runtime_sha256'],
              *(row['sha256'] for row in release['supported_predecessors'])}
    primitives.require(record['execution']=='pilot-live' and record['bundle_sha256'] in accepted,
                       'repair_predecessor_refused')
    environment=Path(record['environment'])
    return environment/'bin/python',environment


def interpreter_matches(python, environment):
    return (Path(sys.executable).absolute()==python and Path(sys.prefix).absolute()==environment
            and sys.flags.isolated and not sys.flags.no_site and sys.dont_write_bytecode)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance',required=True)
    parser.add_argument('--start',action='store_true')
    parser.add_argument('--verified-environment',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if os.geteuid()!=0 or args.instance=='owner'or re.fullmatch('[a-z][a-z0-9-]{0,15}',args.instance)is None:
        raise ValueError('repair_administrator_and_community_instance_required')
    os.umask(0o077)
    os.nice(max(0,19-os.getpriority(os.PRIO_PROCESS,0)))
    kit=Path(__file__).absolute().parent
    # Outer launcher verifies the entire ZIP before staging this root-only kit.
    # Reuse the reviewed archive/custody/report primitives, not its old workflow.
    sys.path.insert(0,str(kit))
    import repair_and_start as primitives
    release=primitives.verify_kit(kit,expected_version=EXPECTED_VERSION)
    if not args.verified_environment:
        # The system -I -S stage can inspect the archive and sealed registration,
        # but deliberately cannot import installed optional dependencies. Exit
        # its temporary SDK before exec so it leaves no abandoned extraction.
        with tempfile.TemporaryDirectory(prefix='prv7-recreation-preflight-')as directory:
            source=primitives.unpack_runtime(kit,Path(directory));sys.path.insert(0,str(source))
            from poolrotater_host.runtime_package import inspect
            manifest=inspect(kit/'runtime.zip',release['runtime_sha256'],execution='pilot-live')
            primitives.require(manifest['version']==EXPECTED_VERSION,'repair_release_refused')
            python,_=registered_environment(args.instance,release,primitives)
        command=[str(python),'-I','-B',str(Path(__file__).absolute()),
                 '--verified-environment','--instance',args.instance]
        if args.start:command.append('--start')
        os.execve(command[0],command,{'PATH':'/usr/sbin:/usr/bin:/bin','LANG':'C.UTF-8',
            'PYTHONDONTWRITEBYTECODE':'1','MALLOC_ARENA_MAX':'2'})
    report=primitives.private_directory(primitives.private_directory(primitives.REPORT_BASE)/args.instance)
    import uuid
    report=primitives.private_directory(report/str(uuid.uuid4()))
    operations=primitives.Operations(args.instance,report)
    try:
        with tempfile.TemporaryDirectory(prefix='prv7-recreation-sdk-')as directory:
            source=primitives.unpack_runtime(kit,Path(directory));sys.path.insert(0,str(source))
            from poolrotater_host.runtime_package import inspect
            manifest=inspect(kit/'runtime.zip',release['runtime_sha256'],execution='pilot-live')
            primitives.require(manifest['version']==EXPECTED_VERSION,'repair_release_refused')
            python,environment=registered_environment(args.instance,release,primitives)
            primitives.require(interpreter_matches(python,environment),
                               'repair_registered_interpreter_required')
            from poolrotater_host.recreation_repair_workflow import run
            result=run(args.instance,kit/'runtime.zip',release['runtime_sha256'],
                       {row['sha256']for row in release['supported_predecessors']},operations,start=args.start)
        result['reports']=str(report);operations.save('result',result)
        print(json.dumps(result,sort_keys=True))
        return 0 if result.get('ok')is True else 2
    except Exception as error:
        result={'ok':False,'phase':'requires_review','failure_code':failure_code(error),
                'failed_operation':operations.operation,'reports':str(report),
                'failure_detail':failure_detail(error),
                'next_action':'retain_instance_and_reports; do_not_repeat_uncertain_start'}
        operations.save('result',result);print(json.dumps(result,sort_keys=True));return 2


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'ok':False,'phase':'repair_preflight_refused','failure_code':failure_code(error),
                          'rooms_started':False}));raise SystemExit(2)
