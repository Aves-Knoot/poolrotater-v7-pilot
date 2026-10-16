#!/usr/bin/python3
"""Prepare an exact sealed hub upgrade; default action is a read-only plan.

Production activation is deliberately a separate exchange command after the
maintainer stops only the hub/publisher and installs the reviewed assets. Bot
units, operator choices, TLS files, account credentials and live rooms are not
controlled by this tool.
"""
from contextlib import contextmanager
import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME=ROOT/'validation/v7.1.0-release-final2/runtime.zip'
DEFAULT_SHA='8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35'
# Retain the actually installed unit's historical path. Its name is not a
# version authority: the verified inventory determines the deployed code.
PREFIX=Path('/opt/poolrotater-v7-hub-v706')
NEXT=Path('/opt/poolrotater-v7-hub-v706.v710-next')
STATE=Path('/var/lib/poolrotater-v7-hub')
ARCHIVES=Path('/var/backups/poolrotater-v7-hub')
OUTPUT=Path('/var/lib/poolrotater-v7-analytics/activity.json')
INPUT=Path('/etc/poolrotater-v7/activity-publisher.json')
HUB_USER='poolrotater-hub'
ANALYTICS_GROUP='poolrotater-analytics'
HUB_UNIT='poolrotater-v7-hub.service'
PUBLISHER='poolrotater-v7-activity-publisher.service'
TIMER='poolrotater-v7-activity-publisher.timer'
ANALYTICS='poolrotator-wiki-analytics.service'
UNIT_FILE=Path('/etc/systemd/system')/HUB_UNIT


def require(value,message):
    if not value:raise ValueError(message)


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def safe_path(path):
    path=Path(path).absolute()
    require(re.fullmatch(r'/[A-Za-z0-9_./-]+',str(path)) is not None and '..' not in path.parts,'unsafe_transition_path')
    for p in (path,*path.parents):require(not p.is_symlink(),'transition_symlink_refused')
    return path


def inventory(directory,*,owner=0,ancestors=True):
    directory=safe_path(directory)
    for parent in ((directory,*directory.parents) if ancestors else (directory,)):
        info=parent.stat();require(stat.S_ISDIR(info.st_mode) and info.st_uid==owner and not info.st_mode&0o022,'protected_hub_code_required')
    receipt=directory/'inventory.json';require(receipt.stat().st_size<=1024*1024,'hub_inventory_too_large')
    value=json.loads(receipt.read_text());require(set(value)=={'format','files'} and type(value['format'])is int and value['format']==1 and type(value['files'])is dict,'hub_inventory_refused')
    actual=set()
    for p in directory.rglob('*'):
        info=p.lstat();require(not p.is_symlink() and info.st_uid==owner and not info.st_mode&0o022,'protected_hub_code_required')
        if p.is_dir():continue
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=1024*1024,'protected_hub_code_required')
        name=p.relative_to(directory).as_posix()
        if name=='inventory.json':continue
        actual.add(name);require(name in value['files'] and sha(p)==value['files'][name],'hub_code_inventory_mismatch')
    require(actual==set(value['files']),'hub_code_inventory_mismatch')
    return sha(receipt)


def assets(owner_account_id):
    require(type(owner_account_id)is int and owner_account_id>0,'explicit_owner_account_required')
    publisher=f'''[Unit]
Description=Publish private PoolRotater community statistics snapshot
After={HUB_UNIT}
ConditionPathExists={PREFIX}/inventory.json

[Service]
Type=oneshot
User={HUB_USER}
Group={ANALYTICS_GROUP}
ExecStart=/usr/bin/python3.12 -I -S {PREFIX}/bootstrap.py --root {STATE} admin activity-export
StandardInput=file:{INPUT}
UMask=0027
Nice=15
CPUQuota=25%
MemoryMax=512M
MemorySwapMax=128M
TimeoutStartSec=90
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateNetwork=yes
PrivateDevices=yes
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX
ReadWritePaths={STATE} {OUTPUT.parent}
InaccessiblePaths={STATE}/tls.key {STATE}/tls.crt
StandardOutput=journal
StandardError=journal
'''
    timer=f'''[Unit]
Description=Refresh private community statistics every minute

[Timer]
OnBootSec=90s
OnUnitInactiveSec=60s
AccuracySec=5s
RandomizedDelaySec=5s
Unit={PUBLISHER}

[Install]
WantedBy=timers.target
'''
    analytics=f'''[Service]
SupplementaryGroups={ANALYTICS_GROUP}
Environment=POOLROTATER_NETWORK_STATISTICS_SNAPSHOT={OUTPUT}
ReadOnlyPaths={OUTPUT.parent}
InaccessiblePaths={STATE}
'''
    return {PUBLISHER:publisher,TIMER:timer,'95-community-statistics-readonly.conf':analytics,
            'activity-publisher.json':json.dumps({'output':str(OUTPUT),'owner_account_id':owner_account_id},sort_keys=True)+'\n',
            'poolrotater-analytics.conf':f'g {ANALYTICS_GROUP} -\n'}


