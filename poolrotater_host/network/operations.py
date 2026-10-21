"""Root administration for the dedicated network agent, independent from rooms."""
import os
import stat
import tempfile

from ..config import ConfigError
from ..layout import file_lease
from ..pilot_installation import administrative_binding, paths
from .. import pilot_operations as pilot
from ..provisioning import protected
from .contracts import require, decode, canonical
from .installed import ACTIONS


def command(instance,action,stream=None):
    require(action in ACTIONS,'invalid_network_action')
    require((stream is not None)==(action in ('configure','enroll','reset','update-report','reporting-configure')),'network_private_input_required')
    target,_,record=administrative_binding(instance)
    # Integrity guard at the actual service UID before reading even explicit input.
    pilot._service_command(target,record,['--pilot-check'])
    data=None;arguments=['--network-action',action]
    if stream is not None:
        data=canonical(decode(stream.read(65537)));arguments.append('--network-input')
    return pilot._service_command(target,record,arguments,input_data=data,timeout=30)


def name(instance):return 'poolrotater-v7-network-'+paths(instance)['root'].name+'.service'


def unit_text(instance):
    target=paths(instance)
    hidden=' '.join('-'+str(target['root']/'secrets'/s) for s in (
        'osu-token.json','oauth-pending.json','oauth-candidate.json','operator-client.json',
        'provider-osu-token.json','provider-oauth-pending.json','provider-oauth-candidate.json','provider-operator-client.json'))
    return f'''[Unit]
Description=PoolRotater independent network heartbeat ({instance})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={target['user']}
Group={target['user']}
WorkingDirectory={target['root']}
ExecStart=/usr/bin/python3.12 -I -S {target['prefix']}/bootstrap.py --root {target['root']} --network-agent
Restart=on-failure
RestartSec=30
KillMode=control-group
KillSignal=SIGTERM
SendSIGKILL=yes
TimeoutStopSec=15
UMask=0077
Slice=poolrotater-v7-transfer.slice
Nice=17
CPUQuota=50%
MemoryMax=512M
MemorySwapMax=128M
TasksMax=16
LimitCORE=0
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={target['root']}
InaccessiblePaths={hidden}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
Environment=LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2

[Install]
WantedBy=multi-user.target
'''


def _checked_unit(instance,optional=False):
    path=pilot.UNIT_BASE/name(instance)
    if optional and not os.path.lexists(path):return None
    protected(path);info=path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>16384
            or path.read_bytes() not in (unit_text(instance).encode(),
                unit_text(instance).replace('Slice=poolrotater-v7-transfer.slice\n','').replace('Nice=17\nCPUQuota=50%','Nice=15').encode())):
        raise ConfigError('network agent unit differs from the reviewed instance unit')
    return path


def _state(instance):
    raw=pilot._systemctl('show',name(instance),'--property=LoadState,ActiveState,SubState,UnitFileState,MainPID')
    try:
        value=dict(line.split('=',1) for line in raw.decode('ascii').splitlines())
        require(set(value)=={'LoadState','ActiveState','SubState','UnitFileState','MainPID'},'invalid_network_unit_state')
        value['MainPID']=int(value['MainPID']);require(value['MainPID']>=0,'invalid_network_unit_state')
    except (ValueError,UnicodeError):raise ConfigError('network agent state refused') from None
    return value


def unit(instance,action):
    require(action in ('unit','start','stop','remove'),'invalid_network_unit_action')
    target,_,record=administrative_binding(instance)
    # Stop/remove must remain available when local network state needs review.
    # The code/environment guard does not parse mutable network secrets.
    pilot._service_command(target,record,['--pilot-check'])
    # This lease does not overlap supervisor/account/ownership/catalog leases.
    with file_lease(target['prefix']/'network-admin.lock'):
        if action in ('unit','start'):
            from .resources import ensure
            if ensure(pilot.UNIT_BASE):pilot._systemctl('daemon-reload')
        path=_checked_unit(instance,optional=action in ('unit','remove'))
        state=_state(instance)
        from ..provisioning import _control
        from ..managed_host.policy import NAME
        managed=_control(target['root']/NAME,record['service_uid'],private=True,optional=True) is not None
        permitted=('','disabled','not-found','enabled') if managed else ('','disabled','not-found')
        if state['UnitFileState'] not in permitted:
            raise ConfigError('network pilot agent must remain disabled for explicit testing')
        if action=='unit':
            if state['MainPID'] or state['ActiveState'] not in ('inactive','failed'):
                raise ConfigError('network unit preparation requires stopped network agent')
            if path is None:
                if state['LoadState']!='not-found':raise ConfigError('unknown network unit cannot be adopted')
                protected(pilot.UNIT_BASE);path=pilot.UNIT_BASE/name(instance);temporary=None
                try:
                    with tempfile.NamedTemporaryFile(dir=pilot.UNIT_BASE,prefix='.prv7-network-',delete=False) as f:
                        from pathlib import Path
                        temporary=Path(f.name);os.fchmod(f.fileno(),0o644);f.write(unit_text(instance).encode());f.flush();os.fsync(f.fileno())
                    os.link(temporary,path)
                finally:
                    if temporary is not None:temporary.unlink(missing_ok=True)
            _checked_unit(instance);pilot._systemctl('daemon-reload');state=_state(instance)
            require(state['ActiveState']=='inactive' and state['MainPID']==0 and state['UnitFileState']=='disabled','network_unit_not_stopped')
        elif action=='remove':
            if state['MainPID'] or state['ActiveState'] not in ('inactive','failed'):
                raise ConfigError('network removal requires stopped network agent')
            if path is not None:path.unlink();pilot._systemctl('daemon-reload')
        else:
            if action=='start':
                # A known predecessor unit is upgraded only while stopped.
                if path.read_bytes()!=unit_text(instance).encode():
                    require(state['MainPID']==0 and state['ActiveState'] in ('inactive','failed'),'network_policy_upgrade_requires_stopped')
                    with tempfile.NamedTemporaryFile(dir=pilot.UNIT_BASE,prefix='.prv7-network-policy-',delete=False) as f:
                        from pathlib import Path
                        temporary=Path(f.name);os.fchmod(f.fileno(),0o644);f.write(unit_text(instance).encode());f.flush();os.fsync(f.fileno())
                    try:os.replace(temporary,path)
                    finally:temporary.unlink(missing_ok=True)
                    pilot._systemctl('daemon-reload')
                status=pilot._service_command(target,record,['--network-action','status'])
                require(status.get('enrolled') is True and status.get('pending_operation') is None,'network_enrollment_required')
            pilot._systemctl(action,name(instance),timeout=25);state=_state(instance)
            if action=='start':require(state['MainPID']>0 and state['ActiveState'] in ('active','activating'),'network_agent_startup_failed')
            if action=='stop':require(state['MainPID']==0 and state['ActiveState'] in ('inactive','failed'),'network_shutdown_incomplete')
        directory_fd=os.open(pilot.UNIT_BASE,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory_fd)
        finally:os.close(directory_fd)
    if action=='start':
        from ..updates.defaults import initialize as initialize_updates
        initialize_updates(instance)
        from ..rescue.operations import ensure
        ensure(instance,record)
        if managed:
            from ..managed_host.operations import ensure as ensure_managed
            ensure_managed(instance,record)
    return {'ok':True,'network_service_installed':action!='remove','network_service_state':state['ActiveState'],
            'network_service_enabled':state.get('UnitFileState')=='enabled','live_rooms_affected':False,'production_deployed':False}
