"""Dedicated installed hub assets and explicit private recovery packages.

Preparation writes only a new caller-owned directory. Installation is a later,
explicit administrator operation: no accounts, services or listener are created
by this module. The server and ratings ledger remain the existing M3/M4 code.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from ..layout import atomic_json, checked_root, file_lease, read_json
from .contracts import require, fields, integer, canonical
from .hub_layout import check

PREFIX = Path('/opt/poolrotater-v7-hub')
STATE = Path('/var/lib/poolrotater-v7-hub')
USER = 'poolrotater-hub'
UNIT = 'poolrotater-v7-hub.service'


def settings(value):
    fields(value, ('format', 'bind', 'port'))
    require(type(value['format']) is int and value['format'] == 1, 'hub_configuration_refused')
    require(type(value['bind']) is str and value['bind'] in ('127.0.0.1', '0.0.0.0'), 'hub_bind_refused')
    integer(value['port'], 1024, 65535)
    return value


def unit_text(*, prefix=PREFIX, state=STATE, user=USER):
    # Fixed words / paths only: none can inject unit directives or specifiers.
    require(re.fullmatch('[a-z][a-z0-9-]{0,31}', user) is not None, 'hub_identity_refused')
    for path in (prefix, state):
        require(type(path) is Path or isinstance(path, Path), 'hub_path_refused')
        require(path.is_absolute() and re.fullmatch('/[A-Za-z0-9_./-]+', str(path)) is not None
                and '..' not in path.parts, 'hub_path_refused')
    return f'''[Unit]
Description=PoolRotater central registration and shared ratings hub
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={state}
ExecStart=/usr/bin/python3.12 -I -S {prefix}/bootstrap.py --root {state} serve
Restart=on-failure
RestartSec=30
KillMode=control-group
KillSignal=SIGTERM
SendSIGKILL=yes
TimeoutStopSec=20
UMask=0077
Nice=15
CPUQuota=50%
MemoryMax=512M
MemorySwapMax=128M
TasksMax=24
LimitCORE=0
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ReadWritePaths={state}
RestrictAddressFamilies=AF_INET AF_UNIX
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
Environment=LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2
StandardOutput=journal
StandardError=journal
SyslogIdentifier=poolrotater-v7-hub

[Install]
WantedBy=multi-user.target
'''


BOOTSTRAP = '''"""Protected installed entry: check inventory before importing hub code."""
import hashlib, json, os, pathlib, stat, sys
sys.dont_write_bytecode=True
base=pathlib.Path(__file__).absolute().parent
try:
    for p in (base,*base.parents):
        s=p.lstat()
        if p.is_symlink() or not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or stat.S_IMODE(s.st_mode)&0o022:
            raise ValueError('unsafe directory')
    receipt=base/'inventory.json'
    s=receipt.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_nlink!=1 or stat.S_IMODE(s.st_mode)&0o022 or s.st_size>1048576:
        raise ValueError('unsafe receipt')
    records=json.loads(receipt.read_bytes())
    if type(records) is not dict or set(records)!={'format','files'} or type(records['format']) is not int or records['format']!=1 or type(records['files']) is not dict:
        raise ValueError('invalid receipt')
    actual=set()
    for p in base.rglob('*'):
        s=p.lstat()
        if p.is_symlink() or s.st_uid!=0 or stat.S_IMODE(s.st_mode)&0o022:
            raise ValueError('unsafe code')
        if stat.S_ISDIR(s.st_mode):continue
        if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or s.st_size>1048576:raise ValueError('unsafe code')
        name=p.relative_to(base).as_posix()
        if name=='inventory.json':continue
        actual.add(name)
        if name not in records['files'] or hashlib.sha256(p.read_bytes()).hexdigest()!=records['files'][name]:
            raise ValueError('code changed')
    if actual!=set(records['files']):raise ValueError('inventory mismatch')
except Exception:
    print('{"ok":false,"error":"protected_hub_runtime_refused"}')
    raise SystemExit(1)
sys.path.insert(0,str(base))
from poolrotater_host.network.hub_service import main
raise SystemExit(main())
'''


def prepare(destination, *, bind='127.0.0.1', port=9444):
    """Freeze a standalone stdlib hub kit into a fresh, private staging folder."""
    config=settings({'format':1,'bind':bind,'port':port})
    destination=checked_root(Path(destination))
    require(not os.path.lexists(destination), 'fresh_hub_kit_required')
    destination.mkdir(mode=0o700)
    try:
        runtime=destination/'runtime';runtime.mkdir(mode=0o755)
        # Freeze only our package; no bot catalogs, tokens, dependencies, history,
        # research pipeline or source-checkout fixtures enter this hub runtime.
        source=Path(__file__).resolve().parents[1]
        for path in sorted(source.rglob('*.py')):
            require(not path.is_symlink() and path.is_file(), 'hub_source_refused')
            target=runtime/'poolrotater_host'/path.relative_to(source)
            target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
            target.write_bytes(path.read_bytes());target.chmod(0o644)
        # Ratings wire provenance deliberately reuses the existing vote writer's
        # closed contract. These two stdlib files are the only bot dependency.
        for name in ('__init__.py','rating_outbox.py'):
            target=runtime/'bot'/name;target.parent.mkdir(mode=0o755,exist_ok=True)
            target.write_bytes((source.parent/'bot'/name).read_bytes());target.chmod(0o644)
        bootstrap=runtime/'bootstrap.py';bootstrap.write_text(BOOTSTRAP);bootstrap.chmod(0o644)
        inventory={p.relative_to(runtime).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(runtime.rglob('*')) if p.is_file()}
        atomic_json(runtime/'inventory.json',{'format':1,'files':inventory})
        (runtime/'inventory.json').chmod(0o644)
        atomic_json(destination/'hub.json',config)
        unit=destination/UNIT;unit.write_text(unit_text());unit.chmod(0o600)
        return {'ok':True,'runtime_files':len(inventory),'service_installed':False,
                'hub_started':False,'provider_access':False,
                'inventory_sha256':hashlib.sha256((runtime/'inventory.json').read_bytes()).hexdigest()}
    except BaseException:
        shutil.rmtree(destination);raise


def install_assets(kit, *, expected_inventory_sha256, cert, key,
                   prefix=PREFIX, state=STATE, user=USER):
    """Explicit root-only fresh installation; no user/unit/service creation.

    The reviewed inventory digest must be supplied out of band. A dedicated
    unprivileged account must already exist. Paths are fresh and root-controlled;
    failure removes only the exact fresh directories created by this invocation.
    """
    import pwd
    require(os.geteuid()==0,'hub_administrator_required')
    require(re.fullmatch('[a-f0-9]{64}',expected_inventory_sha256 or '') is not None,'hub_inventory_digest_required')
    unit_text(prefix=prefix,state=state,user=user)
    account=pwd.getpwnam(user)
    require(account.pw_uid>0 and account.pw_gid>0,'hub_identity_refused')
    kit=checked_root(Path(kit));runtime=kit/'runtime'
    require(not runtime.is_symlink() and runtime.is_dir(),'hub_kit_refused')
    for path in (prefix,state):
        checked_root(path);parent=checked_root(path.parent)
        require(parent.is_dir() and parent.stat().st_uid==0 and not stat.S_IMODE(parent.stat().st_mode)&0o022
                and not os.path.lexists(path),'fresh_hub_installation_required')
    manifest_path=runtime/'inventory.json'
    manifest_info=manifest_path.lstat()
    require(stat.S_ISREG(manifest_info.st_mode) and manifest_info.st_nlink==1
            and manifest_info.st_size<=512*1024 and not manifest_path.is_symlink(),'hub_kit_refused')
    require(hashlib.sha256(manifest_path.read_bytes()).hexdigest()==expected_inventory_sha256,'hub_inventory_identity_mismatch')
    from .contracts import decode
    inventory=decode(manifest_path.read_bytes(),maximum=512*1024)
    fields(inventory,('format','files'))
    require(type(inventory['format']) is int and inventory['format']==1 and type(inventory['files']) is dict,'hub_kit_refused')
    actual={p.relative_to(runtime).as_posix() for p in runtime.rglob('*') if not p.is_dir()}
    require(actual=={'inventory.json',*inventory['files']} and 'bootstrap.py' in inventory['files'],'hub_kit_refused')
    require(inventory['files']['bootstrap.py']==hashlib.sha256(BOOTSTRAP.encode()).hexdigest(),'hub_bootstrap_refused')
    for name,digest in inventory['files'].items():
        path=runtime/name
        require(name in ('bootstrap.py','bot/__init__.py','bot/rating_outbox.py')
                or (name.startswith('poolrotater_host/') and name.endswith('.py')),'hub_kit_refused')
        require(Path(name).as_posix()==name and '..' not in Path(name).parts and not Path(name).is_absolute(),'hub_kit_refused')
        for ancestor in (path,*path.parents):require(not ancestor.is_symlink(),'hub_kit_refused')
        info=path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=1024*1024
                and re.fullmatch('[a-f0-9]{64}',digest or '') is not None
                and hashlib.sha256(path.read_bytes()).hexdigest()==digest,'hub_kit_refused')
    config=settings(read_json(kit/'hub.json'))
    # Explicit private TLS input is checked before creating any destination.
    cert=Path(cert).absolute();key=Path(key).absolute()
    for path,private in ((cert,False),(key,True)):
        for ancestor in (path,*path.parents):require(not ancestor.is_symlink(),'unsafe_hub_tls_file')
        info=path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==0 and info.st_size<=65536
                and not stat.S_IMODE(info.st_mode)&(0o077 if private else 0o022),'unsafe_hub_tls_file')
    from .server import tls
    tls(cert,key)
    created=[]
    try:
        prefix.mkdir(mode=0o755);created.append(prefix)
        for name in (*inventory['files'],'inventory.json'):
            target=prefix/name;target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
            target.write_bytes((runtime/name).read_bytes());target.chmod(0o644)
        state.mkdir(mode=0o700);created.append(state)
        from .hub_layout import initialize
        from ..ratings.hub_migration import migrate
        initialize(state);migrate(state);atomic_json(state/'hub.json',config)
        for source,name in ((cert,'tls.crt'),(key,'tls.key')):
            target=state/name;target.write_bytes(source.read_bytes());target.chmod(0o600)
        # No state reader runs until all ownership is handed to the service UID.
        for path in (*state.rglob('*'),state):os.chown(path,account.pw_uid,account.pw_gid)
        return {'ok':True,'hub_assets_installed':True,'service_installed':False,'hub_started':False,
                'provider_access':False,'unit':unit_text(prefix=prefix,state=state,user=user)}
    except BaseException:
        for path in reversed(created):shutil.rmtree(path)
        raise


def private_digest(path, maximum=256*1024**2):
    path=Path(path).absolute()
    for ancestor in (path,*path.parents):require(not ancestor.is_symlink(),'unsafe_hub_backup')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and info.st_nlink==1
                and not stat.S_IMODE(info.st_mode)&0o077 and info.st_size<=maximum,'unsafe_hub_backup')
        digest=hashlib.sha256();size=0
        while True:
            data=os.read(fd,1024*1024)
            if not data:break
            size+=len(data);require(size<=maximum,'unsafe_hub_backup');digest.update(data)
        return digest.hexdigest()
    finally:os.close(fd)


def backup_package(root, destination):
    """Stopped consistent DB + policy backup; no TLS or OAuth material included."""
    from .store import Store
    from ..ratings.hub_migration import require_settled
    from ..ratings.policy import Policy
    hub_root,_=check(root)
    target=checked_root(Path(destination))
    require(not os.path.lexists(target),'fresh_hub_backup_required')
    parent=checked_root(target.parent)
    require(parent.is_dir() and not stat.S_IMODE(parent.stat().st_mode)&0o077,'private_restore_parent_required')
    require(not target.is_relative_to(hub_root), 'external_backup_directory_required')
    with file_lease(hub_root/'hub-server.lock'),file_lease(hub_root/'ratings-policy.lock'):
        store=Store(hub_root)
        require_settled(store)
        with store.transaction() as db:
            ratings=db.execute("SELECT 1 FROM meta WHERE key='ratings_format'").fetchone() is not None
            policy=Policy(store).check(db)[0] if ratings else None
        stage=Path(tempfile.mkdtemp(prefix='.hub-backup-',dir=parent))
        try:
            # SQLite backup obtains a consistent database image, including WAL.
            # The policy lock fences suppression/deletion while both are copied.
            from ..ratings.migration import snapshot_ledger
            backup=stage/'network.sqlite3'
            snapshot_ledger(store.path,backup,deadline_seconds=10)
            files={'network.sqlite3':private_digest(backup)}
            if policy is not None:
                atomic_json(stage/'ratings-policy.json',policy)
                files['ratings-policy.json']=private_digest(stage/'ratings-policy.json',512*1024)
            atomic_json(stage/'manifest.json',{'format':1,'hub_id':store.marker['hub_id'],'files':files})
            for path in stage.iterdir():
                with path.open('rb') as handle:os.fsync(handle.fileno())
            fd=os.open(stage,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
            require(not os.path.lexists(target),'fresh_hub_backup_required')
            os.rename(stage,target)
            fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
            return {'ok':True,'backup_created':True,'contains_private_identity_data':True,
                    'ratings_policy_included':ratings,'service_started':False}
        finally:
            if stage.exists():shutil.rmtree(stage)


def restore_package(source, destination, *, latest_ratings_policy=None):
    """Existing authority-reset restore, with verified package input and new root.

    The backup policy is evidence, not automatically the latest deletion fence.
    M4 restore always requires an explicitly retained latest policy input.
    """
    from .admin import restore
    source=checked_root(Path(source))
    require(source.is_dir() and not stat.S_IMODE(source.stat().st_mode)&0o077,'private_hub_backup_required')
    manifest=read_json(source/'manifest.json')
    fields(manifest,('format','hub_id','files'))
    from .contracts import identifier
    identifier(manifest['hub_id'])
    require(type(manifest['format']) is int and manifest['format']==1 and type(manifest['files']) is dict
            and set(manifest['files']) in ({'network.sqlite3'},{'network.sqlite3','ratings-policy.json'}),
            'hub_backup_manifest_refused')
    require({p.name for p in source.iterdir()}=={'manifest.json',*manifest['files']},'hub_backup_manifest_refused')
    for name,digest in manifest['files'].items():
        require(type(digest) is str and re.fullmatch('[a-f0-9]{64}',digest) is not None
                and private_digest(source/name,512*1024 if name.endswith('.json') else 256*1024**2)==digest,
                'hub_backup_identity_mismatch')
    ratings='ratings-policy.json' in manifest['files']
    require(not ratings or latest_ratings_policy is not None,'latest_ratings_policy_required',409)
    require(ratings or latest_ratings_policy is None,'unexpected_ratings_policy')
    return restore(source/'network.sqlite3',destination,ratings_policy=latest_ratings_policy)


def check_tls(root):
    """Validate service-owned bounded TLS input before a installed readiness check."""
    from .server import tls
    for name in ('tls.crt','tls.key'):
        private_digest(root/name,65536)
    tls(root/'tls.crt',root/'tls.key')


def main(argv=None):
    import argparse
    import sys
    from ..config import ConfigError
    from .contracts import NetworkError
    parser=argparse.ArgumentParser(description='Isolated central hub service and recovery; no osu! credentials')
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('action',choices=('serve','check','backup','restore','admin'))
    parser.add_argument('admin_action',nargs='?',choices=('invite','revoke','inspect','ratings-grant',
                        'ratings-inspect','ratings-link-replacement','ratings-grant-seed',
                        'ratings-policy','ratings-policy-recover'))
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--latest-ratings-policy',type=Path)
    args=parser.parse_args(argv)
    try:
        root,_=check(args.root)
        config=settings(read_json(root/'hub.json'))
        if args.action=='admin':
            require(args.admin_action is not None,'hub_admin_action_required')
            from .admin import main as administrative
            return administrative(['--root',str(root),args.admin_action])
        require(args.admin_action is None,'unexpected_admin_action')
        if args.action=='serve':
            from .admin import serve
            print(canonical({'ok':True,'hub_starting':True,'provider_access':False}).decode(),flush=True)
            serve(root,root/'tls.crt',root/'tls.key',bind=config['bind'],port=config['port'])
            result={'ok':True,'hub_stopped':True,'provider_access':False}
        elif args.action=='check':
            from .admin import inspect
            from .store import Store
            from ..ratings.hub_migration import require_settled
            store=Store(root);require_settled(store);check_tls(root)
            result={**inspect(store),'configuration_valid':True,'tls_pair_valid':True}
        elif args.action=='backup':
            require(args.destination is not None,'backup_destination_required')
            result=backup_package(root,args.destination)
        else:
            require(args.source is not None and args.destination is not None,'restore_source_and_destination_required')
            result=restore_package(args.source,args.destination,latest_ratings_policy=args.latest_ratings_policy)
    except (NetworkError,ConfigError,OSError,ValueError,TypeError,KeyError) as exc:
        result={'ok':False,'error':exc.code if isinstance(exc,NetworkError) else 'hub_operation_refused',
                'provider_access':False}
    sys.stdout.buffer.write(canonical(result)+b'\n');sys.stdout.buffer.flush()
    return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
