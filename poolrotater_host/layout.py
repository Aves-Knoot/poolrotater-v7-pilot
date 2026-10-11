"""Idempotent explicit-root setup and safe host-local file access."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

from .config import ConfigError, contained, defaults, validate

FORMAT = 1
DIRECTORIES = ('config', 'state', 'catalogs', 'releases', 'downloads', 'logs', 'secrets', 'backups')


def checked_root(value: Path) -> Path:
    # Check lexical ancestors before abspath collapses '..'; otherwise a
    # symlink/../ component can disagree with later filesystem resolution.
    raw = Path(value).absolute()
    for item in (raw, *raw.parents):
        if item.is_symlink():
            raise ConfigError('host root cannot contain symlinks')
    path = Path(os.path.abspath(raw))
    if path.exists() and (not path.is_dir() or path.stat().st_uid != os.geteuid()):
        raise ConfigError('host root must be a directory owned by the current user')
    return path


def read_json(path: Path, maximum: int = 65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r', encoding='utf-8') as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > maximum:
            raise ConfigError('expected a bounded regular JSON file')
        if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077 or st.st_nlink != 1:
            raise ConfigError('host JSON files must be private, owned, and not hard-linked')
        try:
            return json.load(handle, object_pairs_hook=unique_fields,
                             parse_constant=lambda _: (_ for _ in ()).throw(ConfigError('non-finite JSON number')))
        except (ValueError, UnicodeError) as exc:
            raise ConfigError('invalid JSON file') from exc


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError('duplicate JSON fields')
        result[key] = value
    return result


def atomic_json(path: Path, data, *, mode=0o600):
    if mode not in (0o600, 0o644) or (mode == 0o644 and os.geteuid() != 0):
        raise ConfigError('invalid atomic JSON permission policy')
    if path.is_symlink():
        raise ConfigError('refusing symlink destination')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.pending-', delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def file_lease(path: Path, *, timeout=0.0):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
        raise ConfigError('invalid lease timeout')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) & 0o077:
            raise ConfigError('invalid lease file owner or type')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConfigError('another process already owns this installation/account') from None
                time.sleep(max(0, min(.02, deadline - time.monotonic())))
        yield
    finally:
        os.close(fd)


def load(root: Path) -> tuple[dict, dict]:
    root = checked_root(root)
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise ConfigError('host root must be private (0700)')
    marker = read_json(contained(root, '.poolrotater-host.json'))
    if not isinstance(marker, dict) or set(marker) != {'format', 'installation_id', 'generation'} or type(marker['format']) is not int or marker['format'] != FORMAT:
        raise ConfigError('invalid installation marker')
    for key in ('installation_id', 'generation'):
        try: uuid.UUID(marker[key])
        except (ValueError, TypeError, AttributeError) as exc: raise ConfigError('invalid installation identity') from exc
    for directory in DIRECTORIES:
        path = contained(root, directory)
        if not path.is_dir() or path.stat().st_uid != os.geteuid():
            raise ConfigError('missing or foreign-owned host directory')
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ConfigError('host directories must be private (0700)')
    config = validate(read_json(contained(root, 'config/host.json')))
    for room in config['rooms']:
        contained(root, room['catalog'])
    return marker, config


def setup(value: Path, presets=None) -> dict:
    config = defaults(presets)
    root = checked_root(value)
    existed = root.exists()
    if existed and set(p.name for p in root.iterdir()) - {'.setup.lock'} and not (root / '.poolrotater-host.json').is_file():
        raise ConfigError('refusing nonempty directory without a host marker')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise ConfigError('host root must be private (0700)')
    with file_lease(root / '.setup.lock'):
        marker_path = contained(root, '.poolrotater-host.json')
        if not marker_path.exists():
            atomic_json(marker_path, {'format': FORMAT, 'installation_id': str(uuid.uuid4()), 'generation': str(uuid.uuid4())})
        # The marker precedes directory/config creation so a failed setup can resume.
        for directory in DIRECTORIES:
            path = contained(root, directory)
            path.mkdir(mode=0o700, exist_ok=True)
        config_path = contained(root, 'config/host.json')
        if not config_path.exists():
            atomic_json(config_path, config)
        marker, actual = load(root)
        if presets is not None and [r['preset'] for r in actual['rooms']] != presets:
            raise ConfigError('existing room configuration differs; setup never overwrites it')
        return {'installation_id': marker['installation_id'], 'root': str(root), 'rooms': len(actual['rooms']), 'execution': 'offline'}


@contextmanager
def _namespace_lease(key):
    # Same-UID offline hosts share this lock namespace. Never touches v6 locks.
    # Fixed Linux path: changing TMPDIR must not bypass account ownership.
    directory = Path('/tmp') / f'poolrotater-v7-locks-{os.geteuid()}'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.geteuid() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise ConfigError('unsafe account lock directory')
    with file_lease(directory / f'{key}.lock'):
        yield


@contextmanager
def account_lease(account_id):
    if type(account_id) is not int or account_id <= 0:
        raise ConfigError('account identity must be a positive integer')
    with _namespace_lease(f'account-{account_id}'):
        yield


@contextmanager
def owner_lease(root: Path, *, account_id=None):
    marker, config = load(root)
    account = config['account']['expected_user_id']
    if account_id is not None:
        if type(account_id) is not int or account_id <= 0:
            raise ConfigError('account identity must be a positive integer')
        if account is not None and account != account_id:
            raise ConfigError('account identity does not match expected_user_id')
        account = account_id
    with file_lease(contained(root, 'state/owner.lock')):
        if account is None:
            with _namespace_lease(f'install-{marker["installation_id"]}'):
                yield
        else:
            with account_lease(account):
                yield
