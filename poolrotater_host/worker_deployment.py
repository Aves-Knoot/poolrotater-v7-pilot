"""Durable root requests consumed by the existing empty-room rolling engine."""
from pathlib import Path
import time
import uuid

from .config import ConfigError
from .provisioning import _control, protected
from .layout import atomic_json

NAME = 'worker-deployment.json'


def validate_job(value, parent):
    if (type(value) is not dict or set(value) != {'format', 'id', 'phase', 'parent_sha256',
            'installation_id', 'generation', 'candidate_sha256', 'rooms'}
            or type(value['format']) is not int or value['format'] != 1
            or value['phase'] not in ('queued', 'cancelled')
            or any(value.get(k) != parent[k] for k in ('installation_id', 'generation'))
            or value['parent_sha256'] != parent['bundle_sha256']
            or type(value['rooms']) is not list or not value['rooms']
            or len(value['rooms']) != len(set(value['rooms']))
            or not all(type(a) is str for a in value['rooms'])):
        raise ConfigError('invalid or foreign worker deployment')
    try:
        if str(uuid.UUID(value['id'])) != value['id']: raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ConfigError('invalid worker deployment identifier') from None
    from .worker_releases import receipt_path
    receipt_path(parent, value['candidate_sha256'])
    return value


def read_job(parent):
    path = Path(parent['prefix']) / NAME
    value = _control(path, optional=True, maximum=16384)
    return validate_job(value, parent) if value is not None else None


def result_path(root, identifier):
    return Path(root) / 'state/rolling/deployments' / (identifier + '.json')


def retire_after_controller_upgrade(target, parent):
    """Archive old-parent intent only inside a stopped, clean controller upgrade."""
    import os
    from .provisioning import _write
    job = read_job(parent)
    if job is None: return
    journal = _control(target['root']/'state/rolling/ownership.json', parent['service_uid'], private=True)
    if not journal.get('rooms') or any(row.get('operation') != 'closed' or row.get('room_id')
            or row.get('ownership_unknown') is not False for row in journal['rooms'].values()):
        raise ConfigError('old worker deployment requires confirmed closed rooms')
    result = _control(result_path(target['root'], job['id']), parent['service_uid'], private=True, optional=True)
    if job['phase'] != 'cancelled' and (not result or result.get('phase') not in ('complete','cancelled')):
        raise ConfigError('old worker deployment must finish or be cancelled before upgrade')
    folder = Path(parent['prefix'])/'retired-worker-deployments'
    folder.mkdir(mode=0o755, exist_ok=True); protected(folder)
    destination = folder/(job['id']+'.json')
    previous = _control(destination, optional=True)
    if previous is not None and previous != job:
        raise ConfigError('retired worker deployment differs')
    _write(destination, job)
    path = Path(parent['prefix'])/NAME; path.unlink()
    fd = os.open(path.parent, os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def poll(supervisor):
    """Replay pending intent after a clean restart; never replay a failed roll."""
    parent = getattr(supervisor, 'protected_parent', None)
    if parent is None: return
    job = read_job(parent)
    if job is None: return
    path = result_path(supervisor.runtime, job['id'])
    def result(phase, **extra):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_json(path, {'format':1, 'id':job['id'], 'phase':phase,
            'parent_sha256':job['parent_sha256'], 'candidate_sha256':job['candidate_sha256'],
            'rooms':job['rooms'], 'at':time.time(), **extra})
    previous = _control(path, parent['service_uid'], private=True, optional=True)
    if previous and (previous.get('phase')in('complete','cancelled')
                     or previous.get('phase')=='failed' and job['phase']!='cancelled'):return
    if any(alias not in supervisor.configs for alias in job['rooms']):
        result('failed', reason='room_topology_changed'); return
    if job['phase'] == 'cancelled':
        if supervisor.rollout_alias: return
        rows=supervisor.registry.snapshot()['rooms']
        if any(rows.get(a,{}).get('ownership_unknown')is not False
               or rows.get(a,{}).get('phase')=='recovery_required'for a in job['rooms']):return
        from .worker_releases import receipt_path
        approved=_control(protected(receipt_path(parent,job['candidate_sha256'])))
        release=Path(approved['candidate']['release'])
        if any(a in supervisor.targets and Path(supervisor.targets[a])!=release for a in job['rooms']):return
        for alias in job['rooms']:
            supervisor.targets.pop(alias, None)
            row = supervisor.registry.snapshot()['rooms'].get(alias, {})
            if row.get('phase') == 'waiting_empty':
                supervisor.record(alias, phase='running', target=None, target_version=None)
        result('cancelled'); return
    if supervisor.schema_baseline is None or supervisor.rollout_alias: return
    if getattr(supervisor, '_protected_job_id', None) != job['id']:
        if supervisor.targets: return
        # A delayed chat/API call can make a live worker's observation stale.
        # Wait cheaply instead of rehashing both environments every tick.
        if any(supervisor.workers.get(a) is None or not supervisor.workers[a].fresh()
               for a in job['rooms']): return
        from .worker_releases import receipt_path, validate_candidate
        approved = _control(protected(receipt_path(parent, job['candidate_sha256'])))
        try:
            candidate = validate_candidate(parent, approved)
        except Exception:
            result('failed', reason='candidate_integrity_or_contract_changed')
            return
        release = candidate['release']
        aliases = [a for a in job['rooms'] if supervisor.workers.get(a) is None
                   or supervisor.workers[a].release != release]
        if aliases:
            try:
                supervisor.request(aliases, release)
            except Exception:
                result('failed', reason='native_schema_or_admission_refused')
                return
        supervisor._protected_job_id = job['id']
        supervisor._protected_job_release = release
        result('waiting_empty')
    release = supervisor._protected_job_release
    if any(a in supervisor.targets for a in job['rooms']): return
    rows = supervisor.registry.snapshot()['rooms']
    if all(supervisor.workers.get(a) is not None
           and supervisor.workers[a].release == release
           and supervisor.workers[a].fresh()
           and rows.get(a, {}).get('phase') == 'running' for a in job['rooms']):
        result('complete')
    else:
        result('failed', reason='handoff_failed_or_rolled_back')
