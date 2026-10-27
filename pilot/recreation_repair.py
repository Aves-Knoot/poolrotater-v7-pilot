#!/usr/bin/python3.12
"""Run the exact verified recreation repair kit against one stopped community instance."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile

EXPECTED_VERSION='v7.3.8'


def failure_code(error):
    # Only deliberately public local repair codes cross the console boundary.
    value=str(error)
    if type(error).__name__ in ('ConfigError','RepairError')and re.fullmatch(r'(repair|report)_[a-z0-9_]{1,72}',value):
        return value
    if isinstance(error,OSError):return 'repair_local_io_failed'
    return 'repair_requires_review'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance',required=True)
    parser.add_argument('--start',action='store_true')
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
            from poolrotater_host.recreation_repair_workflow import run
            result=run(args.instance,kit/'runtime.zip',release['runtime_sha256'],
                       {row['sha256']for row in release['supported_predecessors']},operations,start=args.start)
        result['reports']=str(report);operations.save('result',result)
        print(json.dumps(result,sort_keys=True))
        return 0 if result.get('ok')is True else 2
    except Exception as error:
        result={'ok':False,'phase':'requires_review','failure_code':failure_code(error),
                'failed_operation':operations.operation,'reports':str(report),
                'next_action':'retain_instance_and_reports; do_not_repeat_uncertain_start'}
        operations.save('result',result);print(json.dumps(result,sort_keys=True));return 2


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'ok':False,'phase':'repair_preflight_refused','failure_code':failure_code(error),
                          'rooms_started':False}));raise SystemExit(2)
