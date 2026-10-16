"""One-time protected controller upgrade at a gated, all-rooms-empty window.

No OAuth or provider calls. Admission uses the running workers' existing private
maintenance gate and fresh independent broker/worker roster observations. Any
late join cancels admission. Interrupted commits require explicit review.
"""
import hashlib
import math
import os
from pathlib import Path
import secrets
import time
import uuid

from .config import ConfigError
from .layout import file_lease, atomic_json
from .provisioning import _control, _write, _provision, protected
from .pilot_installation import RESERVE
from .pilot_upgrade import _binding, require_repair_compatible
from .runtime_lifecycle import validate_runtime

NAME = 'controller-update.json'
GATE = 'state/rolling/supervisor-maintenance.json'
EMPTY_SECONDS = 30
DRAIN_SECONDS = 15
GATE_SECONDS = 300
# Two minute-spaced background observations must fit inside the native gate lease.
ADMISSION_SECONDS = 240


def _config_hashes(root):
    if any(p.is_symlink() for p in (root/'config').rglob('*')):
        raise ConfigError('configuration contains an unreviewed link')
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / 'config').rglob('*')) if p.is_file() and not p.is_symlink()}


def _process(pid):
    if type(pid) is not int or pid <= 0: raise ConfigError('controller must be running before it can be queued')
    raw = Path('/proc') / str(pid) / 'stat'
    return {'pid':pid, 'start':raw.read_text().rpartition(')')[2].split()[19],
            'boot':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def _retired_worker_target(target, parent, alias, row):
    """A cancelled predecessor's diagnostic target is not current rollout intent."""
    import re
    prefix=target['prefix']
    if os.path.lexists(prefix/'worker-deployment.json'):
        return False
    wanted=row.get('target');version=row.get('target_version')
    if (not isinstance(wanted,str) or not isinstance(version,str)
            or not re.fullmatch(r'v7\.[0-9]+\.[0-9]+',version)):
        return False
    path=Path(wanted)
    if path.parent!=prefix/'releases':return False
    folder=prefix/'retired-worker-deployments'
    if not folder.exists():return False
    protected(folder)
    records=list(folder.glob('*.json'))
    if len(records)>64:return False
    for receipt in records:
        job=_control(protected(receipt))
        digest=job.get('candidate_sha256')
        if (job.get('format')==1 and job.get('phase')=='cancelled'
                and job.get('installation_id')==parent['installation_id']
                and job.get('generation')==parent['generation']
                and job.get('parent_sha256')!=parent['bundle_sha256']
                and alias in job.get('rooms',[])
                and isinstance(digest,str) and re.fullmatch('[a-f0-9]{64}',digest)
                and path.name==version+'-'+digest[:16]):
            return True
    return False



def _rows(target, parent, *, now, after=None):
    # Native status uses 0640 inside the checked private 0700 state tree.
    # It is an observation, not a secret/ownership-authority record.
    value = _control(target['root'] / 'state/rolling/status.json', parent['service_uid'])
    generated = value.get('generated_at')
    if (type(generated) not in (int,float) or not 0 <= now-generated <= 8
            or after is not None and generated <= after): return None
    rooms = value.get('rooms')
    if type(rooms) is not dict or not rooms: return None
    for alias,row in rooms.items():
        status = row.get('status', {})
        age = row.get('heartbeat_age')
        if (row.get('phase') != 'running' or row.get('healthy') is not True
                or row.get('error') or row.get('operation') != 'owned'
                or row.get('ownership_unknown') is not False
                or (row.get('target') and not _retired_worker_target(target,parent,alias,row))
                or row.get('admission_locked') or row.get('roster_known') is not True
                or type(row.get('observed_players')) is not int or row['observed_players'] != 0 or type(age) not in (int,float)
                or not math.isfinite(age) or age < 0 or age + now-generated > 8
                or not status.get('ready') or not status.get('running')
                or status.get('state') != 'IDLE' or type(status.get('players')) is not int or status['players'] != 0
                or not row.get('room_id') or row['room_id'] != status.get('room_id')):
            return None
    return rooms


def _same_rooms(rows, job):
    return (rows is not None and {a:r['room_id'] for a,r in rows.items()} == job.get('room_ids')
            and identity(rows) == job.get('worker_identity'))


def identity(rows):
    # Retain the v6 maintenance controller's process/generation/room/path check,
    # now bound to the protected v7 controller registration as well.
    return {a:[r.get(k) for k in ('pid','generation','room_id','release')] for a,r in rows.items()}


def _gated(rows, job):
    if not _same_rooms(rows, job): return False
    return all(type(r.get('empty_seconds')) in (int,float) and r['empty_seconds'] >= 2
        and r.get('status',{}).get('paused') is True
        and r.get('status', {}).get('maintenance') == {
        'nonce':job['nonce'], 'locked':True, 'uncertain':False, 'error':None}
        for r in rows.values())


def journal_matches(target,parent,job):
    from .reconciliation import _journal
    value=_journal(_control(target['root']/'state/rolling/ownership.json',
        parent['service_uid'],private=True),set(job['rooms']))
    return (identity(value['rooms']) == job['worker_identity'] and all(
        r.get('phase')=='running' and r.get('operation')=='owned'
        and r.get('ownership_unknown') is False for r in value['rooms'].values()))


def _gate(target, parent, job, phase):
    from .ratings.operations import _identity
    with _identity(parent['service_uid']):
        path = target['root'] / GATE
        current = _control(path, parent['service_uid'], private=True, optional=True)
        if current is not None and current.get('nonce') != job.get('nonce'):
            raise ConfigError('another maintenance request is present')
        if phase is None:
            path.unlink(missing_ok=True)
        else:
            atomic_json(path, {'format':1, 'phase':phase, 'nonce':job['nonce'],
                'expires_at':time.time()+GATE_SECONDS, 'bridge_release':job['before']['release']})


def queue(instance, bundle, sha256, *, from_sha256, deployment_id=None, release_approval_required=False):
    if type(release_approval_required)is not bool:raise ConfigError('invalid release approval requirement')
    from .pilot_installation import administrative_binding
    from .pilot_operations import _service_state
    from .network.operations import _state
    target, marker, parent = administrative_binding(instance)
    if sha256 == from_sha256:
        raise ConfigError('controller update requires a distinct replacement')
    with file_lease(target['prefix'] / 'deployment-admission.lock'), file_lease(target['prefix'] / 'controller-update-admin.lock'):
        # A previously committing updater can switch registration while this
        # caller waits for admission. Bind again under the same native leases
        # rather than queueing from the predecessor observed before the wait.
        locked_target, marker, parent = administrative_binding(instance)
        if locked_target != target:
            raise ConfigError('controller installation changed during admission')
        path = target['prefix'] / NAME
        old = _control(path, optional=True)
        if deployment_id is not None:
            try:
                if str(uuid.UUID(deployment_id)) != deployment_id: raise ValueError
            except (ValueError,TypeError,AttributeError):
                raise ConfigError('invalid controller deployment request identity') from None
            if old and old.get('id') == deployment_id:
                same_parent=(old.get('before')==parent or old.get('phase')=='complete'and old.get('after')==parent)
                if (not same_parent or old.get('before',{}).get('bundle_sha256')!=from_sha256
                        or old.get('after',{}).get('bundle_sha256') != sha256
                        or old.get('release_approval_required',False)!=release_approval_required):
                    raise ConfigError('controller deployment request identity conflict')
                return {'ok':True,'phase':old['phase'],'id':old['id'],
                        'services_restarted':False,'all_rooms_empty_required':True}
        if parent['bundle_sha256'] != from_sha256:
            raise ConfigError('queued controller starting digest differs')
        if old and old.get('phase') not in ('complete','cancelled'):
            raise ConfigError('a controller update already needs completion or review')
        native = _control(target['root'] / 'state/rolling/status.json', parent['service_uid'])
        from .ratings.operations import _identity
        from .layout import load
        with _identity(parent['service_uid']): _, host = load(target['root'])
        aliases = {r['alias'] for r in host['rooms']}
        if set(native.get('rooms', {})) != aliases:
            raise ConfigError('controller upgrade requires every configured room')
        from .updates.native import require_idle_worker_deployment, require_registered_workers
        require_idle_worker_deployment(target, parent)
        require_registered_workers(parent, native['rooms'])
        if os.path.lexists(target['root'] / GATE):
            raise ConfigError('existing maintenance request must be resolved first')
        staged = _provision(target['root'], target['prefix'], Path(bundle), sha256,
            uid=parent['service_uid'], execution=parent['execution'], reserve=RESERVE, stage_bootstrap=True)
        candidate = _control(target['prefix'] / 'candidates' / marker['installation_id'] / (sha256 + '.json'))
        new = validate_runtime(candidate, target['prefix'])
        from .updates.approval import require_upgrade
        if release_approval_required:
            from .updates.approval import require_approved
            require_approved(validate_runtime(parent,target['prefix'])['manifest'],new['manifest'],parent,candidate)
        else:
            require_upgrade(validate_runtime(parent, target['prefix'])['manifest'], new['manifest'], parent, candidate)
        # Stop/upgrade later rechecks both immutable artifacts. Keep the input
        # under root custody, so a user's zip cannot change while the job waits.
        archive = target['prefix'] / ('controller-' + sha256 + '.zip')
        if archive.exists():
            protected(archive)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != sha256: raise ConfigError('queued archive changed')
        else:
            import shutil
            with open(bundle, 'rb') as source, archive.open('xb') as out:
                shutil.copyfileobj(source, out); out.flush(); os.fsync(out.fileno())
            archive.chmod(0o444)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != sha256:
                raise ConfigError('queued archive differs from staged candidate')
        service = _service_state(instance); network = _state(instance)
        job = {'format':1, 'id':deployment_id or str(uuid.uuid4()), 'phase':'waiting_empty',
            'before':parent, 'after':candidate, 'archive':str(archive),
            'process':_process(service['MainPID']), 'rooms':sorted(aliases),
            'configuration':_config_hashes(target['root']), 'service_enabled':service['UnitFileState']=='enabled',
            'network_active':network['ActiveState']=='active', 'network_enabled':network['UnitFileState']=='enabled',
            'queued_at':time.time(), 'empty_since':None, 'reason':'waiting_for_all_rooms_empty'}
        if release_approval_required:job['release_approval_required']=True
        _write(path, job)
        return {'ok':True, 'phase':job['phase'], 'id':job['id'], 'release':staged['release'],
                'services_restarted':False, 'all_rooms_empty_required':True}


def step(instance):
    """Short timer iteration. A clean idle window may commit once; never retry a
    partially executed stop/upgrade/start transaction without a human review.
    """
    from .pilot_operations import _service_state, _systemctl
    target, marker, _, current = _binding(instance)
    with file_lease(target['prefix'] / 'controller-update-admin.lock'):
        path = target['prefix'] / NAME; job = _control(path)
        if job.get('phase') in ('complete','cancelled','held'):
            disarm(instance)
            return {'ok':True, 'phase':job['phase'], 'reason':job.get('reason')}
        def save(**values):
            job.update(values); _write(path, job)
        if job.get('phase') not in ('waiting_empty','locking'):
            save(phase='held', reason='interrupted_commit_requires_review')
            return {'ok':False,'phase':'held','reason':job['reason']}
        parent = job['before']
        try: same_process=_process(_service_state(instance)['MainPID']) == job['process']
        except (ConfigError, OSError): same_process=False
        if current != parent or not same_process:
            save(phase='held', reason='controller_identity_changed')
            return {'ok':False,'phase':'held','reason':job['reason']}
        if _config_hashes(target['root']) != job['configuration']:
            if job['phase']=='locking': _gate(target,parent,job,None)
            save(phase='held', reason='configuration_changed_since_queue')
            return {'ok':False,'phase':'held','reason':job['reason']}
        # Long empty-room waits never extend a release approval's expiry.
        # Recheck before admission/stop; interrupted commits use explicit native
        # recovery rather than a new unattended expiry exception.
        try:
            from .updates.approval import require_upgrade,require_approved
            check=require_approved if job.get('release_approval_required') else require_upgrade
            check(validate_runtime(parent,target['prefix'])['manifest'],
                  validate_runtime(job['after'],target['prefix'])['manifest'],parent,job['after'])
        except ConfigError:
            if job['phase']=='locking': _gate(target,parent,job,None)
            save(phase='held',reason='controller_approval_expired_or_incompatible')
            return {'ok':False,'phase':'held','reason':job['reason']}
        now=time.time(); rows=_rows(target,parent,now=now)
        if rows is not None and set(rows) != set(job['rooms']): rows=None
        if job['phase']=='waiting_empty':
            if rows is None or any(r['status'].get('paused') for r in rows.values()):
                save(empty_since=None,reason='waiting_for_all_rooms_empty')
            elif job['empty_since'] is None or not _same_rooms(rows,job) or now < job['empty_since']:
                save(empty_since=now,room_ids={a:r['room_id']for a,r in rows.items()},
                     worker_identity=identity(rows),reason='observing_continuous_empty_window')
            elif now-job['empty_since']>=EMPTY_SECONDS and all(
                    type(r.get('empty_seconds')) in (int,float) and r['empty_seconds']>=EMPTY_SECONDS for r in rows.values()):
                save(phase='locking',nonce=secrets.token_hex(16),lock_started=now,locked_since=None,
                     reason='waiting_for_private_admission_and_late_join_drain')
                _gate(target,parent,job,'locking')
        else:
            maintenance=[r.get('status',{}).get('maintenance',{})for r in (rows or {}).values()]
            if any(m.get('uncertain') for m in maintenance):
                save(phase='held',reason='maintenance_outcome_requires_review')
            elif rows is None or not _same_rooms(rows,job) or now-job['lock_started']>ADMISSION_SECONDS:
                _gate(target,parent,job,None)
                save(phase='waiting_empty',empty_since=None,locked_since=None,reason='admission_cancelled_roster_changed_or_timed_out')
            elif not _gated(rows,job):
                save(locked_since=None)
            elif job.get('locked_since') is None:
                save(locked_since=now)
            elif now-job['locked_since']>=DRAIN_SECONDS:
                validate_runtime(parent,target['prefix']); validate_runtime(job['after'],target['prefix'])
                # Full hashing may take time. Require a NEW, current worker and
                # broker observation after hashing, with the same locked nonce.
                barrier=time.time()
                deadline=time.monotonic()+12
                latest=None
                while time.monotonic()<deadline:
                    observation=_rows(target,parent,now=time.time())
                    if not _gated(observation,job):break
                    latest=_rows(target,parent,now=time.time(),after=barrier)
                    if _gated(latest,job):break
                    time.sleep(.25)
                if not _gated(latest,job):
                    _gate(target,parent,job,None)
                    save(phase='waiting_empty',empty_since=None,locked_since=None,reason='late_join_cancelled_commit')
                else:
                    _gate(target,parent,job,'committing')
                    # Refresh after writing the phase too; a late arrival keeps
                    # its old worker and causes all gates to be released.
                    if not _gated(_rows(target,parent,now=time.time()),job):
                        _gate(target,parent,job,None)
                        save(phase='waiting_empty',empty_since=None,locked_since=None,reason='late_join_cancelled_commit')
                    elif not journal_matches(target,parent,job):
                        _gate(target,parent,job,None)
                        save(phase='held',reason='ownership_journal_changed_before_commit')
                    else:
                        save(phase='committing',commit_started_at=time.time(),reason='all_rooms_private_empty_and_drained')
                        try: _commit(instance,target,parent,job,save)
                        except Exception:
                            save(phase='held',reason='commit_requires_review_retain_all_state')
                            raise
        return {'ok':True,'phase':job['phase'],'reason':job.get('reason')}


def _commit(instance,target,parent,job,save):
    from .pilot_operations import _systemctl, lifecycle
    from .network import operations as network
    from .pilot_upgrade import upgrade
    # No mutation or token I/O outside the already-running services. Native
    # shutdown confirms closures before releasing the provider owner lease.
    if job.get('release_approval_required'):
        # Hashing/admission may span expiry. New commit authority is fresh at
        # the first lifecycle mutation even if legacy manual repair would admit
        # this same-version artifact. An interrupted commit stays held.
        from .updates.approval import require_approved
        require_approved(validate_runtime(parent,target['prefix'])['manifest'],
                         validate_runtime(job['after'],target['prefix'])['manifest'],parent,job['after'])
    network._checked_unit(instance,optional=not job['network_active'])
    save(commit_admitted_at=time.time())
    if job['network_enabled']: _systemctl('disable',network.name(instance))
    if job['network_active']: network.unit(instance,'stop')
    _systemctl('stop',target['unit'],timeout=100); _systemctl('disable',target['unit'])
    from .pilot_installation import _stopped,require_no_service_processes
    with _stopped(target['root'],parent['service_uid']):
        require_no_service_processes(parent['service_uid'])
    save(services_stopped_at=time.time())
    _gate(target,parent,job,None)
    result=upgrade(instance,job['archive'],job['after']['bundle_sha256'],
                   from_sha256=parent['bundle_sha256'])
    if _config_hashes(target['root']) != job['configuration']:
        raise ConfigError('configuration changed during controller upgrade')
    save(upgrade_transition=result['transition_id'])
    started=lifecycle(instance,'start')
    if not started.get('ok'): raise ConfigError('upgraded controller startup requires review')
    if job['service_enabled']: _systemctl('enable',target['unit'])
    if job['network_active']: network.unit(instance,'start')
    if job['network_enabled']: _systemctl('enable','poolrotater-v7-network-'+instance+'.service')
    save(phase='complete',reason='controller_upgraded_configuration_preserved',completed_at=time.time())
    disarm(instance)


def disarm(instance):
    """Keep audit records; stop the owned timer instead of polling forever."""
    from .pilot_operations import UNIT_BASE,_systemctl
    name='poolrotater-v7-controller-update-'+instance+'.timer'
    path=UNIT_BASE/name
    if path.exists():
        protected(path)
        from .pilot_installation import paths
        job=_control(paths(instance)['prefix']/NAME)
        if job.get('timer_sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
            return  # Never disable a same-named but unrecognized root timer.
        # A failed timer cleanup must not relabel a successfully upgraded bot.
        try:_systemctl('disable','--now',name)
        except ConfigError:
            import logging
            logging.getLogger(__name__).warning('completed/held controller job timer requires cleanup')


def cancel(instance):
    """Cancel waiting admission only. Never undo an interrupted registration."""
    target,_,_,_= _binding(instance)
    with file_lease(target['prefix']/'controller-update-admin.lock'):
        path=target['prefix']/NAME;job=_control(path)
        if job['phase']=='committing' or job.get('commit_started_at') or job.get('upgrade_transition'):
            raise ConfigError('controller commit requires review; cancellation cannot undo it')
        if job.get('nonce'):_gate(target,job['before'],job,None)
        _write(path,{**job,'phase':'cancelled','reason':'operator_cancelled_before_commit'})
        disarm(instance)
        return {'ok':True,'phase':'cancelled','services_restarted':False}


def arm(instance):
    """Install the exact root timer only after queue admission and validation."""
    from .pilot_operations import UNIT_BASE, _systemctl
    target,_,_,_= _binding(instance)
    job=_control(target['prefix']/NAME)
    candidate=validate_runtime(job['after'],target['prefix'])
    name='poolrotater-v7-controller-update-'+instance
    service=f'''[Unit]
Description=PoolRotater guarded controller update ({instance})

[Service]
Type=oneshot
ExecStart=/usr/bin/python3.12 -I -S -B {candidate['release']}/poolrotater_host/controller_update_entry.py {instance}
Nice=15
UMask=0077
MemoryMax=1G
MemorySwapMax=512M
TasksMax=32
TimeoutStartSec=1200
LimitCORE=0
Environment=LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2
'''
    timer=f'''[Unit]
Description=Wait for all PoolRotater rooms to empty ({instance})

[Timer]
OnActiveSec=30
OnUnitInactiveSec=20
AccuracySec=1
Unit={name}.service

[Install]
WantedBy=timers.target
'''
    for suffix,raw in (('.service',service),('.timer',timer)):
        path=UNIT_BASE/(name+suffix)
        if path.exists():
            protected(path)
            if path.read_text()!=raw:raise ConfigError('unrecognized controller update unit')
        else:
            with path.open('x')as out:out.write(raw);out.flush();os.fsync(out.fileno())
            path.chmod(0o644)
    _write(target['prefix']/NAME,{**job,'timer_sha256':hashlib.sha256(timer.encode()).hexdigest()})
    _systemctl('daemon-reload')
    _systemctl('enable','--now',name+'.timer')
    return {'ok':True,'phase':job['phase'],'timer':name+'.timer','rooms_stopped':False}
