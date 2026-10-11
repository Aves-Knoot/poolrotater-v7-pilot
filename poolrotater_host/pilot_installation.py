"""Fresh, manually pinned protected pilot provisioning; no account or network I/O.

Administrative execution is explicit. Nothing starts/enables/reloads a unit.
Paths and the dedicated non-login account are fixed by a validated instance name.
An interrupted installation is retried only against its original artifact/UID.
"""
from contextlib import contextmanager
import fcntl
import grp
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import uuid

from .config import ConfigError, defaults
from .layout import DIRECTORIES, file_lease
from .pilot_identity import CANDIDATE, LIVE
from .provisioning import _control, _host_identity, _provision, _write, protected
from .runtime_package import inspect

CODE_BASE = Path('/opt/poolrotater-v7-pilot')
STATE_BASE = Path('/var/lib/poolrotater-v7-pilot')
RESERVE = 5 * 1024**3


def paths(instance):
    if type(instance) is not str or not re.fullmatch('[a-z][a-z0-9-]{0,15}', instance):
        raise ConfigError('pilot instance requires a short lowercase identifier')
    return {'prefix': CODE_BASE / instance, 'root': STATE_BASE / instance,
            'user': 'prv7-' + instance, 'unit': 'poolrotater-v7-pilot-' + instance + '.service'}


def _platform():
    import platform
    if (platform.system() != 'Linux' or platform.machine() != 'x86_64'
            or platform.python_version_tuple()[:2] != ('3', '12')
            or platform.freedesktop_os_release().get('ID') != 'ubuntu'
            or platform.freedesktop_os_release().get('VERSION_ID') != '24.04'
            or Path('/proc/1/comm').read_text().strip() != 'systemd'):
        raise ConfigError('protected pilot requires Ubuntu 24.04 x86_64 Python 3.12 and systemd')


def _admin():
    if os.geteuid() != 0:
        raise ConfigError('protected pilot provisioning requires administrator execution')
    if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') == '1':
        raise ConfigError('fixture mounts cannot create a supported pilot installation')
    _platform()


def _parent(path):
    protected(path.parent)
    if not path.exists(): path.mkdir(mode=0o755)
    return protected(path)


def _available_uid():
    used = {v.pw_uid for v in pwd.getpwall()} | {v.gr_gid for v in grp.getgrall()}
    uid = next((value for value in range(999, 99, -1) if value not in used), None)
    if uid is None: raise ConfigError('no unused dedicated system UID/GID available')
    return uid


def _account(name, root, *, create, expected_uid=None):
    if expected_uid is not None and (type(expected_uid) is not int or not 100 <= expected_uid <= 999):
        raise ConfigError('invalid reserved pilot service UID')
    try: entry = pwd.getpwnam(name)
    except KeyError:
        if not create: raise ConfigError('dedicated pilot service identity is missing') from None
        # The stored gid deliberately equals uid. Default useradd allocation can
        # choose different numbers when group and user inventories differ.
        uid = _available_uid() if expected_uid is None else expected_uid
        if uid in ({v.pw_uid for v in pwd.getpwall()} | {v.gr_gid for v in grp.getgrall()}):
            raise ConfigError('reserved pilot service UID/GID is now occupied')
        result = subprocess.run(['/usr/sbin/useradd', '--system', '--user-group', '--uid', str(uid),
            '--no-create-home', '--home-dir', str(root), '--shell', '/usr/sbin/nologin', name],
            env={'PATH': '/usr/sbin:/usr/bin:/bin', 'LANG': 'C.UTF-8'},
            capture_output=True, timeout=30)
        if result.returncode: raise ConfigError('dedicated pilot service identity creation failed')
        entry = pwd.getpwnam(name)
    if (entry.pw_uid <= 0 or entry.pw_gid != entry.pw_uid
            or (expected_uid is not None and entry.pw_uid != expected_uid)
            or entry.pw_dir != str(root) or entry.pw_shell != '/usr/sbin/nologin'):
        raise ConfigError('existing account is not the dedicated pilot service identity')
    return entry


def _publish_intent(prefix, intent):
    """Publish the complete durable transaction before creating its account."""
    from .catalog_download import _publish
    with tempfile.TemporaryDirectory(prefix='.pilot-intent-', dir=CODE_BASE) as directory:
        stage = Path(directory) / 'installation'; stage.mkdir(mode=0o755)
        _write(stage / 'pilot-install.json', intent)
        fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        _publish(stage, prefix)
        fd = os.open(CODE_BASE, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)