def unpack_source(runtime,digest,destination):
    require(re.fullmatch('[a-f0-9]{64}',digest or '') is not None and sha(runtime)==digest,'sealed_runtime_digest_mismatch')
    with zipfile.ZipFile(runtime) as archive:
        require(len(archive.namelist())==len(set(archive.namelist())),'duplicate_runtime_member')
        bundle=json.loads(archive.read('bundle.json'));require(bundle['version']=='v7.1.0','unexpected_hub_candidate_version')
        names=[name for name in archive.namelist() if name.startswith('payload/') and name.endswith('.py')]
        require(0<len(names)<=1000,'runtime_source_bounds_refused')
        total=0
        for name in names:
            relative=Path(name.removeprefix('payload/'))
            require(not relative.is_absolute() and '..' not in relative.parts,'unsafe_runtime_member')
            info=archive.getinfo(name);total+=info.file_size
            require(info.file_size<=1024*1024 and total<=32*1024*1024,'runtime_source_bounds_refused')
            raw=archive.read(name);row=bundle['files'][name]
            require(row['bytes']==len(raw) and row['sha256']==hashlib.sha256(raw).hexdigest(),'runtime_source_inventory_mismatch')
            target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    return bundle


def stage(destination,runtime,digest,*,owner_account_id,old_inventory_sha256):
    destination=safe_path(destination)
    require(not destination.exists(),'fresh_transition_package_required')
    require(re.fullmatch('[a-f0-9]{64}',old_inventory_sha256 or '')is not None,'old_hub_inventory_digest_required')
    content=assets(owner_account_id)
    destination.mkdir(mode=0o700)
    try:
        source=destination/'.source';source.mkdir(mode=0o700)
        unpack_source(runtime,digest,source)
        child="import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from poolrotater_host.network.hub_service import prepare;print(json.dumps(prepare(Path(sys.argv[2])),sort_keys=True))"
        result=subprocess.run(['/usr/bin/python3.12','-I','-S','-B','-c',child,str(source),str(destination/'kit')],capture_output=True,text=True,timeout=60,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
        require(result.returncode==0,'sealed_hub_kit_preparation_failed')
        kit=json.loads(result.stdout);shutil.rmtree(source)
        unitdir=destination/'assets';unitdir.mkdir(mode=0o700)
        for name,text in content.items():(unitdir/name).write_text(text);(unitdir/name).chmod(0o600)
        tool=destination/'transition.py';tool.write_bytes(Path(__file__).read_bytes());tool.chmod(0o600)
        files={p.relative_to(destination).as_posix():sha(p)for p in destination.rglob('*')if p.is_file()}
        manifest={'format':1,'version':'v7.1.0','runtime_sha256':digest,'old_inventory_sha256':old_inventory_sha256,
                  'new_inventory_sha256':kit['inventory_sha256'],'files':files,'owner_account_id':owner_account_id,
                  'production_modified':False,'rooms_affected':False,'sharing_choices_changed':False}
        (destination/'transition.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n');(destination/'transition.json').chmod(0o600)
        return {'ok':True,'prepared':True,'package':str(destination),'new_inventory_sha256':kit['inventory_sha256'],
                'runtime_sha256':digest,'production_modified':False,'next_action':'review_docs_then_explicit_stopped_hub_exchange'}
    except BaseException:
        shutil.rmtree(destination);raise


def package(directory):
    directory=safe_path(directory);info=directory.stat()
    require(info.st_uid==0 and not info.st_mode&0o077,'root_private_transition_package_required')
    value=json.loads((directory/'transition.json').read_text())
    require(value.get('format')==1 and value.get('version')=='v7.1.0' and type(value.get('files'))is dict,'transition_manifest_refused')
    actual={p.relative_to(directory).as_posix()for p in directory.rglob('*')if not p.is_dir()}
    require(actual=={'transition.json',*value['files']},'transition_manifest_refused')
    for relative,digest in value['files'].items():
        path=safe_path(directory/relative);require(path.is_relative_to(directory),'transition_manifest_refused')
        info=path.lstat();require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1 and not info.st_mode&0o022 and sha(path)==digest,'transition_package_changed')
    require(inventory(directory/'kit/runtime')==value['new_inventory_sha256'],'transition_package_changed')
    return value


