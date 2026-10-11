"""Protected runtime provisioning prototype. Administrative writes are fixture-only.

Operator data remains private and separate. Venvs are built at their FINAL path;
code is staged then renamed. No user, systemd unit or live connection is created.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
import venv
import zipfile

from .config import ConfigError
from .layout import atomic_json, file_lease, load, read_json, unique_fields
from .runtime_package import digest, fixture_only, inspect


def protected(path):
    """Check every ancestor, not just the inherited guard's immediate parents."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts: raise ConfigError('protected paths must be absolute and canonical')
    for item in (path, *path.parents):
        st = item.lstat()
        if (stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o022
                or (os.geteuid() != 0 and os.access(item, os.W_OK))):
            raise ConfigError('runtime path or ancestor is writable/unprotected')
    return path


def _control(path, owner=0, *, private=False, optional=False, maximum=2 * 1024**2):
    try: fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if optional: return None
        raise ConfigError('runtime control record missing') from None
    except OSError: raise ConfigError('unsafe runtime control record') from None
    with os.fdopen(fd, 'r') as stream:
        st = os.fstat(stream.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != owner or st.st_size > maximum
                or stat.S_IMODE(st.st_mode) & (0o077 if private else 0o022)):
            raise ConfigError('unsafe runtime control owner/permissions/type')
        try:
            return json.load(stream, object_pairs_hook=unique_fields,
                             parse_constant=lambda _: (_ for _ in ()).throw(ConfigError('invalid runtime number')))
        except (ValueError, UnicodeError): raise ConfigError('invalid runtime control JSON') from None


def _write(path, value, *, owner=0, private=False):
    _control(path, owner, private=private, optional=True)
    atomic_json(path, value)
    os.chmod(path, 0o600 if private else 0o644)
    os.chown(path, owner, owner)
    with path.open('rb') as stream: os.fsync(stream.fileno())


def snapshot(root, *, python_links=False):
    protected(root)
    result = {'files': {}, 'links': {}}
    for path in sorted(Path(root).rglob('*')):
        relative = path.relative_to(root).as_posix()
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            expected = Path(root) / 'lib' if relative == 'lib64' else Path('/usr/bin/python3.12')
            if (not python_links or st.st_uid != 0 or relative not in ('lib64','bin/python','bin/python3','bin/python3.12')
                    or path.resolve() != expected):
                raise ConfigError('unexpected runtime symlink')
            protected(path.resolve())
            result['links'][relative] = os.readlink(path)
        elif stat.S_ISREG(st.st_mode):
            protected(path)
            if st.st_nlink != 1: raise ConfigError('hard-linked runtime file')
            result['files'][relative] = digest(path)
        elif stat.S_ISDIR(st.st_mode): protected(path)
        else: raise ConfigError('special runtime file')
    return result


def _seal(root):
    for path in [*Path(root).rglob('*'), Path(root)]:
        if path.is_symlink(): continue
        os.chmod(path, 0o555 if path.is_dir() or path.stat().st_mode & 0o111 else 0o444)


def _unsealed_remove(root):
    # Only called for an interrupted, unreferenced root-owned environment.
    if root.is_symlink() or root.stat().st_uid != 0: raise ConfigError('unsafe partial environment')
    for path in [root, *root.rglob('*')]:
        if path.is_symlink(): continue
        if path.stat().st_uid != 0: raise ConfigError('foreign partial environment')
        if path.is_dir(): path.chmod(0o755)
    shutil.rmtree(root)


def _host_identity(root, uid):
    root = Path(root)
    if not root.is_absolute() or '..' in root.parts or type(uid) is not int or uid <= 0:
        raise ConfigError('explicit operator root and non-root UID required')
    for path in (root, *root.parents):
        if path.is_symlink(): raise ConfigError('operator path cannot contain links')
    for path in (root, root / 'state'):
        if path.is_symlink(): raise ConfigError('operator state cannot contain links')
        st = path.stat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or stat.S_IMODE(st.st_mode) != 0o700:
            raise ConfigError('operator state must stay privately operator-owned')
    marker = _control(root / '.poolrotater-host.json', uid, private=True)
    if not isinstance(marker, dict) or set(marker) != {'format','installation_id','generation'} or type(marker['format']) is not int or marker['format'] != 1:
        raise ConfigError('invalid operator installation marker')
    try:
        for key in ('installation_id','generation'): uuid.UUID(marker[key])
    except (ValueError, TypeError, AttributeError): raise ConfigError('invalid operator identity') from None
    return marker


def _pointer(marker, prefix):
    return {'format':1,'installation_id':marker['installation_id'],'generation':marker['generation'],'prefix':str(prefix)}


