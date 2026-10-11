#!/usr/bin/env python3
"""Fixture bootstrap: run FIRST using system Python -I -S, before venv imports."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys


def main():
    if os.environ.get('POOLROTATER_PROVISIONING_SANDBOX') != '1' or os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1' or os.geteuid() == 0:
        raise SystemExit('protected runtime checks require the isolated service-user fixture')
    # The OS interpreter and -I -S ignore site/customization, cwd and PYTHONPATH.
    # Validate our bootstrap ancestors before importing the bundled stdlib core.
    release = Path(__file__).absolute().parent
    for path in (Path(__file__).absolute(),release,*release.parents):
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022 or os.access(path,os.W_OK):
            raise SystemExit('bootstrap code/ancestor is unprotected')
    sys.path.insert(0,str(release))
    from poolrotater_host.provisioning import load_provisioned
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--simulate-controller',action='store_true')
    parser.add_argument('--verified-stage',action='store_true',help=argparse.SUPPRESS)
    args = parser.parse_args()
    runtime = load_provisioned(args.root,release=release)
    if args.simulate_controller and not args.verified_stage:
        environment = {key:os.environ[key] for key in ('LANG','POOLROTATER_OFFLINE_SANDBOX','POOLROTATER_PROVISIONING_SANDBOX') if key in os.environ}
        environment.update(PATH=str(runtime['environment'] / 'bin') + ':/usr/bin:/bin',HOME='/tmp',
                           PYTHONDONTWRITEBYTECODE='1',MALLOC_ARENA_MAX='2')
        os.execve(runtime['environment'] / 'bin/python',[str(runtime['environment'] / 'bin/python'),'-I','-B',
                  str(Path(__file__).absolute()),'--root',str(args.root),'--simulate-controller','--verified-stage'],environment)
    if args.simulate_controller:
        from poolrotater_host.testing.runtime import run
        result = run(args.root,release)
    else: result = {'ok':True,'uid':os.geteuid(),'version':runtime['manifest']['version'],'live_ready':False}
    print(json.dumps(result))


if __name__ == '__main__': main()
