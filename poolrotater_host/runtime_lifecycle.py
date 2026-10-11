"""Stopped-only, journaled administrative release switches in offline fixtures."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import stat
import uuid

from .config import ConfigError
from .layout import file_lease
from .provisioning import _control, _host_identity, _pointer, _write, protected, snapshot
from .runtime_package import digest, fixture_only

REGISTRATION_KEYS = {'format', 'installation_id', 'generation', 'prefix', 'execution', 'live_ready',
                     'service_uid', 'host_root', 'release', 'environment', 'bundle_sha256',
                     'environment_id', 'release_snapshot'}


def validate_runtime(record, prefix):
    """Complete protected files/paths first; then parse the legacy manifest."""
    prefix = protected(Path(prefix))
    if (not isinstance(record, dict) or set(record) != REGISTRATION_KEYS
            or type(record['format']) is not int or record['format'] != 1
            or record['execution'] != 'offline-fixture' or record['live_ready'] is not False
            or type(record['service_uid']) is not int or record['service_uid'] <= 0
            or record['prefix'] != str(prefix)
            or not all(isinstance(record[k], str) for k in
                       ('installation_id', 'generation', 'host_root', 'release', 'environment'))
            or not all(isinstance(record[k], str) and re.fullmatch('[a-f0-9]{64}', record[k])
                       for k in ('bundle_sha256', 'environment_id'))):
        raise ConfigError('invalid protected runtime registration')
    release = protected(Path(record['release']))
    environment = protected(Path(record['environment']))
    if (release.parent != prefix / 'releases' or environment.parent != prefix / 'environments'
            or environment.name != record['environment_id']):
        raise ConfigError('protected runtime path authority mismatch')
    if snapshot(release) != record['release_snapshot']:
        raise ConfigError('protected runtime code changed')
    path = environment.with_suffix('.json')
    protected(path)
    env_record = _control(path)
    if (not isinstance(env_record, dict)
            or set(env_record) != {'format', 'environment_id', 'snapshot', 'environment_hash'}
            or type(env_record['format']) is not int or env_record['format'] != 1
            or env_record['environment_id'] != environment.name
            or snapshot(environment, python_links=True) != env_record['snapshot']):
        raise ConfigError('protected runtime environment changed')
    from rolling.release import environment_hash, load_release
    manifest = load_release(release)
    if (release.name != manifest['version'] + '-' + record['bundle_sha256'][:16]
            or Path(manifest['python']).absolute() != environment / 'bin/python'
            or env_record['environment_hash'] != environment_hash(environment / 'bin/python')):
        raise ConfigError('protected interpreter/manifest authority mismatch')
    return {'release': release, 'environment': environment, 'manifest': manifest, 'registration': record}


def transition_path(prefix, installation_id):
    return Path(prefix) / 'transitions' / (installation_id + '.json')


def read_transition(prefix, installation_id):
    parent = Path(prefix) / 'transitions'
    if parent.exists(): protected(parent)
    value = _control(transition_path(prefix, installation_id), optional=True)
    if value is None: return None
    if (not isinstance(value, dict)
            or set(value) != {'format', 'installation_id', 'generation', 'transition_id',
                              'operation', 'phase', 'before', 'after'}
            or type(value['format']) is not int or value['format'] != 1
            or value['installation_id'] != installation_id
            or value['operation'] not in ('activate', 'rollback')
            or value['phase'] not in ('prepared', 'committed', 'aborted')
            or not isinstance(value['before'], dict) or not isinstance(value['after'], dict)):
        raise ConfigError('invalid runtime transition journal')
    return value


def require_no_pending(prefix, installation_id):
    value = read_transition(prefix, installation_id)
    if value and value['phase'] == 'prepared':
        raise ConfigError('runtime transition requires explicit offline recovery')


def store_candidate(prefix, proposal):
    parent = Path(prefix) / 'candidates' / proposal['installation_id']
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    protected(parent)
    path = parent / (proposal['bundle_sha256'] + '.json')
    previous = _control(path, optional=True)
    if previous is not None and previous != proposal:
        raise ConfigError('candidate registration changed')
    if previous is None: _write(path, proposal)


def install_bootstrap(prefix, source):
    path = Path(prefix) / 'bootstrap.py'
    record_path = Path(prefix) / 'bootstrap.json'
    expected = {'format': 1, 'sha256': digest(source)}
    previous = _control(record_path, optional=True)
    if previous is not None:
        protected(path)
        if previous != expected or path.stat().st_nlink != 1 or digest(path) != expected['sha256']:
            raise ConfigError('bootstrap change requires a coordinated administrative upgrade')
        return
    if path.exists() or path.is_symlink():
        # Recover only our matching root-owned partial file, never adopt unknown code.
        protected(path)
        if path.stat().st_nlink != 1 or digest(path) != expected['sha256']:
            raise ConfigError('unrecognized partial service bootstrap')
    else:
        with path.open('xb') as output, Path(source).open('rb') as stream:
            while chunk := stream.read(1024**2): output.write(chunk)
            output.flush(); os.fsync(output.fileno())
    path.chmod(0o444)
    _write(record_path, expected)


def _administrative_binding(root, uid):
    fixture_only()
    if os.environ.get('POOLROTATER_PROVISIONING_SANDBOX') != '1' or os.geteuid() != 0:
        raise ConfigError('runtime switches require the isolated administrator fixture')
    root = Path(root)
    if not root.is_relative_to('/state'):
        raise ConfigError('runtime switches are confined to fixture state')
    marker = _host_identity(root, uid)
    pointer = _control(root / 'state/runtime-installation.json', uid, private=True)
    if not isinstance(pointer, dict) or not isinstance(pointer.get('prefix'), str):
        raise ConfigError('invalid runtime pointer')
    prefix = protected(Path(pointer['prefix']))
    if not prefix.is_relative_to('/runtime') or pointer != _pointer(marker, prefix):
        raise ConfigError('runtime switch identity mismatch')
    path = prefix / 'installations' / (marker['installation_id'] + '.json')
    protected(path)
    record = _control(path)
    binding = {**pointer, 'service_uid': uid, 'host_root': str(root)}
    if not isinstance(record, dict) or any(record.get(k) != v for k, v in binding.items()):
        raise ConfigError('runtime switch belongs to another operator/installation')
    return root, prefix, marker, path, record


@contextmanager
def _stopped(root, uid):
    """Use the service's existing owner lock; never replace/remove its inode."""
    fd = None
    try:
        fd = os.open(root / 'state/owner.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1
                or stat.S_IMODE(st.st_mode) & 0o077):
            raise ConfigError('unsafe service owner lock')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ConfigError('service must be stopped before a runtime switch') from None
        from .ownership_recovery import MAXIMUM, RECOVERY_PATH, validate_record
        recovery = _control(root / RECOVERY_PATH, uid, private=True, optional=True, maximum=MAXIMUM)
        if recovery is not None:
            validate_record(recovery, root=root, marker=_host_identity(root, uid), uid=uid)
            if recovery['execution'] != 'offline-fixture':
                from .provider_room_state import BINDING_PATH
                room_binding = _control(root / BINDING_PATH, uid, private=True, maximum=4096)
                expected = {k: v for k, v in recovery['binding'].items() if k != 'config_sha256'}
                expected['format'] = 1
                if (type(room_binding) is not dict or set(room_binding) != set(expected)
                        or any(type(room_binding[k]) is not type(v) or room_binding[k] != v
                               for k, v in expected.items())):
                    raise ConfigError('provider room recovery belongs to another authority')
            if recovery['phase'] == 'prepared':
                raise ConfigError('ownership recovery requires explicit complete or abort before a runtime switch')
        ledger = _control(root / 'state/rolling/ownership.json', uid)
        if (not isinstance(ledger, dict) or set(ledger) != {'format', 'rooms'}
                or type(ledger['format']) is not int or ledger['format'] != 1
                or not isinstance(ledger['rooms'], dict)):
            raise ConfigError('invalid service ownership journal')
        for state in ledger['rooms'].values():
            if (not isinstance(state, dict) or state.get('phase') != 'stopped'
                    or state.get('room_id') or state.get('ownership_unknown')
                    or state.get('operation') in ('make_pending', 'close_pending')):
                raise ConfigError('room ownership requires reconciliation before a runtime switch')
        yield
    except OSError:
        raise ConfigError('a successful stopped service and checked ownership journal are required') from None
    finally:
        if fd is not None: os.close(fd)


