"""Local administration and fixed privileged reconciliation for one installation."""
import hashlib
import os
from pathlib import Path
import time

from ..config import ConfigError
from ..layout import atomic_json,file_lease
from ..network.contracts import canonical,decode,fields,integer,require,fingerprint


def installed(root,release,action,stream=None):
    from ..network.installed import guard
    from .policy import enabled,state,seed
    from .settings import inventory
    guard(root,release)
    require(enabled(root),'managed_policy_required')
    require(action in ('status','limit','request','bookings'),'invalid_managed_action')
    require((stream is not None)==(action in ('limit','request','bookings')),'managed_input_required')
    if action=='bookings':
        value=decode(stream.read(2049),maximum=2048)
        from ..bookings.policy import configure
        result={'bookings':configure(root,value)}
        return {'ok':True,**result,'protected_runtime_checked':True,'provider_dispatch':False}
    store=state(root);seed(root,store)
    if action=='limit':
        value=decode(stream.read(1025),maximum=1024);fields(value,('max_concurrent_rooms',))
        result={'limits':store.set_limit(value['max_concurrent_rooms'])}
    elif action=='request':
        value=decode(stream.read(65537))
        # Local root operator uses the same typed revision/binding contract as
        # central commands. Secret settings arrive on stdin, never argv.
        result={'receipt':store.enqueue(value)}
    else:
        from ..bookings.policy import load as booking_policy
        from ..bookings.agent import diagnostics as booking_diagnostics
        result={'capabilities':store.snapshot(),'limits':store.limits(),'settings_inventory':inventory(),
                'rooms':[store.room(row['alias']) for row in store.snapshot()['rooms']],
                'bookings':{'policy':booking_policy(root),'agent':booking_diagnostics(root)}}
    return {'ok':True,**result,'protected_runtime_checked':True,'provider_dispatch':False}


def command(instance,action,stream=None):
    from ..pilot_installation import administrative_binding
    from .. import pilot_operations as pilot
    require(action in ('status','limit','request','bookings'),'invalid_managed_action')
    require((stream is not None)==(action in ('limit','request','bookings')),'managed_input_required')
    target,_,record=administrative_binding(instance)
    pilot._service_command(target,record,['--pilot-check'])
    arguments=['--managed-action',action];data=None
    if stream is not None:
        if action=='bookings':
            data=canonical(decode(stream.read(2049),maximum=2048))
        else:
            data=canonical(decode(stream.read(65537)))
        arguments.append('--managed-input')
    return pilot._service_command(target,record,arguments,input_data=data,timeout=60)


def _desired(root,uid):
    from ..ratings.operations import _identity
    from .policy import state
    with _identity(uid):
        store=state(root)
        return store.snapshot(),store.pending(),store.intent_sequence()


def startup_signature(marker,record,caps,intent_sequence,*,boot,recovery=None,booking_leases=None):
    """Failure/progress observations must not grant another startup attempt."""
    return fingerprint({'binding':{k:marker[k] for k in ('installation_id','generation')},
        'runtime':record['bundle_sha256'],'intent_sequence':intent_sequence,
        'max_concurrent_rooms':caps['max_concurrent_rooms'],'provider_room_limit':caps['provider_room_limit'],
        'recovery':recovery.get('transaction_id') if recovery and recovery.get('phase')=='committed' else None,
        'boot':boot,**({'bookings':booking_leases}if booking_leases else{})})


def settle_closed(root,journal):
    """Called as service UID under the stopped owner lease; zero provider I/O."""
    from .policy import state
    from .projection import export
    store=state(root);store.reconcile_closed_journal(journal or {'format':1,'rooms':{}})
    for value in store.pending():
        if value['action']!='close':continue
        row=store.room(value['alias'])
        if row['room_id'] is not None or row['phase']=='uncertain':continue
        result=store.begin(value['id'],ready=True)
        if result['phase']=='applying':
            export(store)
            store.finish(value['id'],'applied',result={'application':'lifecycle','room_id':None,
                         'revision':store.room(value['alias'])['revision']})


def _refresh_recovery_pin(instance, record):
    """Refresh legacy independent code only after native work has settled.

    Normal already-current ticks read one protected control file; inventory and
    ancestry checks run only when a real pin change is needed. This maintenance
    outcome cannot grant lifecycle authority or interrupt healthy workers.
    """
    from ..provisioning import _control, protected
    from ..rescue.operations import refresh_pin
    path=Path(record['prefix'])/'recovery/configuration.json'
    if not os.path.lexists(path):return {'status':'absent','pin_refreshed':False}
    try:
        config=_control(protected(path),private=True,maximum=512*1024)
        if type(config)is dict and config.get('record')==record:
            return {'status':'current','pin_refreshed':False}
        return refresh_pin(instance,record)
    except Exception:
        return {'status':'refused','reason':'recovery_policy_unavailable','pin_refreshed':False}