def systemd(unit):
    require(unit in (HUB_UNIT,PUBLISHER,TIMER),'unexpected_service_target')
    reply=subprocess.run(['/usr/bin/systemctl','show',unit,'--property=MainPID,ActiveState,LoadState,User,ExecStart','--no-pager'],capture_output=True,text=True,timeout=10,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
    state=dict(line.split('=',1)for line in reply.stdout.splitlines()if '='in line)
    require(reply.returncode==0 or (reply.returncode==4 and unit!=HUB_UNIT
            and state.get('LoadState')=='not-found'),'service_state_unavailable')
    return state


def unit_binding():
    """Never exchange an unused historical tree while the unit runs another."""
    path=safe_path(UNIT_FILE);info=path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022,
            'protected_hub_unit_required')
    command=f'/usr/bin/python3.12 -I -S {PREFIX}/bootstrap.py --root {STATE} serve'
    require(path.read_text().splitlines().count('ExecStart='+command)==1,
            'hub_unit_uses_a_different_code_prefix')
    actual=systemd(HUB_UNIT)
    require(actual.get('LoadState')=='loaded' and actual.get('User')==HUB_USER
            and 'argv[]='+command+' ;' in actual.get('ExecStart',''),
            'loaded_hub_unit_uses_a_different_code_prefix')
    return sha(path)


def no_hub_processes(uid):
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():continue
        try:
            lines=(entry/'status').read_text().splitlines()
            ids=next(line for line in lines if line.startswith('Uid:')).split()[1:]
        except FileNotFoundError:continue
        require(str(uid)not in ids,'hub_identity_still_has_processes')


def stopped(uid):
    for name in (HUB_UNIT,PUBLISHER,TIMER):
        value=systemd(name)
        if name!=HUB_UNIT and value.get('LoadState')=='not-found':continue
        require(value.get('LoadState')=='loaded' and value.get('ActiveState')in('inactive','failed')
                and (name.endswith('.timer') or value.get('MainPID')=='0'),
                'stop_only_hub_and_publisher_before_exchange')
        if name==HUB_UNIT:require(value.get('User')==HUB_USER,'unexpected_hub_identity')
    no_hub_processes(uid)


def backup_tree(source,destination,*,allowed_uids):
    source=safe_path(source);require(not destination.exists(),'fresh_transition_backup_required')
    total=0
    for path in (source,*source.rglob('*')):
        info=path.lstat();require(not path.is_symlink() and info.st_uid in allowed_uids,'unsafe_hub_backup_tree')
        if path.is_dir():continue
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1,'unsafe_hub_backup_tree')
        total+=info.st_size;require(total<=4*1024**3,'hub_transition_backup_capacity_required')
    require(shutil.disk_usage(destination.parent).free>total+1024**3,'hub_transition_backup_capacity_required')
    shutil.copytree(source,destination,copy_function=shutil.copy2)
    for path in (destination,*destination.rglob('*')):
        path.chmod(0o700 if path.is_dir() else 0o600)
    for path in destination.rglob('*'):
        if path.is_file():
            require(sha(path)==sha(source/path.relative_to(destination)),'hub_transition_backup_verification_failed')
            with path.open('rb') as stream:os.fsync(stream.fileno())
    fd=os.open(destination,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


@contextmanager
def state_lease(uid):
    path=STATE/'hub-server.lock';fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd);require(stat.S_ISREG(info.st_mode) and info.st_uid==uid and info.st_nlink==1 and not info.st_mode&0o077,'unsafe_hub_server_lease')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);yield
    finally:os.close(fd)