def switch(root, bundle_sha256, *, uid=1000, operation='activate'):
    root, prefix, marker, path, current = _administrative_binding(root, uid)
    if operation not in ('activate', 'rollback') or not isinstance(bundle_sha256, str) or not re.fullmatch('[a-f0-9]{64}', bundle_sha256):
        raise ConfigError('explicit registered runtime digest and switch operation required')
    with file_lease(prefix / '.provision.lock'), _stopped(root, uid):
        # Reread under both leases; another administrator may have switched first.
        _, _, _, _, current = _administrative_binding(root, uid)
        require_no_pending(prefix, marker['installation_id'])
        candidate_path = prefix / 'candidates' / marker['installation_id'] / (bundle_sha256 + '.json')
        target = _control(candidate_path)
        protected(candidate_path)
        for key in ('installation_id', 'generation', 'service_uid', 'host_root', 'prefix'):
            if not isinstance(target, dict) or target.get(key) != current[key]:
                raise ConfigError('candidate belongs to another installation')
        old = validate_runtime(current, prefix)
        new = validate_runtime(target, prefix)
        from rolling.release import require_compatible
        require_compatible(old['manifest'], new['manifest'])
        if target == current: return {'status': 'existing', 'live_ready': False}
        parent = prefix / 'transitions'; parent.mkdir(mode=0o755, exist_ok=True); protected(parent)
        journal = {'format': 1, 'installation_id': marker['installation_id'], 'generation': marker['generation'],
                   'transition_id': str(uuid.uuid4()), 'operation': operation, 'phase': 'prepared',
                   'before': current, 'after': target}
        journal_path = transition_path(prefix, marker['installation_id'])
        _write(journal_path, journal)
        _write(path, target)
        _write(journal_path, {**journal, 'phase': 'committed'})
        return {'status': 'activated' if operation == 'activate' else 'rolled_back',
                'release': str(new['release']), 'live_ready': False}


def recover(root, *, action, uid=1000):
    root, prefix, marker, path, current = _administrative_binding(root, uid)
    if action not in ('complete', 'abort'):
        raise ConfigError('explicit complete or abort recovery required')
    with file_lease(prefix / '.provision.lock'), _stopped(root, uid):
        _, _, _, _, current = _administrative_binding(root, uid)
        journal = read_transition(prefix, marker['installation_id'])
        if not journal or journal['phase'] != 'prepared' or journal['generation'] != marker['generation']:
            raise ConfigError('no matching pending runtime transition')
        if current != journal['before'] and current != journal['after']:
            raise ConfigError('runtime changed outside the pending transition')
        target = journal['after'] if action == 'complete' else journal['before']
        for key in ('installation_id', 'generation', 'service_uid', 'host_root', 'prefix'):
            if target.get(key) != current[key]: raise ConfigError('recovery runtime identity changed')
        runtime = validate_runtime(target, prefix)
        if current != target: _write(path, target)
        _write(transition_path(prefix, marker['installation_id']),
               {**journal, 'phase': 'committed' if action == 'complete' else 'aborted'})
        return {'status': 'recovered_' + action, 'release': str(runtime['release']), 'live_ready': False}
