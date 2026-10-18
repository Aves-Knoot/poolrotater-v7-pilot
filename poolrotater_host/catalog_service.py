"""Frozen catalog listener and root-only sealed-input refresh assets.

No command creates accounts, enables units, starts listeners or reads research DBs.
Wheel dependencies come only from the existing exact-hash catalog lock.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import zipfile

from .config import ConfigError, fields as config_fields
from .layout import atomic_json, checked_root, file_lease, read_json
from .catalog_export import MODES, inspect_bundle, sha256
from .catalog_trust import no_links
from .network.hub_service import BOOTSTRAP as HUB_BOOTSTRAP

PREFIX=Path('/opt/poolrotater-v7-catalog')
STATE=Path('/var/lib/poolrotater-v7-catalog')
USER='poolrotater-catalog'
UNIT='poolrotater-v7-catalog.service'
REFRESH='poolrotater-v7-catalog-refresh.service'
TIMER='poolrotater-v7-catalog-refresh.timer'
BOOTSTRAP=HUB_BOOTSTRAP.replace('hub code','catalog code').replace('1048576','67108864').replace(
    "hashlib.sha256(p.read_bytes()).hexdigest()", "hashlib.file_digest(p.open('rb'),'sha256').hexdigest()").replace(
    'protected_hub_runtime_refused','protected_catalog_runtime_refused').replace(
    'from poolrotater_host.network.hub_service import main',
    "sys.path.insert(0,str(base/'dependencies'))\nfrom poolrotater_host.catalog_service import main")


def require(value,code):
    if not value:raise ConfigError(code)


def fields(value,names):
    return config_fields(value,names,'catalog service')


def public_json(path,maximum=512*1024):
    from .catalog_trust import bounded_file
    from .network.contracts import decode
    with bounded_file(path,maximum) as stream:return decode(stream.read(),maximum=maximum)


def path_word(value):
    value=Path(value)
    require(value.is_absolute() and re.fullmatch('/[A-Za-z0-9_./-]+',str(value)) is not None
            and '..' not in value.parts,'catalog_service_path_refused')
    return value


def protected(path,*,directory=False,maximum=1024*1024,uid=0,private=False):
    path=no_links(path);info=path.lstat()
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and info.st_uid==uid and not stat.S_IMODE(info.st_mode)&(0o077 if private else 0o022)
            and (directory or info.st_nlink==1 and info.st_size<=maximum),'catalog_service_input_refused')
    for parent in path.parents:
        info=parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not stat.S_IMODE(info.st_mode)&0o022,
                'catalog_service_parent_refused')
    return path


def listener_settings(value):
    fields(value,('format','bind','port','served','service_uid'))
    require(type(value['format']) is int and value['format']==1 and value['bind'] in ('127.0.0.1','0.0.0.0')
            and type(value['port']) is int and 1024<=value['port']<=65535
            and type(value['service_uid']) is int and value['service_uid']>0,'catalog_listener_configuration_refused')
    path_word(value['served']);return value


def units(*,prefix=PREFIX,state=STATE,user=USER,custody=Path('/var/lib/poolrotater-v7-catalog-signers/authority'),
          served=Path('/srv/poolrotater-v7-catalog')):
    for value in (prefix,state,custody,served):path_word(value)
    require(re.fullmatch('[a-z][a-z0-9-]{0,31}',user) is not None,'catalog_identity_refused')
    common='''UMask=0077
Slice=poolrotater-v7-transfer.slice
Nice=17
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
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
Environment=LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2
StandardOutput=journal
StandardError=journal
'''
    listener=f'''[Unit]
Description=PoolRotater signed read-only catalog HTTPS origin
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User={user}
Group={user}
ExecStart=/usr/bin/python3.12 -I -S {prefix}/bootstrap.py --state {state} serve
Restart=on-failure
RestartSec=30
KillMode=control-group
SendSIGKILL=yes
TimeoutStopSec=20
RestrictAddressFamilies=AF_INET AF_UNIX
InaccessiblePaths={custody}
{common}
[Install]
WantedBy=multi-user.target
'''
    refresh=f'''[Unit]
Description=PoolRotater owner sealed catalog metadata refresh
After=local-fs.target

[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3.12 -I -S {prefix}/bootstrap.py --state {state} refresh
TimeoutStartSec=30min
KillMode=control-group
SendSIGKILL=yes
TimeoutStopSec=20
PrivateNetwork=yes
RestrictAddressFamilies=AF_UNIX
ReadWritePaths={custody} {served}
{common}'''
    timer=f'''[Unit]
Description=PoolRotater daily signed catalog metadata renewal

[Timer]
OnCalendar=daily
RandomizedDelaySec=30min
Persistent=true
Unit={REFRESH}

[Install]
WantedBy=timers.target
'''
    return {UNIT:listener,REFRESH:refresh,TIMER:timer}


def freeze_dependencies(cache,destination):
    """Extract exact reviewed wheels; no pip, import hook, network or mutable env."""
    from .runtime_package import pins, safe_name, digest
    from .catalog_trust import PINS
    from email.parser import BytesParser
    lock=Path(__file__).resolve().parents[1]/'catalog-requirements-hashed.lock'
    required=pins(lock.read_text());seen=set();budget=0
    require({name:pin['version'] for name,pin in required.items()}==PINS,'catalog_dependency_pin_contract_changed')
    for wheel in sorted(no_links(cache).glob('*.whl')):
        no_links(wheel);info=wheel.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<64*1024**2,'catalog_wheel_refused')
        with zipfile.ZipFile(wheel) as archive:
            metadata=[n for n in archive.namelist() if n.endswith('.dist-info/METADATA') and n.count('/')==1]
            require(len(metadata)==1,'catalog_wheel_refused')
            meta=BytesParser().parsebytes(archive.read(metadata[0]))
            name=re.sub('[-_.]+','-',meta['Name']).lower()
            if name not in required:continue
            require(name not in seen and meta['Version']==required[name]['version']
                    and digest(wheel)==required[name]['sha256'],'catalog_wheel_pin_mismatch')
            seen.add(name)
            for member in archive.infolist():
                if member.is_dir():continue
                safe_name(member.filename)
                require(not member.filename.endswith('.pth') and '.data/' not in member.filename
                        and stat.S_IFMT(member.external_attr>>16) in (0,stat.S_IFREG),'catalog_wheel_member_refused')
                budget+=member.file_size
                require(member.file_size<=64*1024**2 and budget<=192*1024**2,'catalog_dependency_budget_exceeded')
                target=destination/member.filename
                require(not target.exists(),'catalog_wheel_collision')
                target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
                with archive.open(member) as source,target.open('xb') as output:shutil.copyfileobj(source,output,1024*1024)
                target.chmod(0o644)
    require(seen==set(required),'catalog_dependency_closure_missing')
    return required


def prepare(destination,*,wheel_cache):
    import platform
    require(platform.system()=='Linux' and platform.machine()=='x86_64'
            and platform.python_version_tuple()[:2]==('3','12'),'catalog_service_reference_platform_required')
    destination=checked_root(Path(destination));require(not os.path.lexists(destination),'fresh_catalog_kit_required')
    destination.mkdir(mode=0o700)
    try:
        runtime=destination/'runtime';runtime.mkdir(mode=0o755)
        source=Path(__file__).resolve().parent
        for path in sorted(source.rglob('*.py')):
            no_links(path);target=runtime/'poolrotater_host'/path.relative_to(source)
            target.parent.mkdir(mode=0o755,parents=True,exist_ok=True);target.write_bytes(path.read_bytes());target.chmod(0o644)
        # Existing catalog validators import these stdlib map-selection contracts
        # for DT/jump provenance; preserve them rather than inventing constants.
        for name in ('__init__.py','standard_pool.py','pool.py','jump_catalog.py'):
            path=source.parent/'bot'/name;no_links(path);target=runtime/'bot'/name
            target.parent.mkdir(mode=0o755,exist_ok=True);target.write_bytes(path.read_bytes());target.chmod(0o644)
        (runtime/'bootstrap.py').write_text(BOOTSTRAP);(runtime/'bootstrap.py').chmod(0o644)
        dependencies=runtime/'dependencies';dependencies.mkdir(mode=0o755)
        pins=freeze_dependencies(wheel_cache,dependencies)
        inventory={p.relative_to(runtime).as_posix():sha256(p) for p in sorted(runtime.rglob('*')) if p.is_file()}
        atomic_json(runtime/'inventory.json',{'format':1,'files':inventory});(runtime/'inventory.json').chmod(0o644)
        for name,data in units().items():(destination/name).write_text(data)
        from .network.resources import NAME,TEXT
        (destination/NAME).write_text(TEXT)
        return {'ok':True,'inventory_sha256':sha256(runtime/'inventory.json'),'runtime_files':len(inventory),
                'dependencies':{k:v['version'] for k,v in pins.items()},'service_installed':False,'listener_started':False}
    except BaseException:shutil.rmtree(destination);raise


def install_assets(kit,*,expected_inventory_sha256,cert,key,served,custody,
                   prefix=PREFIX,state=STATE,user=USER,bind='127.0.0.1',port=9445):
    return _install_assets(kit,expected_inventory_sha256=expected_inventory_sha256,cert=cert,key=key,
        served=served,custody=custody,prefix=prefix,state=state,user=user,bind=bind,port=port,
        bootstrap=BOOTSTRAP,unit_factory=units,unit_names=(UNIT,REFRESH,TIMER))


def _install_assets(kit,*,expected_inventory_sha256,cert,key,served,custody,
                   prefix,state,user,bind,port,bootstrap,unit_factory,unit_names):
    """Shared reviewed fresh origin installer; no unit/service activation."""
    import pwd
    require(os.geteuid()==0,'catalog_administrator_required')
    unit_factory(prefix=prefix,state=state,user=user,custody=custody,served=served)
    account=pwd.getpwnam(user);require(account.pw_uid>0,'catalog_identity_refused')
    config=listener_settings({'format':1,'bind':bind,'port':port,'served':str(served),'service_uid':account.pw_uid})
    no_links(served);no_links(custody)
    if os.path.lexists(served):protected(served,directory=True)
    else:protected(Path(served).parent,directory=True)
    if os.path.lexists(custody):protected(custody,directory=True,private=True)
    else:protected(Path(custody).parent,directory=True,private=True)
    require(not Path(custody).is_relative_to(served) and not Path(served).is_relative_to(custody),'catalog_custody_separation_required')
    protected(cert,maximum=65536);protected(key,maximum=65536,private=True)
    from .catalog_server import tls_context
    tls_context(cert,key)
    kit=no_links(kit);runtime=kit/'runtime';record=public_json(runtime/'inventory.json')
    fields(record,('format','files'))
    require(re.fullmatch('[a-f0-9]{64}',expected_inventory_sha256 or '') is not None
            and sha256(runtime/'inventory.json')==expected_inventory_sha256
            and type(record['format']) is int and record['format']==1 and type(record['files']) is dict
            and record['files'].get('bootstrap.py')==hashlib.sha256(bootstrap.encode()).hexdigest(),'catalog_inventory_refused')
    actual={p.relative_to(runtime).as_posix() for p in runtime.rglob('*') if not p.is_dir()}
    require(actual=={'inventory.json',*record['files']},'catalog_inventory_refused')
    from .runtime_package import safe_name
    for name,digest in record['files'].items():
        safe_name(name);require(name=='bootstrap.py' or name.startswith(('poolrotater_host/','dependencies/','bot/')),'catalog_inventory_refused')
        path=no_links(runtime/name);info=path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=64*1024**2
                and re.fullmatch('[a-f0-9]{64}',digest or '') is not None and sha256(path)==digest,'catalog_inventory_refused')
    for path in (prefix,state):
        path_word(path);protected(path.parent,directory=True);require(not os.path.lexists(path),'fresh_catalog_installation_required')
    created=[]
    try:
        prefix.mkdir(mode=0o755);prefix.chmod(0o755);created.append(prefix)
        for name in (*record['files'],'inventory.json'):
            target=prefix/name;target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
            shutil.copyfile(runtime/name,target);target.chmod(0o644)
        # Root maintenance commonly uses umask077. Executable code is public
        # and immutable; explicitly grant traversal to the dedicated listener.
        for directory in (p for p in prefix.rglob('*') if p.is_dir()):directory.chmod(0o755)
        state.mkdir(mode=0o755);state.chmod(0o755);created.append(state)
        atomic_json(state/'listener.json',config);(state/'listener.json').chmod(0o644)
        for source,name in ((cert,'tls.crt'),(key,'tls.key')):
            target=state/name;shutil.copyfile(source,target);target.chmod(0o600);os.chown(target,account.pw_uid,account.pw_gid)
        for name,data in unit_factory(prefix=prefix,state=state,user=user,custody=custody,served=served).items():
            (prefix/name).write_text(data);(prefix/name).chmod(0o644)
        # Units enter the protected runtime receipt as part of admission.
        files={**record['files'],**{name:sha256(prefix/name) for name in unit_names}}
        atomic_json(prefix/'inventory.json',{'format':1,'files':files});(prefix/'inventory.json').chmod(0o644)
        return {'ok':True,'service_installed':False,'listener_started':False,'timer_enabled':False,'refresh_configured':False,
                'installed_inventory_sha256':sha256(prefix/'inventory.json')}
    except BaseException:
        for path in reversed(created):shutil.rmtree(path)
        raise


def configure_refresh(state,*,custody,bundles,exclusions=None,allow_expired_pending_renewal=False):
    """Admin pins explicit settled all-mode inputs; no collector is invoked."""
    require(os.geteuid()==0,'catalog_administrator_required')
    protected(state,directory=True);protected(custody,directory=True,private=True)
    from .catalog_publication import Publisher
    publisher=Publisher(custody);require(set(bundles)==set(MODES),'all_prepared_catalog_modes_required')
    require(type(allow_expired_pending_renewal) is bool,'catalog_refresh_configuration_refused')
    inputs={}
    for mode,path in bundles.items():
        path=path_word(path);protected(path,directory=True)
        manifest=inspect_bundle(path);require(manifest['mode']==mode,'catalog_refresh_mode_refused')
        for child in path.rglob('*'):protected(child,directory=child.is_dir(),maximum=2**31)
        inputs[mode]={'path':str(path),'manifest_sha256':sha256(path/'manifest.json')}
    feed=None
    if exclusions is not None:
        exclusions=path_word(exclusions);protected(exclusions,maximum=65536)
        from .catalog_exclusions import validate_feed
        validate_feed(public_json(exclusions,maximum=65536));feed={'path':str(exclusions),'sha256':sha256(exclusions)}
    config={'format':1,'custody':str(custody),'repository_id':publisher.state['repository_id'],
            'bootstrap_sha256':publisher.state['bootstrap_sha256'],'served':str(publisher.served),'inputs':inputs,
            'exclusions':feed,'allow_expired_pending_renewal':allow_expired_pending_renewal}
    atomic_json(Path(state)/'refresh.json',config)
    return {'ok':True,'refresh_configured':True,'timer_enabled':False,'research_collection_or_analysis':False}


def refresh(state,*,dry_run=False):
    require(os.geteuid()==0,'catalog_administrator_required')
    protected(state,directory=True);protected(Path(state)/'refresh.json',private=True)
    config=read_json(Path(state)/'refresh.json');fields(config,('format','custody','repository_id','bootstrap_sha256',
                  'served','inputs','exclusions','allow_expired_pending_renewal'))
    require(type(config['format']) is int and config['format']==1 and type(config['allow_expired_pending_renewal']) is bool
            and type(config['inputs']) is dict and set(config['inputs'])==set(MODES),'catalog_refresh_configuration_refused')
    from .catalog_publication import Publisher
    publisher=Publisher(protected(config['custody'],directory=True,private=True))
    require(publisher.state['repository_id']==config['repository_id'] and publisher.state['bootstrap_sha256']==config['bootstrap_sha256']
            and str(publisher.served)==config['served'],'catalog_refresh_authority_refused')
    # The publisher owns the durable lease. Separate scheduler invocations must
    # not observe the same pending receipt and both publish a follow-up.
    with file_lease(Path(config['custody'])/'scheduler.lock'):
        publisher=Publisher(config['custody'])
        if not dry_run and (publisher.state['pending'] is not None or publisher.state['renewal'] is not None):
            try:publisher.resume()
            except ConfigError as error:
                require(str(error)=='pending_catalog_metadata_expired' and config['allow_expired_pending_renewal'],
                        'catalog_refresh_pending_requires_review')
                publisher.renew_pending()
            publisher=Publisher(config['custody'])
        bundles={}
        for mode,item in config['inputs'].items():
            fields(item,('path','manifest_sha256'));path=protected(item['path'],directory=True)
            require(sha256(protected(path/'manifest.json'))==item['manifest_sha256'],'catalog_refresh_inputs_changed')
            for child in path.rglob('*'):protected(child,directory=child.is_dir(),maximum=2**31)
            bundles[mode]=path
        feed=None
        if config['exclusions'] is not None:
            item=config['exclusions'];fields(item,('path','sha256'));path=protected(item['path'],maximum=65536)
            require(sha256(path)==item['sha256'],'catalog_refresh_inputs_changed');feed=public_json(path,maximum=65536)
        if dry_run:
            for mode,path in bundles.items():require(inspect_bundle(path)['mode']==mode,'catalog_refresh_mode_refused')
            return {'ok':True,'refresh_configuration_valid':True,'dependencies_verified':True,
                    'catalogs_published':False,'research_collection_or_analysis':False}
        result=publisher.publish(bundles,exclusions=feed)
        return {**result,'scheduled_refresh':True,'research_collection_or_analysis':False}


def main(argv=None):
    import argparse,json
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('action',choices=('check','serve','refresh','check-refresh','initialize','configure-refresh'))
    parser.add_argument('--custody',type=Path);parser.add_argument('--served',type=Path)
    for mode in MODES:parser.add_argument('--'+mode,type=Path)
    parser.add_argument('--exclusions',type=Path);parser.add_argument('--allow-expired-pending-renewal',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.action=='initialize':
            require(os.geteuid()==0 and args.custody is not None and args.served is not None,'catalog_administrator_required')
            protected(args.custody.parent,directory=True,private=True);protected(args.served.parent,directory=True)
            from .catalog_publication import initialize
            result=initialize(args.custody,args.served)
        elif args.action=='configure-refresh':
            require(args.custody is not None and all(getattr(args,mode) is not None for mode in MODES),'catalog_refresh_configuration_refused')
            result=configure_refresh(args.state,custody=args.custody,bundles={mode:getattr(args,mode) for mode in MODES},
                exclusions=args.exclusions,allow_expired_pending_renewal=args.allow_expired_pending_renewal)
        elif args.action in ('refresh','check-refresh'):result=refresh(args.state,dry_run=args.action=='check-refresh')
        else:
            protected(args.state,directory=True);protected(args.state/'listener.json')
            config=listener_settings(public_json(args.state/'listener.json'))
            require(os.geteuid()==config['service_uid'],'catalog_listener_identity_refused')
            protected(config['served'],directory=True)
            for name in ('tls.crt','tls.key'):protected(args.state/name,uid=os.geteuid(),maximum=65536,private=True)
            from .catalog_server import CatalogServer,tls_context,serve
            context=tls_context(args.state/'tls.crt',args.state/'tls.key')
            if args.action=='check':
                # Validate root shape without opening the reviewed service port.
                server=CatalogServer(config['served'],('127.0.0.1',0),context=context,protected_uid=0);server.server_close()
                result={'ok':True,'configuration_valid':True,'tls_pair_valid':True,'listener_started':False}
            else:
                print(json.dumps({'ok':True,'catalog_starting':True}),flush=True)
                serve(config['served'],config['bind'],config['port'],args.state/'tls.crt',args.state/'tls.key')
                result={'ok':True,'catalog_stopped':True}
    except Exception as error:
        code=str(error) if isinstance(error,ConfigError) else ''
        # Only closed code-shaped identifiers are observable; never arbitrary
        # crypto/library errors, local paths, PEM or provider payloads.
        if re.fullmatch('[a-z][a-z0-9_]{1,100}',code) is None:code='catalog_service_operation_refused'
        result={'ok':False,'error':code,'provider_access':False}
    print(json.dumps(result,sort_keys=True));return 0 if result['ok'] else 1