def atomic_exchange(left,right):
    """Linux RENAME_EXCHANGE: a crash cannot leave the protected prefix absent."""
    libc=ctypes.CDLL(None,use_errno=True);function=getattr(libc,'renameat2',None)
    require(function is not None,'atomic_directory_exchange_unavailable')
    function.argtypes=(ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint)
    function.restype=ctypes.c_int
    if function(-100,os.fsencode(left),-100,os.fsencode(right),2)!=0:
        raise OSError(ctypes.get_errno(),'atomic_directory_exchange_failed')
    fd=os.open(left.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def exchange(directory,*,rollback=False):
    require(os.geteuid()==0,'hub_transition_requires_root')
    value=package(directory);uid=pwd.getpwnam(HUB_USER).pw_uid;require(uid>0,'hub_service_identity_required')
    unit_sha256=unit_binding()
    stopped(uid)
    expected=value['new_inventory_sha256']if rollback else value['old_inventory_sha256']
    other=value['old_inventory_sha256']if rollback else value['new_inventory_sha256']
    require(inventory(PREFIX)==expected and inventory(NEXT)==other,'hub_transition_inventory_changed')
    safe_path(STATE);require(STATE.stat().st_uid==uid and not STATE.stat().st_mode&0o077,'private_hub_state_required')
    safe_path(ARCHIVES.parent)
    if not ARCHIVES.exists():ARCHIVES.mkdir(mode=0o700)
    require(ARCHIVES.stat().st_uid==0 and not ARCHIVES.stat().st_mode&0o077,'private_hub_archive_required')
    archive=ARCHIVES/(('rollback-'if rollback else 'upgrade-')+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+os.urandom(4).hex())
    archive.mkdir(mode=0o700)
    with state_lease(uid):
        stopped(uid)
        backup_tree(STATE,archive/'private-state',allowed_uids={0,uid})
        backup_tree(PREFIX,archive/'private-code',allowed_uids={0})
        # Recheck immediately before the one atomic mutation. State contents are
        # never overwritten or authority-reset during an ordinary code upgrade.
        require(inventory(PREFIX)==expected and inventory(NEXT)==other,'hub_transition_inventory_changed')
        receipt={'format':1,'phase':'prepared','rollback':rollback,'before':expected,'after':other,
                 'state_replaced':False,'sharing_choices_changed':False,'rooms_affected':False,
                 'hub_unit_sha256':unit_sha256}
        (archive/'receipt.json').write_text(json.dumps(receipt,sort_keys=True)+'\n');(archive/'receipt.json').chmod(0o600)
        atomic_exchange(PREFIX,NEXT)
        receipt['phase']='exchanged';(archive/'receipt.json').write_text(json.dumps(receipt,sort_keys=True)+'\n')
    return {'ok':True,**receipt,'archive':str(archive),'services_started':False,'next_action':'run_sealed_hub_check_then_start_only_hub_and_publisher'}


def main(argv=None):
    if os.getpriority(os.PRIO_PROCESS,0)<15:os.nice(15-os.getpriority(os.PRIO_PROCESS,0))
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',default='plan',choices=('plan','stage','exchange','rollback'))
    parser.add_argument('--runtime',type=Path,default=DEFAULT_RUNTIME);parser.add_argument('--runtime-sha256',default=DEFAULT_SHA)
    parser.add_argument('--destination',type=Path);parser.add_argument('--package',type=Path)
    parser.add_argument('--old-inventory-sha256');parser.add_argument('--owner-account-id',type=int)
    parser.add_argument('--confirm-stopped-hub',action='store_true')
    args=parser.parse_args(argv)
    if args.action=='plan':
        result={'ok':True,'action':'read-only-plan','production_modified':False,'runtime':str(args.runtime),
                'runtime_sha256':args.runtime_sha256,'hub_prefix':str(PREFIX),'hub_state':str(STATE),
                'old_inventory_sha256':inventory(PREFIX),'owner_account_id_required_for_stage':True,
                'bot_services_or_rooms_affected':False,'activation_requires_explicit_stopped_hub_exchange':True}
        result['active_hub_unit_sha256']=unit_binding()
    elif args.action=='stage':
        require(args.destination is not None,'transition_destination_required')
        result=stage(args.destination,args.runtime,args.runtime_sha256,owner_account_id=args.owner_account_id,
                     old_inventory_sha256=args.old_inventory_sha256)
    else:
        require(args.package is not None and args.confirm_stopped_hub,'explicit_stopped_hub_confirmation_required')
        result=exchange(args.package,rollback=args.action=='rollback')
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        print(json.dumps({'ok':False,'error':type(error).__name__,'production_activation_complete':False}));raise SystemExit(1)
