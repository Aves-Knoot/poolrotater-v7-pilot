"""One-time protected controller upgrade at a gated, all-rooms-empty window.

No OAuth or provider calls. Admission uses the running workers' existing private
maintenance gate and fresh independent broker/worker roster observations. Any
late join cancels admission. Interrupted commits resume only through evidence-bound native recovery.
"""
import hashlib
import math
import os
from pathlib import Path
import secrets
import time
import uuid

from .config import ConfigError
from .layout import LeaseBusy, file_lease, atomic_json
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


def configuration_retryable(job):
    """Only a never-admitted configuration hold can return to observation."""
    return (job.get('phase') == 'held'
            and job.get('reason') == 'configuration_changed_since_queue'
            and not any(job.get(key) is not None for key in
                        ('commit_started_at', 'commit_admitted_at', 'services_stopped_at',
                         'upgrade_transition', 'startup_attempted_at')))


def _configuration_snapshot(target, parent):
    """Validate current operator settings without materializing or building pools.

    The queued configuration is a migration baseline, not a settings freeze.
    No configuration value is copied back from an older update job.
    """
    from .ratings.operations import _identity
    from .layout import load
    from .runtime_config import render
    from .managed_host.projection import read as managed_projection
    before = _config_hashes(target['root'])
    with _identity(parent['service_uid']):
        marker, host = load(target['root'])
        if any(marker[key] != parent[key] for key in ('installation_id', 'generation')):
            raise ConfigError('update_configuration_binding_changed')
        render(target['root'])  # read-only schema, integration and catalog bindings
        projection = managed_projection(target['root'], marker)
    after = _config_hashes(target['root'])
    if before != after:
        return None
    return {'configuration': after,
            'rooms': sorted(room['alias'] for room in host['rooms']),
            'managed_before': projection}


def _configuration_unprepared(target, job):
    """A recorded migration's original/planned bytes remain immutable."""
    pending = _control(target['prefix'] / 'pilot-upgrade.json', optional=True,
                       maximum=512 * 1024)
    if pending and pending.get('phase') == 'prepared':
        return False
    return not os.path.lexists(target['prefix'] / ('managed-preparation-' + job['id'] + '.json'))


def _adopt_configuration(job, snapshot, save):
    from .network.contracts import fingerprint
    save(**snapshot, configuration_previous_fingerprint=fingerprint(job['configuration']),
         configuration_baseline_revision=job.get('configuration_baseline_revision', 0) + 1,
         configuration_refreshed_at=time.time())


def _refresh_waiting_configuration(target, parent, job, save):
    if _config_hashes(target['root']) == job['configuration']:
        return False
    if not _configuration_unprepared(target, job):
        raise ConfigError('update_configuration_migration_already_prepared')
    if job['phase'] == 'locking':
        _gate(target, parent, job, None)
    # Drop old empty/identity evidence before validating a changed topology.
    save(phase='waiting_empty', empty_since=None, locked_since=None,
         reason='revalidating_current_configuration')
    try:
        snapshot = _configuration_snapshot(target, parent)
    except ConfigError:
        save(reason='waiting_for_valid_current_configuration')
        return True
    if snapshot is None:
        save(reason='waiting_for_stable_current_configuration')
        return True
    _adopt_configuration(job, snapshot, save)
    save(reason='configuration_refreshed_waiting_for_fresh_empty_window')
    return True


def _refresh_closed_configuration(target, parent, job, save):
    if _config_hashes(target['root']) == job['configuration']:
        return
    if not _configuration_unprepared(target, job):
        raise ConfigError('update_configuration_migration_already_prepared')
    snapshot = _configuration_snapshot(target, parent)
    if snapshot is None or set(snapshot['rooms']) != set(job['rooms']):
        raise ConfigError('update_configuration_changed_during_closed_admission')
    _adopt_configuration(job, snapshot, save)


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
    from .managed_host.admission import closed_aliases
    closed=closed_aliases(target,parent,rooms)
    rooms={alias:dict(row) for alias,row in rooms.items()}
    for alias,row in rooms.items():
        row.pop('managed_closed',None)
        if alias in closed:
            row.update(managed_closed=True,empty_seconds=max(0,now-row.get('updated_at',now)),room_id=None)
            continue
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


def _closed_failed_rows(target, parent, job, *, now, after=None):
    """A running controller with only confirmed-closed failed slots can upgrade.

    Normal operator close/maintenance, a stopped service, stale observations or
    any ownership uncertainty do not grant this path. Never read log text as
    lifecycle authority. The staged, signed successor executes this admission.
    """
    value = _control(target['root'] / 'state/rolling/status.json', parent['service_uid'])
    generated = value.get('generated_at')
    if (type(generated) not in (int, float) or not math.isfinite(generated)
            or not 0 <= now-generated <= 8 or after is not None and generated <= after
            or value.get('configuration_error') or set(value.get('rooms', {})) != set(job['rooms'])):
        return None
    rows = value['rooms']
    from .reconciliation import _journal
    journal = _journal(_control(target['root'] / 'state/rolling/ownership.json',
                               parent['service_uid'], private=True), set(job['rooms']))
    if set(journal['rooms']) != set(job['rooms']):
        return None
    for alias, row in rows.items():
        stored = journal['rooms'][alias]
        for evidence in (row, stored):
            if (evidence.get('phase') != 'parked' or evidence.get('operation') != 'closed'
                    or evidence.get('room_id') is not None or evidence.get('pid') is not None
                    or evidence.get('ownership_unknown') is not False or evidence.get('error')
                    or evidence.get('target') or evidence.get('creation_candidate')):
                return None
        status = row.get('status')
        if (not isinstance(status, dict) or status.get('reopenable') is not False
                or status.get('paused') is not False
                or status.get('maintenance', {}).get('locked')
                or status.get('maintenance', {}).get('uncertain')
                or identity({alias: row}) != identity({alias: stored})):
            return None
    return rows


def _step_closed_failed(instance, target, parent, job, save, *, now):
    rows = _closed_failed_rows(target, parent, job, now=now)
    if rows is None:
        return False
    if (job.get('empty_since') is None or not _same_rooms(rows, job)
            or now < job['empty_since']):
        save(empty_since=now, room_ids={a: None for a in rows}, worker_identity=identity(rows),
             reason='observing_confirmed_closed_failed_rooms')
        return True
    if now-job['empty_since'] < EMPTY_SECONDS:
        return True
    validate_runtime(parent, target['prefix']); validate_runtime(job['after'], target['prefix'])
    # Require a new controller observation after cryptographic validation, then
    # recheck the independent ownership journal immediately before shutdown.
    barrier = time.time(); deadline = time.monotonic()+12
    latest = None
    while time.monotonic() < deadline:
        latest = _closed_failed_rows(target, parent, job, now=time.time(), after=barrier)
        if _same_rooms(latest, job):
            break
        time.sleep(.25)
    if not _same_rooms(latest, job):
        save(empty_since=None, reason='closed_room_evidence_changed')
        return True
    if not _same_rooms(_closed_failed_rows(target, parent, job, now=time.time()), job):
        save(empty_since=None, reason='closed_room_evidence_changed')
        return True
    save(phase='committing', commit_started_at=time.time(), stage='approval', stage_at=time.time(),
         reason='configured_failed_rooms_already_closed',
         admission_kind='confirmed_closed_failed_rooms',
         automatic_recovery=job.get('automatic_recovery', {'format': 1, 'attempts': 0}))
    try:
        _commit(instance, target, parent, job, save)
    except Exception as error:
        from .update_lifecycle import failed
        failed(save, job, error)
        if job['phase'] == 'held':
            disarm(instance)
    return True


def _uncertain_maintenance(target,parent,job,*,now):
    """Fresh worker uncertainty can refuse admission, never authorize a mutation.

    _rows deliberately rejects non-idle/paused observations. Inspect this narrow
    negative evidence independently so a cancelled gate cannot masquerade as an
    ordinary endless empty-room wait while its worker is fenced.
    """
    value=_control(target['root']/'state/rolling/status.json',parent['service_uid'],optional=True)
    if type(value)is not dict:return False
    generated=value.get('generated_at');rows=value.get('rooms')
    if (type(generated)not in(int,float) or not math.isfinite(generated)
            or not 0<=now-generated<=8 or type(rows)is not dict
            or set(rows)!=set(job['rooms'])):return False
    from .managed_host.admission import closed_aliases
    closed=closed_aliases(target,parent,rows)
    return any(alias not in closed and type(row)is dict and type(row.get('status'))is dict
        and type(row['status'].get('maintenance'))is dict
        and row['status']['maintenance'].get('uncertain')is True for alias,row in rows.items())