def _environment(prefix, spec, stage):
    from rolling.release import environment_hash
    key = spec['environment_id']
    root = prefix / 'environments' / key
    record_path = root.with_suffix('.json')
    record = _control(record_path, optional=True)
    if record:
        if (set(record) != {'format','environment_id','snapshot','environment_hash'} or record['format'] != 1
                or record['environment_id'] != key or snapshot(root, python_links=True) != record['snapshot']
                or environment_hash(root / 'bin/python') != record['environment_hash']):
            raise ConfigError('sealed runtime environment changed')
        return root
    if root.exists():
        for path in [*(prefix / 'installations').glob('*.json'), *(prefix / 'candidates').glob('*/*.json')]:
            if _control(path).get('environment') == str(root):
                raise ConfigError('cannot reset a referenced environment')
        _unsealed_remove(root)
    # Build at final path; Python virtual environments are not relocated.
    venv.create(root, with_pip=True, symlinks=True)
    environment = {'PATH':str(root / 'bin') + ':/usr/bin:/bin','HOME':'/tmp','LANG':'C.UTF-8',
                   'PYTHONDONTWRITEBYTECODE':'1','PIP_NO_INDEX':'1','PIP_DISABLE_PIP_VERSION_CHECK':'1',
                   'PIP_NO_CACHE_DIR':'1','MALLOC_ARENA_MAX':'2'}
    command = [str(root / 'bin/python'), '-I', '-m', 'pip', 'install', '--no-index', '--require-hashes',
               '--no-compile', '--find-links',str(stage / 'wheelhouse'), '-r',str(stage / 'payload/requirements-hashed.lock')]
    result = subprocess.run(command, env=environment, cwd='/tmp', capture_output=True, text=True, timeout=180)
    if result.returncode: raise ConfigError('offline runtime dependency installation failed')
    result = subprocess.run([str(root / 'bin/python'), '-I', '-m', 'pip', 'check'], env=environment,
                            cwd='/tmp', capture_output=True, text=True, timeout=30)
    if result.returncode: raise ConfigError('runtime dependency closure failed')
    versions = subprocess.check_output([str(root / 'bin/python'), '-I', '-m', 'pip', 'list', '--format=json'], env=environment, cwd='/tmp', text=True)
    normalized = {re.sub(r'[-_.]+','-',v['name']).lower():v['version'] for v in json.loads(versions)}
    if set(normalized) != {'pip', *(w['name'] for w in spec['wheels'])} or any(normalized[w['name']] != w['version'] for w in spec['wheels']):
        raise ConfigError('unexpected distribution in runtime environment')
    _seal(root)
    _write(record_path, {'format':1,'environment_id':key,'snapshot':snapshot(root,python_links=True),
                         'environment_hash':environment_hash(root / 'bin/python')})
    return root