@contextmanager
def _stopped(root, uid):
    """No journal reset/adoption; coordinate with authorization and workers."""
    path = root / 'state/owner.lock'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != uid
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise ConfigError('invalid pilot owner lock')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ConfigError('pilot installation must be stopped') from None
        journal = root / 'state/rolling/ownership.json'
        if os.path.lexists(journal):
            from .reconciliation import _journal
            from .config import validate
            config = validate(_control(root / 'config/host.json', uid, private=True))
            value = _journal(_control(journal, uid, private=True), {r['alias'] for r in config['rooms']})
            if any(s.get('phase') != 'stopped' or s.get('room_id') or s.get('ownership_unknown')
                   or s.get('operation') in ('make_pending', 'close_pending') for s in value['rooms'].values()):
                raise ConfigError('pilot ownership requires stopped reconciliation')
        recovery = _control(root / 'state/ownership-recovery.json', uid, private=True, optional=True)
        if recovery is not None:
            from .ownership_recovery import validate_record
            validate_record(recovery, root=root, marker=_host_identity(root, uid), uid=uid)
            if recovery['phase'] == 'prepared':
                raise ConfigError('pilot ownership recovery is pending')
        yield
    finally: os.close(fd)


def install(instance, bundle, expected_sha256, *, presets=None, live_pilot=False):
    """Install disabled/stopped into fresh paths, or retry the exact transaction.

    Artifact inspection and disk reserve checks precede service user/state writes.
    The root-owned intent is not live authority; full registration/sealing remains
    mandatory. No fixture, rehearsal, legacy directory or other release is adopted.
    """
    _admin()
    if type(live_pilot) is not bool: raise ConfigError('explicit pilot artifact choice required')
    if live_pilot and presets is None: presets = ['standard-auto']
    return _install(instance, bundle, expected_sha256, presets=presets, execution=LIVE if live_pilot else CANDIDATE)