def tick(instance):
    """Only native guarded start is privileged. Provider mutations stay native.

    One start attempt per intent/boot/evidence. Failed starts require new explicit
    intent or native approved recovery, rather than an endless creation loop.
    """
    from ..pilot_installation import administrative_binding,_stopped
    from .. import pilot_operations as pilot
    from ..provisioning import _control,protected
    from ..network import operations as network
    from .policy import NAME
    target,marker,record=administrative_binding(instance);prefix=target['prefix'];uid=record['service_uid']
    declaration=_control(target['root']/NAME,uid,private=True,optional=True)
    require(declaration is not None and declaration.get('policy_version')==1,'managed_policy_required')
    directory=protected(prefix/'managed')
    with file_lease(prefix/'deployment-admission.lock'),file_lease(directory/'reconcile.lock'):
        # Commit owns all lifecycle work. Queued/held work still permits the
        # independent network agent to deliver status and administration.
        job=_control(prefix/'controller-update.json',optional=True)
        if job is not None and job.get('phase') in ('committing','recovering','retrying'):
            return {'ok':True,'phase':'update-in-progress','rooms_started':False}
        if (job is not None and job.get('phase')=='held'
                and job.get('after',{}).get('bundle_sha256')==record['bundle_sha256']):
            from .upgrade import network_safe
            if not network_safe(target,job):
                return {'ok':True,'phase':'migration-recovery-required','rooms_started':False,
                        'administration_agent_checked':False,'native_recovery_channel_required':True}
        from .topology import pending as topology_pending,initialize as initialize_topology
        if topology_pending(target):
            initialize_topology(target,record,instance)
        pilot._unit(instance,optional=True);network._checked_unit(instance)
        network_state=network._state(instance)
        if network_state['ActiveState'] in ('inactive','failed') and network_state['MainPID']==0:
            # Independent agent stays reachable even when intent says all off.
            status=pilot._service_command(target,record,['--network-action','status'])
            if status.get('enrolled') and status.get('pending_operation') is None:
                pilot._systemctl('start',network.name(instance),timeout=25)
        if job is not None and job.get('phase') not in ('complete','cancelled'):
            return {'ok':True,'phase':'update-in-progress','rooms_started':False,
                    'administration_agent_checked':True}
        # A terminal native update can leave a much older independently pinned
        # reader behind. Refresh through the registered successor after the old
        # callback releases its policy lock; never repin during a commit.
        recovery_pin=_refresh_recovery_pin(instance,record)
        if recovery_pin.get('status')=='refused':
            import logging
            logging.getLogger(__name__).warning('independent recovery pin refresh requires review')
        from .catalog_rollout import prepare_pending
        _,catalog_requests,_=_desired(target['root'],uid)
        prepare_pending(target,record,catalog_requests)
        service=pilot._service_state(instance)
        if service['MainPID'] or service['ActiveState'] in ('active','activating','deactivating'):
            return {'ok':True,'phase':'controller-running','rooms_started':False,'recovery_pin':recovery_pin}
        from ..ratings.operations import _identity
        with _stopped(target['root'],uid):
            journal=_control(target['root']/'state/rolling/ownership.json',uid,private=True,optional=True)
            with _identity(uid):settle_closed(target['root'],journal)
        caps,pending,intent_sequence=_desired(target['root'],uid)
        if caps['policy_version']!=1:return {'ok':True,'phase':'policy-migration-pending','rooms_started':False}
        intent=any(r['desired']=='running' for r in caps['rooms']) and min(caps['max_concurrent_rooms'],caps['provider_room_limit'])>0
        from ..bookings.policy import startup_leases
        with _identity(uid):bookings=startup_leases(target['root'])
        if not pending and not intent and not bookings:return {'ok':True,'phase':'intentionally-stopped','rooms_started':False,'recovery_pin':recovery_pin}
        if pilot._unit(instance,optional=True) is None:
            return {'ok':False,'phase':'unit-preparation-required','rooms_started':False}
        recovery=_control(target['root']/'state/ownership-recovery.json',uid,private=True,optional=True)
        signature=startup_signature(marker,record,caps,intent_sequence,recovery=recovery,booking_leases=bookings,
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        path=directory/'start-attempt.json';old=_control(path,private=True,optional=True)
        if old is not None and old.get('signature')==signature:
            return {'ok':False,'phase':'startup-requires-review','rooms_started':False}
        from ..provisioning import _write
        _write(path,{'format':1,'signature':signature,'at':time.time(),'phase':'attempting'},private=True)
        try:
            result=pilot.automatic_start(instance,target['root'])
        except Exception:
            _write(path,{'format':1,'signature':signature,'at':time.time(),'phase':'uncertain'},private=True)
            return {'ok':False,'phase':'startup-requires-review','rooms_started':False}
        _write(path,{'format':1,'signature':signature,'at':time.time(),'phase':'started'},private=True)
        return {'ok':result.get('ok') is True,'phase':'controller-started','controller_started':True,
                'rooms_started':False,'room_creation_managed_by_controller':True}


def ensure(instance,record):
    """Install scoped helper and enable required policy after enrollment.

    Called by the verified new runtime's network-start/upgrade path. This does
    not start the bot or reset updater trust, ownership or operator ceilings.
    """
    from ..pilot_installation import paths
    from ..provisioning import _control,_write,protected
    from .. import pilot_operations as pilot
    from ..network import operations as network
    from .policy import prepare
    from ..catalog_publication import _write as write_code
    target=paths(instance);uid=record['service_uid'];marker=_control(target['root']/'.poolrotater-host.json',uid,private=True)
    from ..update_helpers import managed_update_in_progress
    defer_timer_activation=managed_update_in_progress(instance,target,record,marker)
    prepare(target['root'],marker,uid=uid)
    directory=target['prefix']/'managed';directory.mkdir(mode=0o700,exist_ok=True);protected(directory)
    with file_lease(directory/'installation.lock'):
        source=protected(Path(record['release'])/'poolrotater_host/managed_bootstrap.py')
        launcher=target['prefix']/'managed-bootstrap.py';pin=hashlib.sha256(source.read_bytes()).hexdigest()
        config={'format':1,**{k:record[k] for k in ('installation_id','generation','service_uid')},'launcher_sha256':pin}
        old=_control(directory/'configuration.json',private=True,optional=True)
        if old is not None:
            require(all(old.get(k)==config[k] for k in ('format','installation_id','generation','service_uid')),'managed_helper_binding_changed')
            require(hashlib.sha256(protected(launcher).read_bytes()).hexdigest() in (old['launcher_sha256'],pin),'managed_launcher_changed')
        elif os.path.lexists(launcher):raise ConfigError('unregistered managed launcher')
        write_code(launcher,source.read_bytes(),mode=0o644,replace=True);_write(directory/'configuration.json',config,private=True)
        name='poolrotater-v7-managed-'+instance
        service=f'''[Unit]
Description=PoolRotater managed host reconciliation ({instance})
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3.12 -I -S -B {launcher} --instance {instance}
WorkingDirectory=/tmp
Nice=17
CPUQuota=50%
UMask=0077
MemoryMax=512M
MemorySwapMax=128M
TasksMax=32
TimeoutStartSec=900
TimeoutStopSec=30
KillMode=control-group
SendSIGKILL=yes
LimitCORE=0
Environment=LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2
'''
        timer=f'''[Unit]
Description=PoolRotater managed host reconciliation timer ({instance})

[Timer]
OnBootSec=90
OnUnitInactiveSec=15
RandomizedDelaySec=3
Unit={name}.service

[Install]
WantedBy=timers.target
'''
        for suffix,text in (('.service',service),('.timer',timer)):
            path=pilot.UNIT_BASE/(name+suffix)
            existing=_control(directory/('unit'+suffix+'.json'),private=True,optional=True)
            if os.path.lexists(path):
                require(existing is not None and hashlib.sha256(protected(path).read_bytes()).hexdigest()
                        in (existing['sha256'],hashlib.sha256(text.encode()).hexdigest()),'managed_unit_changed')
            write_code(path,text.encode(),mode=0o644,replace=True)
            _write(directory/('unit'+suffix+'.json'),{'sha256':hashlib.sha256(text.encode()).hexdigest()},private=True)
        # Preserve pinned release trust and channel; upgrade only policy choices.
        update_path=target['prefix']/'updates/cache/policy.json'
        choices=_control(protected(update_path),private=True)
        from ..updates.agent import policy
        policy(choices,choices['binding'])
        enabled={**choices,'automatic_download':True,'automatic_install':True,'controller_updates':True,
                 'paused':False,'report_to_hub':True}
        _write(update_path,enabled,private=True)
        network._checked_unit(instance)
        pilot._systemctl('daemon-reload')
        pilot._systemctl('enable',network.name(instance))
        if defer_timer_activation:
            # The admitted native update owns the stopped configuration window.
            # Enabling is safe; starting the independent helper is deferred to
            # the update's existing timer-restoration path.
            pilot._systemctl('enable',name+'.timer')
        else:
            pilot._systemctl('enable','--now',name+'.timer')
        _write(target['root']/'state/managed-root-policy.json',{'format':1,'policy_version':1,
            **{k:marker[k] for k in ('installation_id','generation')},'automatic_updates':True,'administration_helper':True},
            owner=uid,private=True)
    return {'ok':True,'managed_helper_installed':True,'rooms_started':False}