def _same_rooms(rows, job):
    return (rows is not None and {a:r['room_id'] for a,r in rows.items()} == job.get('room_ids')
            and identity(rows) == job.get('worker_identity'))


def identity(rows):
    # Retain the v6 maintenance controller's process/generation/room/path check,
    # now bound to the protected v7 controller registration as well.
    return {a:[r.get(k) for k in ('pid','generation','room_id','release')] for a,r in rows.items()}


def _gated(rows, job):
    if not _same_rooms(rows, job): return False
    return all(r.get('managed_closed') is True or type(r.get('empty_seconds')) in (int,float) and r['empty_seconds'] >= 2
        and r.get('status',{}).get('paused') is True
        and r.get('status', {}).get('maintenance') == {
        'nonce':job['nonce'], 'locked':True, 'uncertain':False, 'error':None}
        for r in rows.values())


def journal_matches(target,parent,job):
    from .reconciliation import _journal
    value=_journal(_control(target['root']/'state/rolling/ownership.json',
        parent['service_uid'],private=True),set(job['rooms']))
    from .managed_host.admission import closed_aliases
    closed=closed_aliases(target,parent,value['rooms'])
    return (identity(value['rooms']) == job['worker_identity'] and all(
        alias in closed or r.get('phase')=='running' and r.get('operation')=='owned'
        and r.get('ownership_unknown') is False for alias,r in value['rooms'].items()))


def _gate(target, parent, job, phase):
    from .ratings.operations import _identity
    from .bookings.admission import update_admission
    from .bookings.policy import holds_updates
    with _identity(parent['service_uid']):
        with update_admission(target['root']):
            path = target['root'] / GATE
            current = _control(path, parent['service_uid'], private=True, optional=True)
            if current is not None and current.get('nonce') != job.get('nonce'):
                raise ConfigError('another maintenance request is present')
            if phase is None:
                path.unlink(missing_ok=True)
            else:
                if holds_updates(target['root']):return False
                atomic_json(path, {'format':1, 'phase':phase, 'nonce':job['nonce'],
                    'expires_at':time.time()+GATE_SECONDS, 'bridge_release':job['before']['release']})
    return True


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
        from .operator_maintenance import require_idle
        require_idle(target,parent)
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
        service = _service_state(instance)
        stopped=None
        if service['MainPID']==0 and service['ActiveState'] in ('inactive','failed'):
            from .managed_host.admission import stopped_host
            stopped=stopped_host(target,parent,reconcile=True)
            if stopped is None:raise ConfigError('stopped controller requires confirmed managed off intent')
            native={'rooms':stopped['rooms']}
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
        network = _state(instance)
        job = {'format':1, 'id':deployment_id or str(uuid.uuid4()), 'phase':'waiting_empty',
            'before':parent, 'after':candidate, 'archive':str(archive),
            'process':_process(service['MainPID']) if stopped is None else None, 'rooms':sorted(aliases),
            'configuration':_config_hashes(target['root']), 'service_enabled':service['UnitFileState']=='enabled',
            'network_active':network['ActiveState']=='active', 'network_enabled':network['UnitFileState']=='enabled',
            'queued_at':time.time(), 'empty_since':None, 'reason':'waiting_for_all_rooms_empty',
            'automatic_recovery':{'format':1,'attempts':0}}
        if release_approval_required:job['release_approval_required']=True
        from .managed_host.projection import read as managed_projection
        # The secret-free projection is owned by the service UID, like other
        # host JSON. Keep its normal owner/privacy checks under that identity.
        with _identity(parent['service_uid']):
            projection=managed_projection(target['root'],marker)
        if projection is not None:job['managed_before']=projection
        _write(path, job)
        return {'ok':True, 'phase':job['phase'], 'id':job['id'], 'release':staged['release'],
                'services_restarted':False, 'all_rooms_empty_required':True}


def _step_managed_stopped(instance,target,parent,job,save):
    """Install through a positively closed host, without inventing a worker PID."""
    from .managed_host.admission import stopped_host,stop_only_configuration
    proof=stopped_host(target,parent,reconcile=True)
    if proof is None:return False
    actual=_config_hashes(target['root'])
    if not stop_only_configuration(target,parent,job,proof,actual):
        save(phase='held',reason='configuration_changed_since_queue')
        return True
    from .updates.recovery import _approval
    _approval(target,job)
    # Recheck after complete artifact hashing under deployment admission. No
    # helper or explicit operator start can slip between this and commit.
    from .pilot_operations import _service_state
    service=_service_state(instance)
    latest=stopped_host(target,parent,reconcile=True)
    if (service['MainPID']!=0 or service['ActiveState'] not in ('inactive','failed')
            or latest is None or _config_hashes(target['root'])!=actual):return False
    from .bookings.admission import declared
    if declared(target['root']):
        if not job.get('nonce'):save(nonce=secrets.token_hex(16))
        if not _gate(target,parent,job,'committing'):
            save(phase='waiting_empty',empty_since=None,locked_since=None,reason='private_booking_reserved_or_active')
            return True
    else:_gate(target,parent,job,None)
    if actual!=job['configuration']:
        save(configuration_before_stop=job['configuration'],configuration=actual)
    admitted_at=time.time()
    save(phase='committing',commit_started_at=admitted_at,commit_admitted_at=admitted_at,
        stage='approval',stage_at=admitted_at,
         reason='managed_rooms_confirmed_closed_and_intentionally_stopped',
         admission_kind='managed_intentionally_stopped',
         automatic_recovery=job.get('automatic_recovery',{'format':1,'attempts':0}))
    try:_commit(instance,target,parent,job,save)
    except Exception as error:
        from .update_lifecycle import failed
        failed(save,job,error)
        if job['phase']=='held':disarm(instance)
    return True


def _precommit_controller_failure_candidate(job):
    """Recognize only the held, never-admitted controller-identity failure."""
    authority=job.get('automatic_recovery')
    return (job.get('phase')=='held' and job.get('reason')=='controller_identity_changed'
        and type(authority)is dict and authority.get('format')==1
        and authority.get('attempts')==0
        and all(job.get(key)is None for key in ('commit_started_at','commit_admitted_at',
            'services_stopped_at','upgrade_transition','startup_attempted_at',
            'admission_kind','failure_stage','failure_code')))


