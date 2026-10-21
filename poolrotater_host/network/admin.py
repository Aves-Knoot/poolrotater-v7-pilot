"""Local hub administration. No remote administrative endpoint or provider access."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import signal
import threading

from ..config import ConfigError
from ..layout import checked_root, file_lease
from .contracts import NetworkError, require, decode, canonical, fields
from .hub_layout import initialize
from .store import Store


def inspect(store):
    with store.transaction() as db:
        hosts=db.execute('SELECT count(*) FROM hosts WHERE revoked=0').fetchone()[0]
        revoked=db.execute('SELECT count(*) FROM hosts WHERE revoked=1').fetchone()[0]
        invitations=db.execute('SELECT count(*) FROM invitations WHERE consumed_host IS NULL AND expires>?',(store.clock(),)).fetchone()[0]
        epoch=db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
    return {'ok':True,'active_hosts':hosts,'revoked_hosts':revoked,'available_invitations':invitations,
            'server_epoch':epoch,'provider_access':False}


def restore(source,destination,*,ratings_policy=None):
    """Build/reset in an unpublished private staging root, then publish atomically.

    Never restore over an existing hub. No recoverable intermediate root retains
    usable authority at the requested destination, even if copying/reset fails.
    """
    source=Path(source).absolute();destination=checked_root(Path(destination))
    require(not os.path.lexists(destination),'fresh_restore_destination_required')
    parent=checked_root(destination.parent)
    require(parent.is_dir() and not stat.S_IMODE(parent.stat().st_mode)&0o077,'private_restore_parent_required')
    for path in (source,*source.parents):require(not path.is_symlink(),'unsafe_hub_backup')
    fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and info.st_nlink==1
                and not stat.S_IMODE(info.st_mode)&0o077 and info.st_size<=256*1024**2,'unsafe_hub_backup')
        require(all(not os.path.lexists(str(source)+suffix) for suffix in ('-wal','-shm','-journal')),'standalone_backup_required')
        stage=Path(tempfile.mkdtemp(prefix='.network-restore-',dir=parent))
        try:
            initialize(stage);target=stage/'network.sqlite3'
            target.touch(mode=0o600)
            with closing(sqlite3.connect(source.as_uri()+'?mode=ro&immutable=1',uri=True)) as incoming,closing(sqlite3.connect(target)) as outgoing:
                require(incoming.execute('PRAGMA quick_check').fetchone()[0]=='ok','backup_verification_failed')
                incoming.backup(outgoing)
            restored=Store(stage)
            with restored.transaction() as db:
                m4=db.execute("SELECT 1 FROM meta WHERE key='ratings_format'").fetchone() is not None
            if m4:
                require(ratings_policy is not None,'latest_ratings_policy_required',409)
                from ..ratings.migration import _private
                from ..layout import atomic_json,read_json
                from ..ratings.policy import Policy
                source_policy=_private(ratings_policy)
                atomic_json(stage/'ratings-policy.json',read_json(source_policy,maximum=512*1024))
                Policy(restored).recover()
            else:require(ratings_policy is None,'unexpected_ratings_policy')
            result=restored.restore_authority_reset()
            with target.open('rb') as stream:os.fsync(stream.fileno())
            require(not os.path.lexists(destination),'fresh_restore_destination_required')
            os.rename(stage,destination)
            directory_fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)
            return {**result,'restored':True,'production_deployed':False}
        finally:
            # Only the exact staging directory created by this invocation.
            if stage.exists():shutil.rmtree(stage)
    except sqlite3.DatabaseError:raise NetworkError('backup_verification_failed') from None
    finally:os.close(fd)


def input_body():return decode(sys.stdin.buffer.read(65537))


def serve(root,cert,key,*,bind='127.0.0.1',port=9444):
    """Explicit staging listener. No unit installation or provider access."""
    from .server import HubServer,tls
    from .contracts import integer
    integer(port,1024,65535)
    for filename,private in ((cert,False),(key,True)):
        path=Path(filename).absolute()
        for ancestor in (path,*path.parents):require(not ancestor.is_symlink(),'unsafe_hub_tls_file')
        info=path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and info.st_nlink==1
                and info.st_size<=65536 and not stat.S_IMODE(info.st_mode)&(0o077 if private else 0o022),'unsafe_hub_tls_file')
    store=Store(root)
    with file_lease(store.root/'hub-server.lock'):
        from ..ratings.hub_migration import require_settled
        require_settled(store)
        stop=threading.Event();server=HubServer((bind,port),store,tls_context=tls(cert,key));server.timeout=.5
        previous={sig:signal.signal(sig,lambda *_:stop.set()) for sig in (signal.SIGTERM,signal.SIGINT)}
        try:
            while not stop.is_set():server.handle_request()
        finally:
            server.server_close()
            for sig,handler in previous.items():signal.signal(sig,handler)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Dedicated staging hub administration; JSON inputs on stdin')
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('action',choices=('initialize','invite','revoke','inspect','backup','restore','serve',
                                       'ratings-prepare','ratings-grant','ratings-inspect','ratings-link-replacement','ratings-grant-seed',
                                       'ratings-policy','ratings-policy-recover','updates-inspect','activity-export','logs-search',
                                       'managed-inspect','managed-request','recovery-inspect','recovery-request'))
    parser.add_argument('--cert',type=Path);parser.add_argument('--key',type=Path)
    parser.add_argument('--bind',default='127.0.0.1');parser.add_argument('--port',type=int,default=9444)
    args=parser.parse_args(argv)
    try:
        if args.action=='serve':
            require(args.cert is not None and args.key is not None,'explicit_hub_tls_required')
            serve(args.root,args.cert,args.key,bind=args.bind,port=args.port)
            result={'ok':True,'hub_stopped':True,'provider_access':False}
        elif args.action=='ratings-prepare':
            from ..ratings.hub_migration import migrate
            result=migrate(args.root)
        elif args.action=='initialize':initialize(args.root);result={'ok':True,'hub_initialized':True,'production_deployed':False}
        elif args.action=='restore':
            value=input_body()
            require(type(value) is dict and set(value) in ({'source'},{'source','ratings_policy'}),'unexpected_fields')
            result=restore(value['source'],args.root,ratings_policy=value.get('ratings_policy'))
        else:
            store=Store(args.root)
            if args.action.startswith('ratings-'):
                if args.action in ('ratings-policy','ratings-policy-recover'):
                    from ..ratings.policy import Policy
                    policy=Policy(store)
                    if args.action=='ratings-policy-recover':result=policy.recover()
                    else:
                        value=input_body()
                        require(type(value) is dict and {'subject','effect'}<=set(value)
                                and set(value)<={'subject','effect','user_id','beatmap_id','host_id'},'unexpected_fields')
                        result=policy.change(**value)
                    sys.stdout.buffer.write(canonical(result)+b'\n');return 0
                from ..ratings.repository import Repository
                repository=Repository(store)
                if args.action=='ratings-inspect':result=repository.diagnostics()
                elif args.action=='ratings-grant-seed':
                    value=input_body();fields(value,('host_id','allow'));result=repository.grant_seed(**value)
                elif args.action=='ratings-link-replacement':
                    value=input_body();fields(value,('host_id','previous_host_id'))
                    result=repository.link_replacement(**value)
                else:
                    value=input_body();fields(value,('host_id','mode'));result=repository.grant(value['host_id'],mode=value['mode'])
            elif args.action=='activity-export':
                from ..activity.publisher import publish_snapshot
                value=input_body();fields(value,('output','owner_account_id'))
                result=publish_snapshot(store,Path(value['output']),owner_account_id=value['owner_account_id'])
            elif args.action=='logs-search':
                from ..diagnostic_logs import HubLogs
                value=input_body()
                require(type(value) is dict and set(value)<={'host_id','room','version','since','until','contains','limit','cursor'},'unexpected_fields')
                result=HubLogs(store).search(**value)
            elif args.action=='updates-inspect':
                from ..updates.telemetry import Reports
                value=input_body();fields(value,('host_id',));result=Reports(store).local(value['host_id'])
            elif args.action in ('managed-inspect','managed-request'):
                from ..managed_host.hub import Repository
                repository=Repository(store);value=input_body()
                if args.action=='managed-inspect':
                    fields(value,('host_id',));result=repository.inspect(value['host_id'])
                else:
                    required={'host_id','alias','action'}
                    optional={'patch','timing','actor','role','expected_revision','expected_room_generation','expires_seconds','cancel_operation_id'}
                    require(type(value) is dict and required<=set(value)<=required|optional,'unexpected_fields')
                    result=repository.queue(**value)
            elif args.action in ('recovery-inspect','recovery-request'):
                from ..rescue.repository import Repository
                repository=Repository(store);value=input_body()
                if args.action=='recovery-inspect':
                    fields(value,('host_id',));result=repository.local(value['host_id'])
                else:
                    fields(value,('host_id','action','expected_job_sha256'))
                    result={'ok':True,'request':repository.queue(**value)}
            elif args.action=='inspect':result=inspect(store)
            elif args.action=='invite':
                value=input_body();fields(value,('binding','label','expires_seconds'))
                result={'ok':True,**store.invite(value['binding'],label=value['label'],expires_seconds=value['expires_seconds'])}
            elif args.action=='revoke':
                value=input_body();fields(value,('host_id',));result=store.revoke(value['host_id'])
            else:
                value=input_body();fields(value,('filename',))
                require(type(value['filename']) is str and Path(value['filename']).name==value['filename']
                        and value['filename'] not in ('','.', '..','network.sqlite3','.poolrotater-network-hub.json'),'invalid_backup_name')
                result=store.backup(store.root/value['filename'])
    except (NetworkError,ConfigError,OSError,ValueError,TypeError,KeyError) as exc:
        result={'ok':False,'error':exc.code if isinstance(exc,NetworkError) else 'local_hub_state_refused'}
    sys.stdout.buffer.write(canonical(result)+b'\n')
    return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
