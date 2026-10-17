"""Protected stage/queue/cancel/status; no room or credential requests."""
from pathlib import Path
from contextlib import nullcontext
import uuid

from .config import ConfigError
from .layout import file_lease
from .pilot_installation import administrative_binding, RESERVE
from .provisioning import _control, _provision, _write, protected
from .runtime_lifecycle import validate_runtime
from .worker_releases import receipt_path, validate_candidate, require_worker_contract
from .worker_deployment import NAME, read_job, result_path


def command(instance, action, *, bundle=None, sha256=None, rooms=None, deployment_id=None):
    if action not in ('stage', 'queue', 'cancel', 'status'):
        raise ConfigError('unknown worker deployment operation')
    target, marker, parent = administrative_binding(instance)
    prefix = target['prefix']
    admission = file_lease(prefix / 'deployment-admission.lock') if action in ('queue','cancel') else nullcontext()
    with admission, file_lease(prefix / 'worker-admin.lock'):
        # Recheck after acquiring the same lock used by other worker admins.
        target, marker, parent = administrative_binding(instance)
        if action == 'stage':
            if bundle is None: raise ConfigError('worker bundle required')
            staged = _provision(target['root'], prefix, Path(bundle), sha256,
                uid=parent['service_uid'], execution=parent['execution'], reserve=RESERVE,
                stage_bootstrap=True)
            proposal = _control(prefix / 'candidates' / marker['installation_id'] / (sha256 + '.json'))
            value = {'format':1, 'parent_sha256':parent['bundle_sha256'], 'candidate':proposal}
            validate_candidate(parent, value)
            path = receipt_path(parent, sha256)
            for folder in (path.parent.parent, path.parent):
                folder.mkdir(mode=0o755, exist_ok=True); protected(folder); folder.chmod(0o755)
            previous = _control(path, optional=True)
            if previous is not None and previous != value:
                raise ConfigError('worker approval changed')
            _write(path, value)
            return {'ok':True, 'phase':'staged', 'candidate_sha256':sha256,
                    'release':staged['release'], 'controller_unchanged':True, 'rooms_affected':False}
        job = read_job(parent)
        previous = (_control(result_path(target['root'], job['id']), parent['service_uid'],
                             private=True, optional=True) if job else None)
        if action=='cancel' and deployment_id is not None:
            try:
                if str(uuid.UUID(deployment_id))!=deployment_id:raise ValueError
            except (ValueError,TypeError,AttributeError):raise ConfigError('invalid worker cancellation identity')from None
            if not job or job['id']!=deployment_id or sha256 is not None and job['candidate_sha256']!=sha256:
                raise ConfigError('worker cancellation request identity changed')
        if action == 'queue':
            from .operator_maintenance import require_idle
            require_idle(target,parent)
            controller = _control(prefix / 'controller-update.json', optional=True)
            if controller and controller.get('phase') not in ('complete','cancelled'):
                raise ConfigError('controller deployment must finish or be resolved first')
            if deployment_id is not None:
                try:
                    if str(uuid.UUID(deployment_id)) != deployment_id: raise ValueError
                except (ValueError, TypeError, AttributeError):
                    raise ConfigError('invalid worker deployment request identity') from None
                if job and job['id'] == deployment_id:
                    if job['candidate_sha256'] != sha256 or (rooms is not None and job['rooms'] != list(rooms)):
                        raise ConfigError('worker deployment request identity conflict')
                    return {'ok':True, 'deployment':job, 'result':previous,
                            'controller_unchanged':True, 'occupied_rooms_interrupted':False}
            from .layout import load
            from .ratings.operations import _identity
            with _identity(parent['service_uid']): _, config = load(target['root'])
            aliases = list(rooms or [r['alias'] for r in config['rooms']])
            if not aliases or len(set(aliases)) != len(aliases) or any(a not in {r['alias'] for r in config['rooms']} for a in aliases):
                raise ConfigError('select existing room aliases')
            approved = _control(protected(receipt_path(parent, sha256)))
            validate_candidate(parent, approved)
            if job and (not previous or previous.get('phase') not in ('complete','failed','cancelled')):
                raise ConfigError('finish or cancel the existing worker deployment first')
            job = {'format':1, 'id':deployment_id or str(uuid.uuid4()), 'phase':'queued',
                'parent_sha256':parent['bundle_sha256'], 'installation_id':marker['installation_id'],
                'generation':marker['generation'], 'candidate_sha256':sha256, 'rooms':aliases}
            _write(prefix / NAME, job)
            previous = None
        elif action == 'cancel' and job:
            job = {**job, 'phase':'cancelled'}; _write(prefix / NAME, job)
        return {'ok':True, 'deployment':job, 'result':previous,
                'controller_unchanged':True, 'occupied_rooms_interrupted':False}