def provision(root, prefix, bundle, expected_sha256, *, uid=1000):
    fixture_only()
    if os.environ.get('POOLROTATER_PROVISIONING_SANDBOX') != '1' or os.geteuid() != 0:
        raise ConfigError('administrative provisioning requires offline-provisioning fixture')
    root, prefix, bundle = Path(root), Path(prefix), Path(bundle)
    # Administrative writes in this prototype can only target disposable tmpfs.
    if not prefix.is_relative_to('/runtime') or prefix == Path('/runtime') or not root.is_relative_to('/state'):
        raise ConfigError('provisioning writes are confined to fixture mounts')
    protected(prefix.parent)
    if prefix.exists(): protected(prefix)
    marker = _host_identity(root, uid)
    pointer_path = root / 'state/runtime-installation.json'
    pointer = _control(pointer_path, uid, private=True, optional=True)
    expected_pointer = _pointer(marker, prefix)
    if pointer is not None and pointer != expected_pointer: raise ConfigError('operator runtime binding changed')
    prefix.mkdir(mode=0o755, exist_ok=True)
    for name in ('releases','environments','installations','candidates','transitions','.staging'):
        path = prefix / name; path.mkdir(mode=0o755, exist_ok=True); protected(path)
    with file_lease(prefix / '.provision.lock'):
        from .runtime_lifecycle import install_bootstrap, require_no_pending, store_candidate
        require_no_pending(prefix, marker['installation_id'])
        installation_path = prefix / 'installations' / (marker['installation_id'] + '.json')
        current = _control(installation_path, optional=True)
        if current and any(current.get(k) != v for k,v in {**expected_pointer,'service_uid':uid,'host_root':str(root)}.items()):
            raise ConfigError('protected installation belongs to another root/generation/operator')
        with tempfile.TemporaryDirectory(prefix='package-',dir=prefix / '.staging') as directory:
            stage = Path(directory)
            # Stabilize user-provided input under root ownership before inspection/extraction.
            archive_path = stage / 'input.zip'
            fd = os.open(bundle,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as source, archive_path.open('xb') as target:
                from .runtime_package import MAX_BUNDLE
                st = os.fstat(source.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_BUNDLE:
                    raise ConfigError('runtime input must be a bounded regular file')
                copied = 0
                while chunk := source.read(1024**2):
                    copied += len(chunk)
                    if copied > MAX_BUNDLE: raise ConfigError('runtime input grew beyond quota')
                    target.write(chunk)
            spec = inspect(archive_path, expected_sha256)
            if spec['interpreter_sha256'] != digest('/usr/bin/python3.12'):
                raise ConfigError('runtime interpreter differs from bundle target')
            required = sum(f['bytes'] for f in spec['files'].values()) + sum(w['expanded_bytes'] for w in spec['wheels']) + 32*1024**2
            if shutil.disk_usage(prefix).free < required: raise ConfigError('insufficient fixture runtime space')
            with zipfile.ZipFile(archive_path) as archive:
                for name in spec['files']:
                    destination = stage / name
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    with archive.open(name) as source, destination.open('xb') as target:
                        shutil.copyfileobj(source,target,length=1024**2)
            environment = _environment(prefix,spec,stage)
            release = prefix / 'releases' / (spec['version'] + '-' + expected_sha256[:16])
            if not release.exists():
                from rolling.release import build_manifest
                payload = stage / 'payload'
                manifest = build_manifest(payload,spec['version'],environment / 'bin/python')
                manifest['validation'] = {'tests':1,'offline_fixture':True,'scope':'verified local package; full M1/live gate open'}
                _write(payload / 'release.json',manifest)
                _seal(payload)
                os.rename(payload,release)
            protected(release)
            expected_files = {n.removeprefix('payload/'):v['sha256'] for n,v in spec['files'].items() if n.startswith('payload/')}
            actual_files = snapshot(release)['files']
            if set(actual_files) != {*expected_files,'release.json'} or any(actual_files[k] != v for k,v in expected_files.items()):
                raise ConfigError('installed runtime differs from artifact')
            from rolling.release import load_release
            manifest = load_release(release)
            release_snapshot = snapshot(release)
            proposal = {**expected_pointer,'execution':'offline-fixture','live_ready':False,'service_uid':uid,
                        'host_root':str(root),'release':str(release),'environment':str(environment),
                        'bundle_sha256':expected_sha256,'environment_id':spec['environment_id'],
                        'release_snapshot':release_snapshot}
            install_bootstrap(prefix, release / 'poolrotater_host/service_bootstrap.py')
            store_candidate(prefix, proposal)
            if current and current['bundle_sha256'] != expected_sha256:
                return {'status':'candidate_staged','release':str(release),'environment':str(environment),'live_ready':False}
            if current and current != proposal: raise ConfigError('protected release binding changed')
            if current is None: _write(installation_path,proposal)
            if pointer is None: _write(pointer_path,expected_pointer,owner=uid,private=True)
            return {'status':'existing' if current else 'installed','release':str(release),
                    'environment':str(environment),'bundle_sha256':expected_sha256,'live_ready':False}


def load_provisioned(root, *, release=None, prepared=False):
    """Service-user preflight. Includes real immutable guard and complete file hashes."""
    if type(prepared) is not bool: raise ConfigError('invalid prepared runtime check')
    if not prepared: fixture_only()
    if os.geteuid() == 0: raise ConfigError('protected runtime checks require the service identity')
    root = Path(root).absolute()
    marker, _ = load(root)
    try:
        pointer = read_json(root / 'state/runtime-installation.json')
    except OSError:
        raise ConfigError('external runtime requires a protected installation binding') from None
    if not isinstance(pointer,dict) or set(pointer) != {'format','installation_id','generation','prefix'}:
        raise ConfigError('invalid operator runtime pointer')
    prefix = protected(Path(pointer['prefix']))
    if pointer != _pointer(marker,prefix): raise ConfigError('operator runtime identity changed')
    path = prefix / 'installations' / (marker['installation_id'] + '.json')
    protected(path)
    record = _control(path)
    from .runtime_lifecycle import REGISTRATION_KEYS, require_no_pending, validate_runtime
    keys = REGISTRATION_KEYS
    if (not isinstance(record,dict) or set(record) != keys or record['execution'] != 'offline-fixture' or record['live_ready'] is not False
            or any(record[k] != v for k,v in {**pointer,'service_uid':os.geteuid(),'host_root':str(root)}.items())):
        raise ConfigError('protected runtime registration mismatch')
    require_no_pending(prefix, marker['installation_id'])
    runtime = validate_runtime(record, prefix)
    expected_release, environment = runtime['release'], runtime['environment']
    if release is not None and Path(release).absolute() != expected_release: raise ConfigError('release is not the registered runtime')
    from rolling.release import require_immutable
    manifest = runtime['manifest']
    require_immutable(expected_release,manifest)
    return {'release':expected_release,'environment':environment,'manifest':manifest,'registration':record}