def _failed_controller_lifetime(target,process,queued,*,now):
    """Pin the failed systemd invocation to the queued /proc process identity."""
    import re
    from .pilot_operations import _systemctl
    from .config import ConfigError
    try:
        raw=_systemctl('show',target['unit'],'--property=LoadState,ActiveState,SubState,MainPID,'
            'ExecMainPID,Result,ExecMainStatus,ExecMainStartTimestampMonotonic,'
            'ExecMainExitTimestampMonotonic,ControlGroup,InvocationID')
        values=dict(line.split('=',1) for line in raw.decode('ascii').splitlines() if '=' in line)
        required={'LoadState','ActiveState','SubState','MainPID','ExecMainPID','Result',
            'ExecMainStatus','ExecMainStartTimestampMonotonic','ExecMainExitTimestampMonotonic',
            'ControlGroup','InvocationID'}
        if set(values)!=required:return None
        main=int(values['MainPID']);pid=int(values['ExecMainPID']);status=int(values['ExecMainStatus'])
        started=int(values['ExecMainStartTimestampMonotonic']);exited=int(values['ExecMainExitTimestampMonotonic'])
        invocation=values['InvocationID'];group=values['ControlGroup']
        if (values['LoadState']!='loaded' or values['ActiveState']!='failed'
                or values['SubState']!='failed' or main!=0 or pid!=process['pid']
                or values['Result']!='exit-code' or status<=0 or started<=0 or exited<=started
                or group not in ('','/system.slice/'+target['unit'])
                or re.fullmatch(r'[a-f0-9]{32}',invocation)is None):return None
        # /proc stat and systemd both use a boot-relative monotonic clock. The
        # process identity saved at queue must be the main process in this exact
        # failed unit invocation, not a same-PID stale receipt.
        ticks=os.sysconf('SC_CLK_TCK')
        if type(ticks)is not int or ticks<=0:return None
        if abs(started/1_000_000-int(process['start'])/ticks)>1.5:return None
        btime=next((int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                    if line.startswith('btime ')),None)
        exit_wall=(btime+exited/1_000_000) if type(btime)is int else None
        if (type(exit_wall)not in (int,float) or exit_wall<queued-1.5
                or exit_wall>now+1.5):return None
        if group:
            cgroup=Path('/sys/fs/cgroup')/group.lstrip('/')
            if cgroup.is_symlink():return None
            if cgroup.exists():
                if not cgroup.is_dir():return None
                for directory,dirs,files in os.walk(cgroup,followlinks=False):
                    root=Path(directory)
                    if any((root/name).is_symlink() for name in dirs):return None
                    if 'cgroup.procs' in files:
                        entries=(root/'cgroup.procs').read_text().split()
                        if entries:return None
        return {'unit':target['unit'],'invocation_id':invocation,'exec_main_pid':pid,
            'exec_main_started_monotonic':started,'exec_main_exited_monotonic':exited,
            'cgroup_empty':True}
    except (ConfigError,OSError,UnicodeError,ValueError,IndexError,KeyError,OverflowError):
        return None