def _install(instance, bundle, expected_sha256, *, presets, execution):
    """Shared provisioning core; qualification must prove its donor role first."""
    _execution_admin(execution)
    target = paths(instance)
    config = defaults(presets); config['execution'] = execution
    from .config import validate
    validate(config)
    spec = inspect(bundle, expected_sha256, execution=execution)
    from .runtime_package import digest
    if spec['interpreter_sha256'] != digest('/usr/bin/python3.12'):
        raise ConfigError('pilot interpreter differs from approved artifact')
    from .catalog_trust import PINS
    versions = {w['name']: w['version'] for w in spec['wheels']}
    if any(versions.get(name) != version for name, version in PINS.items()):
        raise ConfigError('pilot artifact must include the reviewed catalog dependencies')
    required = sum(f['bytes'] for f in spec['files'].values()) + sum(w['expanded_bytes'] for w in spec['wheels']) + 32 * 1024**2
    if any(shutil.disk_usage(p.parent).free < RESERVE + required for p in (CODE_BASE, STATE_BASE)):
        raise ConfigError('pilot installation would consume the five-GiB disk reserve')
    _parent(CODE_BASE); _parent(STATE_BASE)
    prefix, root = target['prefix'], target['root']
    # All instances serialize UID/GID reservation, not just their own prefix.
    with file_lease(CODE_BASE / '.install.lock'):
        intent_path = prefix / 'pilot-install.json'
        if os.path.lexists(prefix):
            protected(prefix)
            intent = _control(intent_path)
            fields = {'format', 'execution', 'bundle_sha256', 'service_uid', 'host_root',
                      'presets', 'installation_id', 'generation'}
            if (type(intent) is not dict or set(intent) not in (fields, fields | {'account_ready'})
                    or intent['format'] != 1 or intent['execution'] != execution
                    or type(intent.get('account_ready', True)) is not bool
                    or intent['bundle_sha256'] != expected_sha256 or intent['host_root'] != str(root)
                    or intent['presets'] != [r['preset'] for r in config['rooms']]):
                raise ConfigError('pilot installation retry differs from the original transaction')
        else:
            if os.path.lexists(root): raise ConfigError('pilot installer requires fresh state; no migration/adoption')
            # Refuse an unrelated preexisting identity rather than claim it.
            try: pwd.getpwnam(target['user'])
            except KeyError: pass
            else: raise ConfigError('pilot service name is already in use')
            intent = {'format': 1, 'execution': execution, 'bundle_sha256': expected_sha256,
                      'service_uid': _available_uid(), 'host_root': str(root), 'account_ready': False,
                      'presets': [r['preset'] for r in config['rooms']],
                      'installation_id': str(uuid.uuid4()), 'generation': str(uuid.uuid4())}
            _publish_intent(prefix, intent)
        ready = intent.get('account_ready', True)
        if not ready and os.path.lexists(root):
            raise ConfigError('unfinished service identity cannot adopt preexisting state')
        account = _account(target['user'], root, create=not ready, expected_uid=intent['service_uid'])
        if not ready:
            intent = {**intent, 'account_ready': True}
            _write(intent_path, intent)
        uid = account.pw_uid
        marker = {key: intent[key] for key in ('format', 'installation_id', 'generation')}
        if not os.path.lexists(root):
            root.mkdir(mode=0o700); os.chown(root, uid, uid)
        st = root.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or stat.S_IMODE(st.st_mode) != 0o700:
            raise ConfigError('pilot state ownership/type changed')
        for name in DIRECTORIES:
            path = root / name
            if not os.path.lexists(path):
                path.mkdir(mode=0o700); os.chown(path, uid, uid)
            st = path.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or stat.S_IMODE(st.st_mode) != 0o700:
                raise ConfigError('pilot state directory ownership/type changed')
        for path, value in ((root / '.poolrotater-host.json', marker), (root / 'config/host.json', config)):
            existing = _control(path, uid, private=True, optional=True)
            if existing is None: _write(path, value, owner=uid, private=True)
            elif path.name == '.poolrotater-host.json' and existing != value:
                raise ConfigError('pilot installation marker changed')
        lock = root / 'state/owner.lock'
        if not os.path.lexists(lock):
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try: os.fchown(fd, uid, uid); os.fsync(fd)
            finally: os.close(fd)
        with _stopped(root, uid):
            from .config import validate
            if validate(_control(root / 'config/host.json', uid, private=True))['execution'] != execution:
                raise ConfigError('pilot cannot adopt a rehearsal/fixture configuration')
            result = _provision(root, prefix, Path(bundle), expected_sha256,
                                uid=uid, execution=execution, reserve=RESERVE)
        return {**result, 'execution': execution, 'service_user': target['user'],
                'service_uid': uid, 'service_installed': False, 'service_started': False,
                'account_authorized': False, 'provider_dispatch_enabled': False}


def administrative_binding(instance):
    """Read-only root binding for manual catalog/unit/lifecycle operations."""
    _admin()
    intent = _control(protected(paths(instance)['prefix'] / 'pilot-install.json'))
    execution = intent.get('execution')
    if execution not in (CANDIDATE, LIVE):
        raise ConfigError('public pilot commands refuse qualification or legacy authority')
    return _administrative_binding(instance, execution=execution)


def _execution_admin(execution):
    from .pilot_identity import QUALIFICATION
    if execution in (CANDIDATE, LIVE):
        _admin()
    elif execution == QUALIFICATION:
        from .testing.pilot_qualification import require_donor
        require_donor()
    else:
        raise ConfigError('unsupported protected pilot installation authority')


def _administrative_binding(instance, *, execution):
    _execution_admin(execution)
    target = paths(instance)
    root, prefix = target['root'], protected(target['prefix'])
    account = _account(target['user'], root, create=False)
    marker = _host_identity(root, account.pw_uid)
    from .provisioning import _pointer
    pointer = _pointer(marker, prefix)
    if _control(root / 'state/runtime-installation.json', account.pw_uid, private=True) != pointer:
        raise ConfigError('pilot runtime pointer changed')
    record = _control(protected(prefix / 'installations' / (marker['installation_id'] + '.json')))
    if (type(record) is not dict or any(record.get(k) != v for k, v in {**pointer, 'execution': execution,
            'host_root': str(root), 'service_uid': account.pw_uid, 'live_ready': False}.items())):
        raise ConfigError('pilot registration belongs to another authority')
    from .runtime_lifecycle import require_no_pending, validate_runtime
    require_no_pending(prefix, marker['installation_id'])
    validate_runtime(record, prefix)
    return target, marker, record
