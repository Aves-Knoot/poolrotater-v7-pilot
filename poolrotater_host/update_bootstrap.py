"""Stable root update launcher; imports no mutable operator or release code.

The normal protected admin entry verifies the complete selected release and its
pinned interpreter. This launcher verifies that entry against the root-owned
registration before exec, so a controller switch needs no timer rewrite.
"""
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
            raise ValueError('update launcher protected path required')
    return path


def read(path):
    protected(path);info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>2*1024**2:
        raise ValueError('update launcher record refused')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('duplicate launcher field')
            value[key]=item
        return value
    return json.loads(path.read_bytes(),object_pairs_hook=unique,
                      parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid launcher JSON')))


ACTIONS=('tick','status','check','download','apply','pause','resume','recover','unit','start','stop','cleanup','configure')


def action_arguments(action,*,operation_id=None,decision=None,updates_stdin=False):
    if action not in ACTIONS or type(updates_stdin)is not bool:raise ValueError('update action refused')
    if updates_stdin!=(action=='configure'):raise ValueError('update configure requires explicit stdin')
    if (operation_id is None)!=(decision is None)or decision is not None and action!='recover':
        raise ValueError('update recovery identity required')
    result=['update-'+action]
    if updates_stdin:result.append('--updates-stdin')
    if decision is not None:
        import uuid
        if str(uuid.UUID(operation_id))!=operation_id or decision not in('review','retry','cancel','complete','rollback','supersede'):
            raise ValueError('update recovery decision refused')
        result+=['--operation-id',operation_id,'--decision',decision]
    return result


def main():
    if os.geteuid()!=0:raise ValueError('update administrator required')
    parser=argparse.ArgumentParser();parser.add_argument('--instance',required=True)
    parser.add_argument('--action',choices=ACTIONS,default='tick')
    parser.add_argument('--operation-id');parser.add_argument('--decision')
    parser.add_argument('--updates-stdin',action='store_true')
    args=parser.parse_args()
    action=action_arguments(args.action,operation_id=args.operation_id,decision=args.decision,updates_stdin=args.updates_stdin)
    if not re.fullmatch('[a-z][a-z0-9-]{0,15}',args.instance):raise ValueError('update instance refused')
    prefix=protected(Path('/opt/poolrotater-v7-pilot')/args.instance)
    if Path(__file__).absolute()!=prefix/'updates-bootstrap.py':raise ValueError('update launcher location refused')
    intent=read(prefix/'pilot-install.json');identity=intent['installation_id']
    import uuid
    if str(uuid.UUID(identity))!=identity:raise ValueError('update installation refused')
    record=read(prefix/'installations'/(identity+'.json'))
    if record['prefix']!=str(prefix) or record['installation_id']!=identity:
        raise ValueError('update registration refused')
    release=protected(Path(record['release']))
    if release.parent!=prefix/'releases':raise ValueError('update release location refused')
    entry=protected(release/'poolrotater_host/pilot_admin_entry.py')
    info=entry.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>131072:
        raise ValueError('update admin entry refused')
    expected=record['release_snapshot']['files']['poolrotater_host/pilot_admin_entry.py']
    if hashlib.sha256(entry.read_bytes()).hexdigest()!=expected:raise ValueError('update admin entry changed')
    command=['/usr/bin/python3.12','-I','-S','-B',str(entry),'--instance',args.instance,*action]
    os.chdir('/tmp')
    os.execve(command[0],command,{'PATH':'/usr/sbin:/usr/bin:/bin','LANG':'C.UTF-8',
                               'PYTHONDONTWRITEBYTECODE':'1','MALLOC_ARENA_MAX':'2'})


if __name__=='__main__':
    try:main()
    except Exception:
        print('{"ok":false,"error":"update_bootstrap_refused"}')
        raise SystemExit(2)