def _closed_failed_controller_proof(instance,target,parent,job,*,now=None,committing=False,
        after_native_stop=False):
    """Prove a crashed predecessor and a fully settled native host, read-only.

    This is narrower than stopped-host admission: running desired intent stays
    intact, and only the exact previously queued controller can use the proof.
    Worker closure comes from the exact failed systemd invocation (with an
    empty service cgroup), the provider-service reap receipt, and the
    generation-bound managed ownership journal. The network agent may remain
    active until normal commit quiescence stops it.
    """
    import re
    from .network.contracts import fingerprint,NetworkError,require
    from .pilot_installation import _quiescent
    from .ratings.operations import _identity
    from .layout import load
    from .managed_host.policy import state as managed_state
    from .managed_host.projection import read as read_projection
    from .diagnostic_logs.common import transaction
    from .reconciliation import _journal
    from .operator_maintenance import NAME as MAINTENANCE_NAME,RECEIPT as MAINTENANCE_RECEIPT
    from .operator_maintenance import validate as validate_maintenance
    from .managed_host.policy import NAME as MANAGED_NAME
    from .updates.native import require_idle_worker_deployment
    from .bookings.policy import holds_updates
    from .pilot_identity import provider_execution
    from .pilot_operations import _service_state

    at=time.time() if now is None else now
    try:
        if job.get('before')!=parent:return None
        if committing:
            if (job.get('admission_kind')!='confirmed_closed_controller_failure'
                    or job.get('phase') not in ('committing','recovering','retrying')
                    or job.get('commit_started_at') is None):return None
        elif not _precommit_controller_failure_candidate(job):return None
        require(type(at) in (int,float) and math.isfinite(at) and at>=0,
                'update_failed_controller_clock_refused')
        process=job.get('process')
        require(type(process)is dict and set(process)=={'pid','start','boot'}
                and type(process['pid'])is int and process['pid']>0
                and type(process['start'])is str and process['start'].isdigit()
                and type(process['boot'])is str
                and process['boot']==Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'update_failed_controller_process_binding_refused')
        try:current_process=_process(process['pid'])
        except (FileNotFoundError,ProcessLookupError):current_process=None
        except (ConfigError,OSError,ValueError,IndexError):return None
        if current_process==process:return None
        service=_service_state(instance)
        if service['MainPID']!=0 or service['ActiveState'] not in ('inactive','failed'):
            return None
        queued=job.get('queued_at')
        if (type(queued)not in (int,float) or not math.isfinite(queued)
                or queued<=0 or queued>at):return None
        saved_proof=job.get('precommit_controller_failure_proof') if after_native_stop else None
        saved_lifetime=None
        saved_provider=None
        if after_native_stop:
            # Recovery runs after native stop has replaced systemd's failed
            # invocation and the provider lifecycle receipt. Reuse only the
            # exact root-owned proof persisted before stop, then recheck every
            # live managed/configuration/closure condition below.
            if (not committing or type(saved_proof)is not dict
                    or set(saved_proof)!={'process','drain_request_id','closed_aliases',
                        'configuration_sha256','projection_sha256','provider_service_failure'}
                    or saved_proof.get('process')!=process
                    or type(saved_proof.get('drain_request_id'))is not str
                    or type(saved_proof.get('closed_aliases'))is not list
                    or saved_proof.get('closed_aliases')!=sorted(job.get('rooms',[]))
                    or any(type(saved_proof.get(key))is not str or
                        re.fullmatch(r'[a-f0-9]{64}',saved_proof[key]) is None
                        for key in ('configuration_sha256','projection_sha256'))):return None
            if job.get('precommit_controller_failure_proof_sha256')!=fingerprint(saved_proof):
                return None
            saved_provider=saved_proof.get('provider_service_failure')
            if (type(saved_provider)is not dict
                    or set(saved_provider)!={'pid','workers_reaped','referee_child_reaped',
                        'systemd_invocation'}
                    or saved_provider.get('pid')!=process['pid']
                    or saved_provider.get('workers_reaped') is not True
                    or saved_provider.get('referee_child_reaped') is not True):return None
            saved_lifetime=saved_provider.get('systemd_invocation')
            if (type(saved_lifetime)is not dict
                    or set(saved_lifetime)!={'unit','invocation_id','exec_main_pid',
                        'exec_main_started_monotonic','exec_main_exited_monotonic','cgroup_empty'}
                    or saved_lifetime.get('unit')!=target['unit']
                    or type(saved_lifetime.get('invocation_id'))is not str
                    or re.fullmatch(r'[a-f0-9]{32}',saved_lifetime['invocation_id']) is None
                    or saved_lifetime.get('exec_main_pid')!=process['pid']
                    or type(saved_lifetime.get('exec_main_started_monotonic'))is not int
                    or saved_lifetime['exec_main_started_monotonic']<=0
                    or type(saved_lifetime.get('exec_main_exited_monotonic'))is not int
                    or saved_lifetime['exec_main_exited_monotonic']<=saved_lifetime['exec_main_started_monotonic']
                    or saved_lifetime.get('cgroup_empty') is not True):return None
            ticks=os.sysconf('SC_CLK_TCK')
            btime=next((int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                        if line.startswith('btime ')),None)
            if type(ticks)is not int or ticks<=0 or type(btime)is not int:return None
            process_started=saved_lifetime['exec_main_started_monotonic']/1_000_000
            process_start_tick=int(process['start'])/ticks
            exited_wall=btime+saved_lifetime['exec_main_exited_monotonic']/1_000_000
            if (abs(process_started-process_start_tick)>1.5 or exited_wall<queued-1.5
                    or exited_wall>at+1.5):return None
            lifetime=saved_lifetime
        else:
            lifetime=_failed_controller_lifetime(target,process,queued,now=at)
            if lifetime is None:return None

        root=Path(target['root']);uid=parent['service_uid']
        require_idle_worker_deployment(target,parent)
        provider_mode=provider_execution({'registration':parent})
        # The failed-service receipt is emitted only after the normal provider
        # context has reaped every worker/referee child. Bind it and the
        # separate failure receipt to the exact queued controller process.
        with _quiescent(root,uid):
            with _identity(uid):
                marker,host=load(root)
                if any(marker.get(k)!=parent.get(k) for k in ('installation_id','generation')):
                    return None
                aliases={room['alias'] for room in host['rooms']}
                if aliases!=set(job.get('rooms',[])) or not aliases:return None
                provider=None if after_native_stop else _control(
                    root/'state/provider-service.json',uid,private=True)
                if not after_native_stop and (type(provider)is not dict or provider.get('format')!=1
                        or provider.get('phase')!='failed' or provider.get('pid')!=process['pid']
                        or provider.get('uid')!=uid
                        or provider.get('execution')!=provider_mode
                        or provider.get('installation_id')!=parent.get('installation_id')
                        or provider.get('generation')!=parent.get('generation')
                        or provider.get('live_ready') is not False
                        or provider.get('workers_reaped') is not True
                        or provider.get('referee_child_reaped') is not True):return None
                if after_native_stop:
                    provider={'pid':saved_provider['pid'],'workers_reaped':True,
                        'referee_child_reaped':True}
                # Newer diagnostic receipts are optional corroboration; older
                # installed controllers only provide the lifecycle receipt.
                failure=None if after_native_stop else _control(
                    root/'state/controller-failure.json',uid,private=True,optional=True)
                if failure is not None and (type(failure)is not dict
                        or failure.get('format')!=1 or failure.get('pid')!=process['pid']
                        or type(failure.get('at'))not in (int,float)
                        or not math.isfinite(failure['at'])
                        or not queued<=failure['at']<=at):return None

                require(os.path.lexists(root/MANAGED_NAME),
                        'update_failed_controller_managed_host_missing')
                store=managed_state(root)
                managed_snapshot=store.snapshot()
                limits=store.limits()
                managed_pending=store.pending()
                projection=read_projection(root,marker)
                if (projection is None or type(job.get('managed_before'))is not dict
                        or projection!=job['managed_before']
                        or type(managed_pending)is not list or managed_pending
                        or managed_snapshot.get('policy_version')!=1
                        or limits.get('capacity_used')!=0):return None
                rooms={row['alias']:row for row in managed_snapshot.get('rooms',[])
                       if type(row)is dict}
                if set(rooms)!=aliases:return None
                with transaction(store.path) as db:
                    reservations=[dict(row) for row in db.execute('SELECT * FROM reservations')]
                    live_operations=db.execute("SELECT id FROM operations WHERE phase IN "
                        "('received','validated','waiting_boundary','applying')").fetchall()
                if reservations or live_operations:return None

                journal=_journal(_control(root/'state/rolling/ownership.json',uid,private=True),aliases)
                owners=journal.get('rooms')
                if type(owners)is not dict or set(owners)!=aliases:return None
                drain=job.get('controller_update_drain')
                from .update_drain import RECORD,_validate_drain,_validate_maintenance_receipt,_configuration_sha
                if type(drain)is not dict:return None
                drain=_validate_drain(drain,parent,job)
                if (drain['configuration_sha256']!=fingerprint(job.get('configuration'))
                        or _configuration_sha(root)!=drain['configuration_sha256']):return None

                request=_control(Path(target['prefix'])/MAINTENANCE_NAME)
                validate_maintenance(request,parent)
                progress=_control(root/MAINTENANCE_RECEIPT,uid,private=True)
                _validate_maintenance_receipt(progress,parent,request)
                gate=_control(root/GATE,uid,private=True,optional=True)
                if gate is not None and (type(gate)is not dict or gate.get('format')!=1
                        or gate.get('nonce')!=job.get('nonce')
                        or gate.get('bridge_release')!=parent.get('release')
                        or gate.get('phase') not in ('locking','committing')
                        or type(gate.get('expires_at'))not in (int,float)
                        or not math.isfinite(gate['expires_at'])):return None
                if (request.get('id')!=drain['request_id'] or request.get('phase')!='requested'
                        or request.get('mode')!='empty' or request.get('rooms')!=drain['rooms']
                        or progress.get('id')!=drain['request_id']
                        or progress.get('controller_pid')!=process['pid']
                        or progress.get('phase') not in ('complete','draining') or progress.get('failures')
                        or set(progress.get('rooms',{}))!=set(drain['rooms'])
                        or not drain['created_at']<=progress['updated_at']<=drain['expires_at']):return None
                closures=progress.get('closure_evidence')
                if type(closures)is not dict:return None
                recorded_closed={alias for alias,phase in progress['rooms'].items() if phase=='closed'}
                safe_partial={'waiting_empty','waiting_for_fresh_empty_roster','waiting_for_fresh_roster',
                    'waiting_map','waiting_for_notice_and_match_evidence','waiting_for_fresh_worker',
                    'waiting_for_worker_acknowledgement','request_delivered','requested','ready'}
                if (set(closures)!=recorded_closed
                        or progress['phase']=='complete' and recorded_closed!=set(drain['rooms'])
                        or progress['phase']=='draining' and any(
                            phase!='closed' and phase not in safe_partial
                            for phase in progress['rooms'].values())):return None
                for alias in drain['rooms']:
                    prior=drain['initial'][alias]
                    identity=prior['identity'];closure=closures.get(alias)
                    started=closure.get('closure_started_at') if type(closure)is dict else None
                    if alias in recorded_closed and (type(closure)is not dict
                            or closure.get('room_id')!=identity['room_id']
                            or closure.get('mode')!='empty' or closure.get('require_empty') is not True
                            or type(closure.get('notice_confirmed'))is not bool
                            or type(closure.get('active_map_observed'))is not bool
                            or type(started)not in (int,float) or not math.isfinite(started)
                            or not drain['created_at']<=started<=progress['updated_at']):return None

                for alias,row in rooms.items():
                    baseline=projection['rooms'].get(alias)
                    owner=owners[alias]
                    if (type(baseline)is not dict or row.get('desired')!=baseline.get('desired')
                            or row.get('revision')!=baseline.get('revision')
                            or row.get('preset')!=baseline.get('settings',{}).get('preset')
                            or row.get('visibility')!=baseline.get('settings',{}).get('visibility','public')
                            or row.get('pending_operation') is not None
                            or row.get('room_id') is not None or row.get('room_generation') is not None
                            or row.get('phase') not in ('stopped','unobserved')
                            or row.get('failure_code') is not None):return None
                    if (owner.get('phase') not in ('parked','stopped')
                            or owner.get('operation')!='closed' or owner.get('room_id') is not None
                            or owner.get('pid') is not None or owner.get('creation_candidate') is not None
                            or owner.get('creation_verification_failure') is not None
                            or owner.get('target') is not None or owner.get('error') is not None
                            or owner.get('ownership_unknown') is not False
                            or owner.get('admission_locked')):return None
                    if alias in drain['rooms']:
                        identity=drain['initial'][alias]['identity']
                        if (owner.get('generation')!=identity['generation']
                                or owner.get('release')!=identity['release']
                                or type(owner.get('updated_at'))not in (int,float)
                                or not math.isfinite(owner['updated_at'])
                                or not drain['created_at']<=owner['updated_at']<=at):return None

            # These readers open the local booking ledger read-only. Keep them
            # at the registered UID and outside the earlier identity scope.
            with _identity(uid):
                if holds_updates(root):return None
            from .update_drain import _eligibility_sha
            if _eligibility_sha(root,host,uid,projection)!=drain['eligibility_sha256']:
                return None

        if not _configuration_unprepared(target,job):return None
        snapshot=_configuration_snapshot(target,parent)
        if (snapshot is None or snapshot.get('configuration')!=job.get('configuration')
                or snapshot.get('rooms')!=sorted(aliases)
                or snapshot.get('managed_before')!=projection):return None
        if (job.get('phase')=='held' and not committing
                and not _precommit_controller_failure_candidate(job)):return None
        if committing and (job.get('admission_kind')!='confirmed_closed_controller_failure'
                or job.get('commit_started_at') is None):return None
        proof={'process':process,'drain_request_id':drain['request_id'],
            'closed_aliases':sorted(aliases),
            'configuration_sha256':fingerprint(snapshot['configuration']),
            'projection_sha256':fingerprint(projection),'provider_service_failure':{
                'pid':provider['pid'],'workers_reaped':True,'referee_child_reaped':True,
                'systemd_invocation':lifetime}}
        if after_native_stop and proof!=saved_proof:return None
        return proof
    except (ConfigError,NetworkError,OSError,ValueError,TypeError,KeyError,
            AttributeError,IndexError):
        return None


