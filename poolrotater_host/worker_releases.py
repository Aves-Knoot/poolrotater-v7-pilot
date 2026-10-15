"""Root-approved worker releases, separate from the account-owner registration.

Worker promotion never changes the controller, interpreter, dependencies, stores,
or credential authority. Approval is specific to one installation and parent.
"""
from pathlib import Path
import os
import re

from .config import ConfigError
from .provisioning import _control, protected
from .runtime_lifecycle import validate_runtime


def receipt_path(parent, digest):
    if type(digest) is not str or not re.fullmatch('[a-f0-9]{64}', digest):
        raise ConfigError('explicit worker runtime digest required')
    return Path(parent['prefix']) / 'worker-releases' / parent['installation_id'] / (digest + '.json')


def require_worker_contract(parent, candidate):
    from rolling.release import require_compatible
    require_compatible(parent, candidate)
    if parent.get('migrations') != [] or candidate.get('migrations') != []:
        raise ConfigError('worker updates cannot run database migrations')
    # Native shared/storage fingerprints remain mandatory. Additionally keep
    # every controller/host/launcher file identical, including this validator.
    # New worker-local bot modules are permitted; shared bot modules are already
    # covered by require_compatible. A host change needs a coordinated upgrade.
    before, after = parent['files'], candidate['files']
    for name in before.keys() | after.keys():
        if not name.startswith('bot/') and before.get(name) != after.get(name):
            raise ConfigError('worker update changes controller code: ' + name)
    if parent['version'] == candidate['version'] and before != after:
        raise ConfigError('changed worker code requires a distinct release version')


def validate_candidate(parent, value):
    if (type(value) is not dict or set(value) != {'format', 'parent_sha256', 'candidate'}
            or type(value['format']) is not int or value['format'] != 1
            or value['parent_sha256'] != parent['bundle_sha256']
            or type(value['candidate']) is not dict):
        raise ConfigError('worker approval belongs to another controller')
    candidate = value['candidate']
    for key in ('installation_id', 'generation', 'prefix', 'execution', 'service_uid',
                'host_root', 'environment', 'environment_id'):
        if candidate.get(key) != parent[key]:
            raise ConfigError('worker approval identity/environment changed')
    current = validate_runtime(parent, Path(parent['prefix']))
    result = validate_runtime(candidate, Path(parent['prefix']))
    require_worker_contract(current['manifest'], result['manifest'])
    from rolling.release import require_immutable
    if os.geteuid() == 0:
        from .ratings.operations import _identity
        with _identity(parent['service_uid']):
            require_immutable(result['release'], result['manifest'])
    else:
        require_immutable(result['release'], result['manifest'])
    return result


def load_worker(root, release, *, prepared=False):
    """Revalidate parent authority before every prepare, rollback and restart."""
    from .provisioning import load_provisioned
    current = load_provisioned(root, prepared=prepared)
    requested = Path(release).absolute()
    if requested == current['release']:
        return current
    parent = current['registration']
    # Discover by canonical protected paths only; never search operator data
    # for executable candidates or accept a caller-supplied approval filename.
    folder = Path(parent['prefix']) / 'worker-releases' / parent['installation_id']
    if folder.exists():
        protected(folder)
        for path in sorted(folder.glob('*.json')):
            protected(path)
            value = _control(path)
            candidate = value.get('candidate', {}) if type(value) is dict else {}
            if candidate.get('release') != str(requested):
                continue
            if path != receipt_path(parent, candidate.get('bundle_sha256')):
                raise ConfigError('worker approval filename changed')
            return validate_candidate(parent, value)
    raise ConfigError('worker release has no protected approval for this controller')


def retired_controller(parent, pinned, row):
    if (parent is None or row.get('phase') != 'stopped' or row.get('operation') != 'closed'
            or row.get('room_id') or row.get('ownership_unknown')):
        return False
    value = _control(Path(parent['prefix']) / 'pilot-upgrade.json', optional=True, maximum=512*1024)
    committed = (type(value) is dict and value.get('phase') == 'committed'
            and value.get('after') == parent and type(value.get('before')) is dict
            and all(value['before'].get(k) == parent[k] for k in
                ('installation_id','generation','host_root','service_uid','prefix')))
    if not committed: return False
    before = value['before']
    if before.get('release') == str(pinned): return True
    # A stopped controller upgrade also retires workers explicitly approved for
    # its predecessor. Validate the old protected approval and complete immutable
    # contract; an arbitrary last_good path is never sufficient authority.
    folder = Path(parent['prefix'])/'worker-releases'/parent['installation_id']
    if not folder.exists(): return False
    protected(folder)
    for path in folder.glob('*.json'):
        approved = _control(protected(path))
        candidate = approved.get('candidate', {}) if type(approved) is dict else {}
        if candidate.get('release') != str(pinned): continue
        if path != receipt_path(before, candidate.get('bundle_sha256')):
            raise ConfigError('retired worker approval filename changed')
        validate_candidate(before, approved)
        return True
    return False
