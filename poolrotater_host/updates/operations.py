"""Explicit root administration for the independent update timer."""
import hashlib
import os
from pathlib import Path
import time

from ..catalog_delivery import HttpsOrigin
from ..layout import atomic_json, file_lease
from ..pilot_installation import administrative_binding
from ..provisioning import _control, protected
from .agent import Agent, configure
from .client import Client
from .contracts import canonical, decode, digest, fields, fingerprint, require, root
from .native import Backend

ACTIONS=('configure','check','download','apply','pause','resume','status','tick','recover',
         'unit','start','stop','cleanup')


def binding(instance):
    from ..network.operations import command
    target,marker,parent=administrative_binding(instance)
    network=command(instance,'status')
    require(network.get('enrolled') is True and network.get('account_binding_matches') is True
            and network.get('pending_operation') is None
            and network.get('credential_expires_at',0)>time.time(), 'update_enrollment_required')
    return target,parent,{**{k:marker[k]for k in ('installation_id','generation')},'host_id':network['host_id']}


def client(instance):
    target,parent,bound=binding(instance)
    directory=protected(target['prefix']/'updates')
    config=_control(protected(directory/'configuration.json'),private=True)
    fields(config,('format','binding','origin','ca_sha256','bootstrap_sha256'))
    require(config['format']==1 and config['binding']==bound,'update_installation_binding_required')
    bootstrap=_control(protected(directory/'bootstrap.json'),private=True)
    require(fingerprint(bootstrap)==config['bootstrap_sha256'],'update_bootstrap_digest_mismatch')
    cache=directory/'cache';cache.mkdir(mode=0o700,exist_ok=True);protected(cache)
    origin=HttpsOrigin(config['origin'],cache/'transport',ca_file=protected(directory/'ca.pem'),
                       ca_sha256=config['ca_sha256'],timeout=5,deadline=30)
    return Client(cache,canonical(bootstrap),bootstrap_sha256=config['bootstrap_sha256'],origin=origin,binding=bound)


def command(instance,action,stream=None,operation_id=None,decision=None):
    require(action in ACTIONS,'update_action_refused')
    require((operation_id is None)==(decision is None) and (decision is None or action=='recover'),'update_recovery_identity_required')
    require((stream is not None)==(action=='configure'),'update_configuration_input_required')
    if action in ('unit','start','stop'):return unit(instance,action)
    if action=='configure':
        return configure_instance(instance,decode(stream.read(256*1024+1)))
    configured=client(instance)
    if action=='cleanup':
        from .retention import cleanup
        return cleanup(configured,Backend(instance,configured))
    agent=Agent(configured,Backend(instance,configured))
    result=agent.resolve(operation_id,decision)if decision is not None else agent.step(action)
    if action!='status':
        result['hub_report']=publish_report(instance,configured,result)
    if action=='tick':result['cleanup']=scheduled_cleanup(instance,configured)
    return result


def scheduled_cleanup(instance,configured):
    from .client import read
    path=configured.root/'cleanup-schedule.json'
    try:
        value=read(path,optional=True)or{'next_attempt_at':0}
        if time.time()<value['next_attempt_at']:return 'scheduled'
        from .retention import cleanup
        cleanup(configured,Backend(instance,configured))
        atomic_json(path,{'next_attempt_at':time.time()+86400})
        return 'complete'
    except Exception:
        # A busy admission/staging lock or retained suspicious file must never
        # relabel a valid deployment or touch its service. Retry is bounded.
        try:atomic_json(path,{'next_attempt_at':time.time()+3600})
        except Exception:pass  # Even an unwritable diagnostic cannot stop rooms.
        return 'deferred; use update-cleanup for local inspection'


def validate_configuration(data,bound):
    """Validate consent/trust/TLS/origin before publishing persistent authority."""
    import tempfile
    from .agent import policy
    data={**{'channel':'pilot','automatic_download':True,'automatic_install':True,
             'controller_updates':True,'paused':False,'check_seconds':1800,'report_to_hub':True},**data}
    fields(data,('origin','ca_pem','ca_sha256','bootstrap','bootstrap_sha256','channel',
                 'automatic_download','automatic_install','controller_updates','paused','check_seconds','report_to_hub'))
    root(data['bootstrap']);digest(data['bootstrap_sha256']);digest(data['ca_sha256'])
    require(fingerprint(data['bootstrap'])==data['bootstrap_sha256'],'update_bootstrap_digest_mismatch')
    require(time.time()<data['bootstrap']['expires_at'],'update_bootstrap_expired')
    require(type(data['ca_pem'])is str,'update_ca_digest_mismatch')
    try:ca=data['ca_pem'].encode('ascii')
    except UnicodeError:require(False,'update_ca_digest_mismatch')
    require(hashlib.sha256(ca).hexdigest()==data['ca_sha256'],'update_ca_digest_mismatch')
    choices={k:data[k]for k in ('channel','automatic_download','automatic_install',
                               'controller_updates','paused','check_seconds','report_to_hub')}
    policy({'format':1,'binding':bound,**choices},bound)
    # The existing transport owns canonical URL, CA syntax, digest and namespace
    # checks. Its constructor does no network I/O. Disposable validation cannot
    # pin an invalid URL/CA or partially enable consent in the installed prefix.
    with tempfile.TemporaryDirectory(prefix='poolrotater-update-preflight-')as temporary:
        probe=Path(temporary);path=probe/'ca.pem';path.write_bytes(ca);path.chmod(0o600)
        HttpsOrigin(data['origin'],probe/'transport',ca_file=path,ca_sha256=data['ca_sha256'],timeout=5,deadline=30)
    return choices,ca