def _step_precommit_controller_failure(instance,target,parent,job,save):
    """Commit a never-admitted exact job after terminal, proven room closure."""
    import re
    from .network.contracts import NetworkError,fingerprint
    proof=_closed_failed_controller_proof(instance,target,parent,job)
    if proof is None:return False
    try:
        after=job.get('after');sha=after.get('bundle_sha256') if type(after)is dict else None
        if type(sha)is not str or re.fullmatch(r'[a-f0-9]{64}',sha)is None:return True
        archive=Path(job.get('archive',''))
        expected=Path(target['prefix'])/('controller-'+sha+'.zip')
        if archive!=expected:return True
        protected(archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=sha:return True
        from .updates.recovery import _approval
        _approval(target,job)
        from .bookings.policy import holds_updates
        from .ratings.operations import _identity
        with _identity(parent['service_uid']):
            if holds_updates(target['root']):return True
        if not _gate(target,parent,job,'committing'):return True
        # Gate publication is the admission barrier. Repeat the entire closure
        # and configuration proof after it so late local work cannot slip in.
        proof=_closed_failed_controller_proof(instance,target,parent,job)
        if proof is None:return True
        _approval(target,job)
    except (ConfigError,NetworkError,OSError,ValueError,TypeError,KeyError,
            AttributeError,IndexError):
        return True
    admitted_at=time.time()
    save(phase='committing',commit_started_at=admitted_at,commit_admitted_at=admitted_at,
        stage='approval',stage_at=admitted_at,
        reason='original_controller_failed_after_all_rooms_were_confirmed_closed',
        admission_kind='confirmed_closed_controller_failure',
        precommit_controller_failure_proof=proof,
        precommit_controller_failure_proof_sha256=fingerprint(proof),
        automatic_recovery=job.get('automatic_recovery',{'format':1,'attempts':0}))
    try:_commit(instance,target,parent,job,save)
    except Exception as error:
        from .update_lifecycle import failed
        failed(save,job,error)
        if job['phase']=='held':disarm(instance)
    return True


def _drained_rows(target, parent, job, *, now, after=None):
    """Combine exact maintenance closures with independently clean off slots.

    A maintenance receipt alone grants no authority. The helper binds its room
    IDs/generations to the original request and live controller; every remaining
    alias must satisfy the existing managed-off ownership proof.
    """
    from .update_drain import proof
    drained = proof(target, parent, job, now=now)
    if not drained:
        return None
    value = _control(target['root'] / 'state/rolling/status.json', parent['service_uid'])
    generated = value.get('generated_at')
    rows = value.get('rooms')
    if (type(generated) not in (int, float) or not math.isfinite(generated)
            or not 0 <= now-generated <= 8 or after is not None and generated <= after
            or value.get('configuration_error') or type(rows) is not dict
            or set(rows) != set(job['rooms'])):
        return None
    from .managed_host.admission import closed_aliases
    closed = closed_aliases(target, parent, rows)
    if set(drained) | set(closed) != set(rows):
        return None
    from .reconciliation import _journal
    journal = _journal(_control(target['root'] / 'state/rolling/ownership.json',
                               parent['service_uid'], private=True), set(rows))['rooms']
    if identity(rows) != identity(journal):
        return None
    for row in journal.values():
        if (row.get('phase') not in ('parked', 'stopped') or row.get('operation') != 'closed'
                or row.get('room_id') is not None or row.get('pid') is not None
                or row.get('ownership_unknown') is not False or row.get('creation_candidate')
                or row.get('error') or row.get('target')):
            return None
    return rows


def _step_independent_drain(instance, target, parent, job, save, *, now):
    """Reuse native per-room empty maintenance for signed controller releases."""
    from .update_drain import (RECORD, PAUSE, advance_configuration_pause,
                               _pending_configuration_work,managed_operations_pending,
                               release_unpublished_gate,request)
    from .network.contracts import NetworkError
    if (not job.get('release_approval_required') or job['phase'] != 'waiting_empty'
            or job.get('independent_drain_disabled')):
        return False
    from .managed_host.policy import NAME as MANAGED
    if not os.path.lexists(target['root'] / MANAGED):
        return False
    if job.get(RECORD)is None:
        try:
            released_orphan=release_unpublished_gate(target,parent,job)
        except (ConfigError,NetworkError,OSError,ValueError,TypeError,KeyError,
                AttributeError,IndexError):
            save(reason='waiting_for_exact_unpublished_drain_gate_cleanup')
            return True
        if released_orphan is False:
            save(reason='waiting_for_exact_unpublished_drain_gate_cleanup')
            return True
        try:
            pending=managed_operations_pending(target['root'],parent['service_uid'])
        except (ConfigError,NetworkError,OSError,ValueError,TypeError,KeyError,
                AttributeError,IndexError):
            # Do not let the legacy all-room-empty path publish another gate
            # when the managed ledger cannot prove it has no accepted work.
            save(reason='waiting_for_managed_admission_snapshot')
            return True
        if pending:
            save(reason='waiting_for_pending_managed_operation')
            return True
        if released_orphan:
            save(reason='rechecking_admission_after_unpublished_gate_cleanup')
    if job.get(PAUSE) is not None:
        try:
            result=advance_configuration_pause(target,parent,job,save,now=time.time)
        except NetworkError as error:
            if error.code in ('update_drain_pause_status_stale','update_drain_status_stale_or_incomplete',
                              'update_drain_receipt_stale'):
                save(reason='waiting_for_fresh_configuration_pause_evidence')
                return True
            save(phase='held',reason='managed_configuration_pause_requires_review',
                 failure_stage='admission',failure_code='update_maintenance_outcome_requires_review',
                 next_action='review_controller_update_with_operation_id')
            disarm(instance)
            return True
        except (ConfigError,NetworkError,OSError,ValueError,TypeError,KeyError,AttributeError,IndexError):
            save(phase='held',reason='managed_configuration_pause_requires_review',
                 failure_stage='admission',failure_code='update_maintenance_outcome_requires_review',
                 next_action='review_controller_update_with_operation_id')
            disarm(instance)
            return True
        if result['phase']=='requires_review':
            save(phase='held',reason='managed_configuration_pause_requires_review',
                 failure_stage='admission',failure_code='update_maintenance_outcome_requires_review',
                 next_action='review_controller_update_with_operation_id')
            disarm(instance)
        return True
    if job.get(RECORD) is not None:
        try:
            pending=_pending_configuration_work(target['root'],parent['service_uid'],job[RECORD])
        except NetworkError as error:
            if error.code=='update_drain_pause_status_stale':
                save(reason='waiting_for_fresh_managed_configuration_evidence')
                return True
            save(phase='held',reason='pending_managed_operation_requires_review',
                 failure_stage='admission',failure_code='update_managed_operation_requires_review',
                 next_action='review_controller_update_with_operation_id')
            disarm(instance)
            return True
        except (ConfigError,NetworkError,OSError,ValueError,TypeError,KeyError,AttributeError,IndexError):
            save(phase='held',reason='pending_managed_operation_requires_review',
                 failure_stage='admission',failure_code='update_managed_operation_requires_review',
                 next_action='review_controller_update_with_operation_id')
            disarm(instance)
            return True
        if pending:
            pause={'format':1,'request_id':job[RECORD]['request_id'],
                   'phase':'cancelling','operations':pending}
            save(**{PAUSE:pause},reason='canceling_drain_to_settle_managed_configuration')
            job[PAUSE]=pause
            try:
                result=advance_configuration_pause(target,parent,job,save,now=time.time)
            except NetworkError as error:
                if error.code in ('update_drain_pause_status_stale','update_drain_status_stale_or_incomplete',
                                  'update_drain_receipt_stale'):
                    save(reason='waiting_for_fresh_configuration_pause_evidence')
                    return True
                save(phase='held',reason='managed_configuration_pause_requires_review',
                     failure_stage='admission',failure_code='update_maintenance_outcome_requires_review',
                     next_action='review_controller_update_with_operation_id')
                disarm(instance)
                return True
            except (ConfigError,NetworkError,OSError,ValueError,TypeError,KeyError,AttributeError,IndexError):
                save(phase='held',reason='managed_configuration_pause_requires_review',
                     failure_stage='admission',failure_code='update_maintenance_outcome_requires_review',
                     next_action='review_controller_update_with_operation_id')
                disarm(instance)
                return True
            if result['phase']=='requires_review':
                save(phase='held',reason='managed_configuration_pause_requires_review',
                     failure_stage='admission',failure_code='update_maintenance_outcome_requires_review',
                     next_action='review_controller_update_with_operation_id')
                disarm(instance)
            return True
    try:
        result = request(target, parent, job, save, now=lambda: now)
    except LeaseBusy:
        # A scheduled refresh owns the admission lease. Retry from a fresh
        # snapshot rather than falling through to the legacy all-room gate.
        save(empty_since=None, reason='waiting_for_scheduled_room_refresh')
        return True
    except (ConfigError, NetworkError, OSError, ValueError):
        # A fresh request has not authorized any closure if admission failed.
        # Once a request exists, retain its operation and keep retrying fresh
        # evidence; never create another request or reset ownership to proceed.
        if job.get(RECORD) is None:
            return False
        save(reason='waiting_for_valid_independent_drain_evidence')
        return True
    if result['phase'] == 'no_eligible_rooms':
        return False
    if result['phase'] in ('requires_review', 'cancelled', 'expired'):
        save(phase='held', reason='independent_drain_requires_review',
             failure_stage='admission', failure_code='update_maintenance_outcome_requires_review')
        disarm(instance)
        return True
    rows = _drained_rows(target, parent, job, now=time.time())
    if rows is None:
        save(reason='draining_each_room_when_empty')
        return True
    validate_runtime(parent, target['prefix'])
    validate_runtime(job['after'], target['prefix'])
    barrier = time.time()
    deadline = time.monotonic()+12
    latest = None
    while time.monotonic() < deadline:
        latest = _drained_rows(target, parent, job, now=time.time(), after=barrier)
        if latest is not None:
            break
        time.sleep(.25)
    if latest is None or not _gate(target, parent, job, 'committing'):
        save(reason='waiting_for_fresh_independent_drain_evidence')
        return True
    # Recheck after the gate, so a booking/managed admission cannot race the
    # final proof. The old controller still owns every closure and journal.
    latest = _drained_rows(target, parent, job, now=time.time(), after=barrier)
    if latest is None:
        save(reason='waiting_for_fresh_independent_drain_evidence')
        return True
    save(phase='committing', commit_started_at=time.time(), stage='approval', stage_at=time.time(),
         reason='all_requested_rooms_independently_closed',
         admission_kind='confirmed_maintenance_drained_rooms', worker_identity=identity(latest),
         automatic_recovery=job.get('automatic_recovery', {'format': 1, 'attempts': 0}))
    try:
        _commit(instance, target, parent, job, save)
    except Exception as error:
        from .update_lifecycle import failed
        failed(save, job, error)
        if job['phase'] == 'held':
            disarm(instance)
    return True


def _drain_restoration_needed(job):
    """Only a never-admitted canceled/held drain may restore old room intent."""
    return (not _precommit_controller_failure_candidate(job)
            and job.get('controller_update_drain') is not None
            and job.get('phase') in ('cancelled', 'held')
            and job.get('drain_restoration_phase') not in ('complete', 'requires_review')
            and all(job.get(k) is None for k in ('commit_started_at', 'commit_admitted_at',
                                                'services_stopped_at', 'upgrade_transition')))


def _restore_canceled_drain(instance, target, current, job, save):
    """Keep the owned candidate timer alive until native restoration settles."""
    from .update_drain import (cancel as cancel_drain, restore,
                               restoration_failure_code, RESTORATION_FAILURE_CODES)
    from .network.contracts import NetworkError
    if not _drain_restoration_needed(job):
        return False
    if current != job['before']:
        save(drain_restoration_phase='requires_review',
             drain_restoration_failure_code='update_recovery_registration_changed')
        return True
    try:
        canceled = cancel_drain(target, current, job)
        if canceled['phase'] in ('waiting', 'requires_review'):
            save(drain_restoration_phase=canceled['phase'],
                 drain_restoration_failure_code=('update_drain_cancellation_requires_review'
                     if canceled['phase']=='requires_review' else None),
                 drain_restoration_updated_at=time.time())
            return True
        if canceled.get('rooms') == []:
            # Admission failed before the mailbox was written; there are no
            # native closures to restore and the exact gate was released.
            save(drain_restoration_phase='complete',
                 drain_restoration_failure_code=None,
                 drain_restoration_updated_at=time.time())
            return True
        # Valid settings saved by still-playing siblings must remain current.
        # A cancellation grants no exception for an already prepared migration.
        if not _configuration_unprepared(target, job):
            raise ConfigError('update_recovery_configuration_changed')
        if _config_hashes(target['root']) != job['configuration']:
            snapshot = _configuration_snapshot(target, current)
            if snapshot is None:
                save(drain_restoration_phase='restoring',drain_restoration_failure_code=None)
                return True
            _adopt_configuration(job, snapshot, save)
        failures=[]
        result = restore(target, current, job,diagnostic=failures.append)
        detail=failures[-1] if failures else None
        if type(detail)is not str or detail not in RESTORATION_FAILURE_CODES:
            detail='update_guard_requires_review'
        save(drain_restoration_phase=result['phase'],
             drain_restoration_failure_code=detail if result['phase']=='requires_review' else None,
             drain_restoration_updated_at=time.time())
    except (ConfigError, NetworkError, OSError, ValueError) as error:
        save(drain_restoration_phase='requires_review',
             drain_restoration_failure_code=restoration_failure_code(error),
             drain_restoration_updated_at=time.time())
    return True


def _ensure_automatic_recovery_timer(instance, job):
    """Keep the signed staged updater independently scheduled until completion.

    Older update agents stop dispatching after an identity-change hold. A
    timer owned by the same approved job can retry its existing proof-gated
    step; it neither grants room ownership nor admits a transaction itself.
    """
    if (job.get('release_approval_required') is not True
            or job.get('phase') not in ('waiting_empty', 'locking')):
        return False
    recorded = job.get('timer_sha256')
    if type(recorded) is str:
        from .pilot_operations import UNIT_BASE, _systemctl
        timer = UNIT_BASE / ('poolrotater-v7-controller-update-' + instance + '.timer')
        try:
            if (hashlib.sha256(protected(timer).read_bytes()).hexdigest() == recorded
                    and _systemctl('show', timer.name, '--property=ActiveState').decode().strip()
                    == 'ActiveState=active'):
                return True
        except (OSError, ConfigError):
            pass
    arm(instance)
    return True


def step(instance):
    """Fresh empty admission, or evidence-bound continuation of this exact job."""
    from .pilot_operations import _service_state, _systemctl
    target, marker, _, current = _binding(instance)
    # Recovery owns both native locks itself. Never recursively acquire them.
    observed=_control(target['prefix']/NAME)
    # This runs in the staged successor even when an older installed agent
    # dispatched it. Arm before admission so its proof-gated recovery remains
    # callable if that older agent later treats a hold as terminal.
    _ensure_automatic_recovery_timer(instance, observed)
    if observed.get('phase') in ('committing','recovering','retrying'):
        from .update_lifecycle import automatic_eligible
        if automatic_eligible(observed):
            from .updates.recovery import controller
            result=controller(instance,observed['id'],'complete',automatic=True)
            if result.get('phase')in('complete','held'):disarm(instance)
            return result
    with file_lease(target['prefix'] / 'deployment-admission.lock'),file_lease(target['prefix'] / 'controller-update-admin.lock'):
        path = target['prefix'] / NAME; job = _control(path)
        if _drain_restoration_needed(job):
            def restore_save(**values):
                job.update(values); _write(path, job)
            _restore_canceled_drain(instance, target, current, job, restore_save)
            if not _drain_restoration_needed(job):
                disarm(instance)
            return {'ok':job.get('drain_restoration_phase')!='requires_review',
                    'phase':job['phase'], 'reason':job.get('reason'),
                    'drain_restoration_phase':job.get('drain_restoration_phase')}
        retry_configuration = configuration_retryable(job)
        retry_failed_controller=_precommit_controller_failure_candidate(job)
        def save(**values):
            job.update(values); _write(path, job)
        if job.get('phase') in ('complete','cancelled','held') and not retry_configuration:
            if retry_failed_controller:
                # The only held identity-change case that remains retryable is
                # the exact queued predecessor, still registered, now terminal.
                # A live/reused/replaced controller follows the ordinary held
                # path and never reaches the closed-owner exception.
                if current==job.get('before'):
                    service=_service_state(instance)
                    if (service['MainPID']==0 and service['ActiveState'] in ('inactive','failed')):
                        _step_precommit_controller_failure(instance,target,job['before'],job,save)
                        return {'ok':job.get('phase')!='held','phase':job['phase'],
                                'reason':job.get('reason')}
            disarm(instance)
            return {'ok':True, 'phase':job['phase'], 'reason':job.get('reason')}
        if job.get('phase') not in ('waiting_empty','locking') and not retry_configuration:
            save(phase='held', reason='interrupted_commit_requires_review',
                 failure_stage=job.get('stage','recovery_validation'),
                 failure_code='update_recovery_limit' if job.get('automatic_recovery') else 'update_interrupted_commit',
                 next_action='review_controller_update_with_operation_id')
            return {'ok':False,'phase':'held','reason':job['reason']}
        parent = job['before']
        from .bookings.policy import holds_updates
        from .ratings.operations import _identity
        with _identity(parent['service_uid']):booking_held=holds_updates(target['root'])
        if booking_held:
            if job['phase']=='locking':_gate(target,parent,job,None)
            save(phase='waiting_empty',empty_since=None,locked_since=None,reason='private_booking_reserved_or_active')
            return {'ok':True,'phase':'waiting_empty','reason':job['reason']}
        service=_service_state(instance)
        if current==parent and service['MainPID']==0 and service['ActiveState'] in ('inactive','failed'):
            if retry_configuration:
                save(phase='waiting_empty', empty_since=None, locked_since=None,
                     reason='revalidating_current_configuration')
            if _refresh_waiting_configuration(target, parent, job, save):
                return {'ok':True,'phase':job['phase'],'reason':job['reason']}
            if _step_managed_stopped(instance,target,parent,job,save):
                return {'ok':job['phase']!='held','phase':job['phase'],'reason':job.get('reason')}
        try: same_process=_process(service['MainPID']) == job['process']
        except (ConfigError, OSError): same_process=False
        if current != parent or not same_process:
            save(phase='held', reason='controller_identity_changed')
            return {'ok':False,'phase':'held','reason':job['reason']}
        if retry_configuration:
            save(phase='waiting_empty', empty_since=None, locked_since=None,
                 reason='revalidating_current_configuration')
        if _refresh_waiting_configuration(target, parent, job, save):
            return {'ok':True,'phase':job['phase'],'reason':job['reason']}
        # Long empty-room waits never extend a release approval's expiry.
        # Recheck before admission/stop; interrupted commits use explicit native
        # recovery rather than a new unattended expiry exception.
        try:
            from .updates.approval import require_upgrade,require_approved
            check=require_approved if job.get('release_approval_required') else require_upgrade
            check(validate_runtime(parent,target['prefix'])['manifest'],
                  validate_runtime(job['after'],target['prefix'])['manifest'],parent,job['after'])
        except ConfigError:
            if job.get('controller_update_drain') is not None:
                from .update_drain import cancel as cancel_drain
                cancel_drain(target, parent, job)
            elif job['phase']=='locking': _gate(target,parent,job,None)
            save(phase='held',reason='controller_approval_expired_or_incompatible')
            return {'ok':False,'phase':'held','reason':job['reason']}
        now=time.time()
        if _uncertain_maintenance(target,parent,job,now=now):
            save(phase='held',reason='maintenance_outcome_requires_review',stage='admission',
                stage_at=now,failure_stage='admission',failure_code='update_maintenance_outcome_requires_review',
                failure_at=now,next_action='review_controller_update_with_operation_id; retain_worker_uncertainty')
            disarm(instance)
            return {'ok':False,'phase':'held','reason':job['reason'],
                'failure_stage':job['failure_stage'],'failure_code':job['failure_code']}
        if job['phase'] == 'waiting_empty' and _step_closed_failed(instance, target, parent, job, save, now=now):
            return {'ok': job['phase'] != 'held', 'phase': job['phase'], 'reason': job.get('reason')}
        if _step_independent_drain(instance, target, parent, job, save, now=now):
            return {'ok': job['phase'] != 'held', 'phase': job['phase'], 'reason': job.get('reason')}
        rows=_rows(target,parent,now=now)
        if rows is not None and set(rows) != set(job['rooms']): rows=None
        if job['phase']=='waiting_empty':
            if rows is None or any(not r.get('managed_closed') and r['status'].get('paused') for r in rows.values()):
                save(empty_since=None,reason='waiting_for_all_rooms_empty')
            elif job['empty_since'] is None or not _same_rooms(rows,job) or now < job['empty_since']:
                save(empty_since=now,room_ids={a:r['room_id']for a,r in rows.items()},
                     worker_identity=identity(rows),reason='observing_continuous_empty_window')
            elif now-job['empty_since']>=EMPTY_SECONDS and all(
                    type(r.get('empty_seconds')) in (int,float) and r['empty_seconds']>=EMPTY_SECONDS for r in rows.values()):
                save(phase='locking',nonce=secrets.token_hex(16),lock_started=now,locked_since=None,
                     reason='waiting_for_private_admission_and_late_join_drain')
                if not _gate(target,parent,job,'locking'):
                    save(phase='waiting_empty',empty_since=None,locked_since=None,reason='private_booking_reserved_or_active')
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
                    admitted=_gate(target,parent,job,'committing')
                    # Refresh after writing the phase too; a late arrival keeps
                    # its old worker and causes all gates to be released.
                    if not admitted:
                        _gate(target,parent,job,None)
                        save(phase='waiting_empty',empty_since=None,locked_since=None,reason='private_booking_reserved_or_active')
                    elif not _gated(_rows(target,parent,now=time.time()),job):
                        _gate(target,parent,job,None)
                        save(phase='waiting_empty',empty_since=None,locked_since=None,reason='late_join_cancelled_commit')
                    elif not journal_matches(target,parent,job):
                        _gate(target,parent,job,None)
                        save(phase='held',reason='ownership_journal_changed_before_commit')
                    else:
                        save(phase='committing',commit_started_at=time.time(),stage='approval',stage_at=time.time(),reason='all_rooms_private_empty_and_drained',
                             automatic_recovery=job.get('automatic_recovery',{'format':1,'attempts':0}))
                        try: _commit(instance,target,parent,job,save)
                        except Exception as error:
                            from .update_lifecycle import failed
                            failed(save,job,error)
                            if job['phase']=='held':disarm(instance)
                            return {'ok':False,'phase':job['phase'],'reason':job['reason'],
                                    'failure_stage':job.get('failure_stage'),'failure_code':job['failure_code']}
        return {'ok':True,'phase':job['phase'],'reason':job.get('reason')}


def _commit_approval(target,parent,job):
    if job.get('release_approval_required'):
        from .updates.approval import require_approved
        require_approved(validate_runtime(parent,target['prefix'])['manifest'],
                         validate_runtime(job['after'],target['prefix'])['manifest'],parent,job['after'])


def _commit(instance,target,parent,job,save):
    # Pausing helpers is a lifecycle mutation too. Validate approval before it,
    # then repeat at the actual service switch after bounded quiescence.
    _commit_approval(target,parent,job)
    if job.get('admission_kind')=='confirmed_closed_controller_failure':
        if _closed_failed_controller_proof(instance,target,parent,job,committing=True) is None:
            raise ConfigError('update_failed_controller_closure_changed')
    from .update_helpers import quiesce
    with quiesce(instance,target,job,save):
        return _commit_quiescent(instance,target,parent,job,save)


def _commit_quiescent(instance,target,parent,job,save):
    from .pilot_operations import _systemctl, automatic_start
    from .network import operations as network
    from .pilot_upgrade import upgrade
    from .ratings.operations import _identity
    from .bookings.policy import holds_updates
    with _identity(parent['service_uid']):booking_held=holds_updates(target['root'])
    if booking_held:raise ConfigError('private booking blocks controller update')
    # No mutation or token I/O outside the already-running services. Native
    # shutdown confirms closures before releasing the provider owner lease.
    _commit_approval(target,parent,job)
    from .update_lifecycle import checkpoint,wait_stopped
    checkpoint(save,'approval')
    if job.get('admission_kind')=='confirmed_closed_controller_failure':
        if _closed_failed_controller_proof(instance,target,parent,job,committing=True) is None:
            raise ConfigError('update_failed_controller_closure_changed')
    network._checked_unit(instance,optional=not job['network_active'])
    # A proof-backed admission records its first durable timestamp only after
    # gate, approval, and closure checks passed. Preserve it across the normal
    # quiescent handoff so helper loss in that window remains recoverable.
    if job.get('commit_admitted_at') is None:
        save(commit_admitted_at=time.time())
    checkpoint(save,'network_stop')
    if job['network_enabled']: _systemctl('disable',network.name(instance))
    if job['network_active']: network.unit(instance,'stop')
    checkpoint(save,'controller_stop')
    if job.get('admission_kind')=='confirmed_closed_controller_failure':
        if _closed_failed_controller_proof(instance,target,parent,job,committing=True) is None:
            raise ConfigError('update_failed_controller_closure_changed')
    _systemctl('stop',target['unit'],timeout=100); _systemctl('disable',target['unit'])
    checkpoint(save,'cleanup')
    wait_stopped(target['root'],parent['service_uid'],save=save)
    # Workers may persist their last valid preferences during clean shutdown.
    # Quiescence is the last safe baseline refresh before any migration writes.
    _refresh_closed_configuration(target, parent, job, save)
    if job.get('admission_kind')=='managed_intentionally_stopped':
        from .managed_host.admission import stopped_host
        if stopped_host(target,parent,reconcile=True) is None:
            raise ConfigError('update_guard_requires_review')
    save(services_stopped_at=time.time())
    _gate(target,parent,job,None)
    checkpoint(save,'upgrade')
    result=upgrade(instance,job['archive'],job['after']['bundle_sha256'],
                   from_sha256=parent['bundle_sha256'])
    checkpoint(save,'configuration')
    if _config_hashes(target['root']) != job['configuration']:
        raise ConfigError('configuration changed during controller upgrade')
    from .managed_host.upgrade import prepare as prepare_managed_configuration
    prepare_managed_configuration(target,job['after'],job)
    save(upgrade_transition=result['transition_id'])
    checkpoint(save,'controller_start',startup_attempted_at=time.time())
    started=automatic_start(instance,target['root'])
    if not started.get('ok'): raise ConfigError('upgraded controller startup requires review')
    checkpoint(save,'readiness')
    from .updates.recovery import wait_for_workers
    wait_for_workers(instance,target,job['after'])
    checkpoint(save,'network_restore')
    if job['service_enabled']: _systemctl('enable',target['unit'])
    from .managed_host.policy import administrative_enabled as managed_enabled
    managed=managed_enabled(target['root'],job['after']['service_uid'])
    if job['network_active'] or managed: network.unit(instance,'start')
    if job['network_enabled'] or managed: _systemctl('enable','poolrotater-v7-network-'+instance+'.service')
    checkpoint(save,'completion')
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
        if _drain_restoration_needed(job):
            return  # Native canceled-drain restoration still owns this timer.
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
    with file_lease(target['prefix']/'deployment-admission.lock'), file_lease(target['prefix']/'controller-update-admin.lock'):
        path=target['prefix']/NAME;job=_control(path)
        if job['phase']=='committing' or job.get('commit_started_at') or job.get('upgrade_transition'):
            raise ConfigError('controller commit requires review; cancellation cannot undo it')
        if job.get('controller_update_drain') is not None:
            from .update_drain import cancel as cancel_drain
            cancel_drain(target, job['before'], job)
        elif job.get('nonce'):_gate(target,job['before'],job,None)
        _write(path,{**job,'phase':'cancelled','reason':'operator_cancelled_before_commit'})
        disarm(instance)
        return {'ok':True,'phase':'cancelled','services_restarted':False}


def _refresh_timer_service(instance, target, path, expected, candidate):
    """Rebind only an exact recognized, quiescent native timer launcher."""
    import re
    from .pilot_operations import UNIT_BASE, _systemctl
    actual=path.read_text()
    prefix='ExecStart=/usr/bin/python3.12 -I -S -B '
    lines=[line for line in actual.splitlines() if line.startswith('ExecStart=')]
    if len(lines)!=1 or not lines[0].startswith(prefix):
        raise ConfigError('unrecognized controller update unit')
    suffix='/poolrotater_host/controller_update_entry.py '+instance
    if not lines[0].endswith(suffix):raise ConfigError('unrecognized controller update unit')
    old=Path(lines[0][len(prefix):-len(suffix)])
    if (old.parent!=target['prefix']/'releases'
            or re.fullmatch(r'v7\.[0-9]+\.[0-9]+-[a-f0-9]{16}',old.name) is None
            or actual.replace(str(old),str(candidate['release']))!=expected):
        raise ConfigError('unrecognized controller update unit')
    from .pilot_upgrade import _code,_replace_code
    old_entry=old/'poolrotater_host/controller_update_entry.py'
    new_entry=candidate['release']/'poolrotater_host/controller_update_entry.py'
    if _code(old_entry)!=_code(new_entry):
        raise ConfigError('unrecognized controller update launcher')
    timer=UNIT_BASE/('poolrotater-v7-controller-update-'+instance+'.timer')
    # This fixed template is independent of the release path. Do not touch an
    # unrelated same-named timer to make a stale service acceptable.
    expected_timer=_timer_text(instance)
    if not timer.exists() or protected(timer).read_text()!=expected_timer:
        raise ConfigError('unrecognized controller update unit')
    _systemctl('stop',timer.name)
    raw=_systemctl('show',path.name,'--property=ActiveState,MainPID')
    try:
        rows=raw.decode('ascii').splitlines();state=dict(line.split('=',1)for line in rows)
        if len(rows)!=2 or set(state)!={'ActiveState','MainPID'}:raise ValueError
        if state['ActiveState'] not in ('inactive','failed') or state['MainPID']!='0':raise ValueError
    except (ValueError,UnicodeError):
        raise ConfigError('controller_update_unit_busy_or_unconfirmed') from None
    _replace_code(path,expected.encode('ascii'))


def _timer_text(instance):
    name='poolrotater-v7-controller-update-'+instance
    return f'''[Unit]
Description=Wait for all PoolRotater rooms to empty ({instance})

[Timer]
OnActiveSec=30
OnUnitInactiveSec=20
AccuracySec=1
Unit={name}.service

[Install]
WantedBy=timers.target
'''


def arm(instance):
    """Install the exact root timer only after queue admission and validation."""
    target,_,_,_=_binding(instance)
    with file_lease(target['prefix']/'controller-update-admin.lock'):
        return _arm(instance)


def _arm(instance):
    from .pilot_operations import UNIT_BASE, _systemctl
    target,_,_,_= _binding(instance)
    job=_control(target['prefix']/NAME)
    if job['phase'] in ('complete','cancelled'):
        return {'ok':True,'phase':job['phase'],'timer_armed':False,'rooms_stopped':False}
    if job['phase'] in ('committing','held'):
        raise ConfigError('controller_update_job_requires_review')
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
    timer=_timer_text(instance)
    for suffix,raw in (('.service',service),('.timer',timer)):
        path=UNIT_BASE/(name+suffix)
        if path.exists():
            protected(path)
            if path.read_text()!=raw:
                if suffix!='.service':raise ConfigError('unrecognized controller update unit')
                _refresh_timer_service(instance,target,path,raw,candidate)
        else:
            with path.open('x')as out:out.write(raw);out.flush();os.fsync(out.fileno())
            path.chmod(0o644)
    _write(target['prefix']/NAME,{**job,'timer_sha256':hashlib.sha256(timer.encode()).hexdigest()})
    _systemctl('daemon-reload')
    _systemctl('enable','--now',name+'.timer')
    return {'ok':True,'phase':job['phase'],'timer':name+'.timer','rooms_stopped':False}