def configure_instance(instance,data):
    target,parent,bound=binding(instance)
    choices,ca=validate_configuration(data,bound)
    expected={'format':1,'binding':bound,'origin':data['origin'],
              'ca_sha256':data['ca_sha256'],'bootstrap_sha256':data['bootstrap_sha256']}
    directory=target['prefix']/'updates';directory.mkdir(mode=0o700,exist_ok=True);protected(directory)
    with file_lease(directory/'configuration.lock'):
        old=_control(directory/'configuration.json',private=True,optional=True)
        require(old is None or old==expected,'update_trust_replacement_requires_review')
        existing=_control(directory/'bootstrap.json',private=True,optional=True)
        require(existing is None or existing==data['bootstrap'],'update_trust_replacement_requires_review')
        from ..catalog_publication import _write
        _write(directory/'ca.pem',ca,mode=0o600)
        if existing is None:atomic_json(directory/'bootstrap.json',data['bootstrap'])
        cache=directory/'cache';cache.mkdir(mode=0o700,exist_ok=True);protected(cache)
        origin=HttpsOrigin(data['origin'],cache/'transport',ca_file=directory/'ca.pem',
                          ca_sha256=data['ca_sha256'],timeout=5,deadline=30)
        configured=Client(cache,canonical(data['bootstrap']),bootstrap_sha256=data['bootstrap_sha256'],
                          origin=origin,binding=bound)
        configure(configured,**choices)
        # Publish last. A crash before this write leaves an unactivated staging
        # namespace that the same explicit, valid configuration may complete.
        atomic_json(directory/'configuration.json',expected)
    return {'ok':True,'configured':True,'automatic_install':choices['automatic_install'],
            'timer_started':False,'live_rooms_affected':False}


def publish_report(instance,configured,summary):
    from .agent import policy
    from .client import read
    from .telemetry import projection
    import io
    consent=policy(read(configured.root/'policy.json'),configured.binding)
    if not consent['report_to_hub']:return 'disabled'
    path=configured.root/'telemetry.json'
    try:
        with file_lease(configured.root/'telemetry.lock'):
            value=read(path,optional=True)or{'next_sequence':1,'pending':None,'received_at':0}
            if value['pending']is not None and time.time()-value['pending']['sent_at']>240:
                # A later status supersedes an old diagnostic observation; this
                # does not replay or alter an installation operation.
                value['pending']=None
            if value['pending']is None:
                if time.time()-value['received_at']<60:return 'scheduled'
                value['pending']=projection(summary,sequence=value['next_sequence'],now=time.time())
                value['next_sequence']+=1;atomic_json(path,value)
            from ..network.operations import command as network_command
            receipt=network_command(instance,'update-report',stream=io.BytesIO(canonical(value['pending'])))
            require(receipt.get('ok')is True and receipt.get('sequence')==value['pending']['sequence'],
                    'update_telemetry_receipt_refused')
            value.update(pending=None,received_at=receipt['received_at']);atomic_json(path,value)
        return 'acknowledged'
    except Exception:
        # Diagnostics failure is independent from installation outcome and must
        # not reach a native room lifecycle action or expose transport text.
        return 'pending'


def unit_text(instance):
    from ..pilot_installation import paths
    target=paths(instance)
    # Separate oneshot process and no bot unit dependency: a failed update check
    # cannot stop rooms or the network/ratings agent. Controller commit remains
    # the native, independently gated operation.
    return f'''[Unit]
Description=PoolRotater opt-in update check ({instance})
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/tmp
ExecStart=/usr/bin/python3.12 -I -S -B {target['prefix']}/updates-bootstrap.py --instance {instance}
UMask=0077
Nice=15
MemoryMax=1G
MemorySwapMax=512M
TasksMax=64
TimeoutStartSec=600
TimeoutStopSec=30
KillMode=control-group
SendSIGKILL=yes
LimitCORE=0
Environment=LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2
'''


def unit(instance,action):
    from ..pilot_operations import UNIT_BASE,_systemctl
    target,_,parent=administrative_binding(instance)
    name='poolrotater-v7-updates-'+instance
    timer=f'''[Unit]
Description=PoolRotater independent update polling ({instance})

[Timer]
OnBootSec=90
OnUnitInactiveSec=30
RandomizedDelaySec=15
Unit={name}.service

[Install]
WantedBy=timers.target
'''
    if action=='unit':
        client(instance) # Explicit configured enrollment/trust required first.
        source=protected(Path(parent['release'])/'poolrotater_host/update_bootstrap.py')
        from ..catalog_publication import _write
        launcher=target['prefix']/'updates-bootstrap.py'
        _write(launcher,source.read_bytes(),mode=0o644)
        for suffix,raw in (('.service',unit_text(instance)),('.timer',timer)):
            _write(UNIT_BASE/(name+suffix),raw.encode(),mode=0o644)
        _systemctl('daemon-reload')
        return {'ok':True,'installed':True,'timer_started':False,'live_rooms_affected':False}
    for suffix,raw in (('.service',unit_text(instance)),('.timer',timer)):
        path=protected(UNIT_BASE/(name+suffix));require(path.read_text()==raw,'update_unit_changed')
    if action=='start':
        configured=client(instance)
        # Starting polling does not silently grant download/install consent.
        from .agent import policy
        from .client import read
        policy(read(configured.root/'policy.json'),configured.binding)
        _systemctl('enable','--now',name+'.timer')
    else:
        # Disable future ticks. An already committing update must settle or use
        # explicit native recovery; killing it here could strand ownership.
        _systemctl('disable','--now',name+'.timer')
    return {'ok':True,'timer_started':action=='start','live_rooms_affected':False}
