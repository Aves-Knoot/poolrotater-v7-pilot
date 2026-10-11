"""Deterministic local runtime bundle, independent of host data and preview wheel.

Only pinned cached wheels are bundled; no resolution/download/build hook occurs.
Inspection requires an externally supplied trusted digest before provisioning.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import tempfile
import zipfile

from .config import ConfigError
from .layout import unique_fields

MAX_BUNDLE = 128 * 1024**2
ROOT_SOURCES = ('run_bot.py', 'run_multiroom.py', 'run_rolling.py', 'run_room_worker.py',
                'offline_host_controller.py', 'check_protected_runtime.py', 'deployment/sqlite_readonly.py')
TARGET = {'os': 'linux', 'architecture': 'x86_64', 'python': '3.12', 'reference_os': 'Ubuntu 24.04'}


def fixture_only():
    if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1':
        raise ConfigError('runtime packaging requires offline isolation')


def digest(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def json_bytes(value): return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def safe_name(name):
    if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_./+-]+', name)
            or name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/'))
            or str(PurePosixPath(name)) != name):
        raise ConfigError('unsafe runtime member path')
    return name


def pins(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+) --hash=sha256:([a-f0-9]{64})', line)
        if not match: raise ConfigError('runtime dependencies must be exact single-hash pins')
        name = re.sub(r'[-_.]+', '-', match[1]).lower()
        if name in result: raise ConfigError('duplicate runtime dependency')
        result[name] = {'name': name, 'version': match[2], 'sha256': match[3]}
    if not result: raise ConfigError('empty runtime dependency closure')
    return result


def source_names(root):
    names = set(ROOT_SOURCES) | {'requirements.txt', 'requirements-hashed.lock'}
    for folder in ('bot', 'rolling', 'poolrotater_host'):
        names.update(p.relative_to(root).as_posix() for p in (root / folder).rglob('*.py'))
    return sorted(names)


def environment_id(spec):
    execution = spec.get("execution", "offline-fixture")
    from .pilot_identity import installation_execution
    installation_execution(execution)
    extra = {} if execution == "offline-fixture" else {"execution": execution}
    return hashlib.sha256(json_bytes({'target': spec['target'], 'interpreter_sha256': spec['interpreter_sha256'],
                                     'wheels': spec['wheels'], **extra})).hexdigest()


def _regular(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1: raise ConfigError('runtime inputs must be regular unlinked files')
    return st


def build(root, cache, output, *, version='v7.0.0', catalog_cache=None):
    fixture_only()
    return _build(root, cache, output, version=version, catalog_cache=catalog_cache, execution='offline-fixture')


def build_pilot(root, cache, output, *, version='v7.0.0', catalog_cache):
    # Candidate packaging is credential-free, with a distinct externally pinned digest.
    return _build(root, cache, output, version=version, catalog_cache=catalog_cache, execution='pilot-candidate')


def build_live_pilot(root, cache, output, *, catalog_cache):
    from .live_release import VERSION, INSTALLATION
    return _build(root, cache, output, version=VERSION, catalog_cache=catalog_cache, execution=INSTALLATION)


def _build(root, cache, output, *, version, catalog_cache, execution):
    from .pilot_identity import installation_execution
    installation_execution(execution)
    root, cache, output = Path(root).absolute(), Path(cache).absolute(), Path(output).absolute()
    if not re.fullmatch(r'v7\.\d+\.\d+', version): raise ConfigError('use a complete v7 runtime version')
    if platform.system() != 'Linux' or platform.machine() != 'x86_64' or platform.python_version_tuple()[:2] != ('3', '12'):
        raise ConfigError('runtime build requires the reference Python/platform')
    required = pins((root / 'requirements-hashed.lock').read_text())
    generated = {}
    from .live_release import INSTALLATION, VERSION, POLICY_NAME, policy_bytes
    if execution == INSTALLATION:
        if version != VERSION or catalog_cache is None:
            raise ConfigError('live pilot requires its reviewed version and catalog dependency closure')
        generated['payload/' + POLICY_NAME] = policy_bytes()
    caches = [cache]
    if catalog_cache is not None:
        optional = pins((root / 'catalog-requirements-hashed.lock').read_text())
        from .catalog_trust import PINS
        if {n: p['version'] for n, p in optional.items()} != PINS:
            raise ConfigError('catalog closure differs from reviewed versions')
        for name, pin in optional.items():
            if name in required and required[name] != pin:
                raise ConfigError('catalog/runtime pin conflict')
            required[name] = pin
        generated['payload/requirements-hashed.lock'] = ''.join(
            f"{p['name']}=={p['version']} --hash=sha256:{p['sha256']}\n"
            for p in sorted(required.values(), key=lambda p: p['name'])).encode()
        generated['payload/requirements.txt'] = ''.join(
            f"{p['name']}=={p['version']}\n" for p in sorted(required.values(), key=lambda p: p['name'])).encode()
        caches.append(Path(catalog_cache).absolute())
    wheel_rows = []
    wheel_paths = {}
    for path in sorted(p for c in caches for p in c.glob('*.whl')):
        _regular(path)
        with zipfile.ZipFile(path) as archive:
            metadata, = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA') and n.count('/') == 1]
            from email.parser import BytesParser
            meta = BytesParser().parsebytes(archive.read(metadata))
            name = re.sub(r'[-_.]+', '-', meta['Name']).lower()
            if name not in required: continue
            pin = required[name]
            if any(w['name'] == name for w in wheel_rows):
                # Shared cffi/pycparser pins appear in both curated caches.
                if path.name == wheel_paths[name].name and digest(path) == pin['sha256']: continue
                raise ConfigError('ambiguous cached runtime wheel')
            if meta['Version'] != pin['version'] or digest(path) != pin['sha256']: raise ConfigError('cached runtime wheel differs from lock')
            wheel_paths[name] = path
            wheel_rows.append({**pin, 'file': path.name, 'bytes': path.stat().st_size,
                               'expanded_bytes': sum(i.file_size for i in archive.infolist()),
                               'requires': meta.get_all('Requires-Dist', [])})
    if {w['name'] for w in wheel_rows} != set(required): raise ConfigError('incomplete offline runtime wheel cache')
    wheel_rows.sort(key=lambda w: w['name'])
    files = {}
    inputs = {}
    for name in source_names(root):
        safe_name(name); path = root / name
        _regular(path)
        for parent in (path.parent, *path.parent.parents):
            if parent.is_symlink(): raise ConfigError('runtime source paths cannot contain links')
            if parent == root: break
        inputs['payload/' + name] = path
    for row in wheel_rows: inputs['wheelhouse/' + safe_name(row['file'])] = wheel_paths[row['name']]
    inputs.update(generated)
    for name, path in sorted(inputs.items()):
        files[name] = {'sha256': hashlib.sha256(path).hexdigest() if isinstance(path, bytes) else digest(path),
                       'bytes': len(path) if isinstance(path, bytes) else path.stat().st_size}
    spec = {'format': 1, 'execution': execution, 'live_ready': False, 'version': version, 'target': TARGET,
            'interpreter_sha256': digest('/usr/bin/python3.12'), 'wheels': wheel_rows, 'files': files,
            'external_prerequisites': ['system CPython 3.12 + venv/ensurepip', 'Ubuntu 24.04 system libraries',
                                       '.NET 8 Microsoft.NETCore.App for optional official PP calculator'],
            'system_service_installed': False, 'catalogs_included': False, 'signed': False}
    spec['environment_id'] = environment_id(spec)
    if sum(f['bytes'] for f in files.values()) > MAX_BUNDLE: raise ConfigError('runtime bundle exceeds local quota')
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink(): raise ConfigError('runtime builder never overwrites an artifact')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.bundle-', delete=False) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name in ['bundle.json', *sorted(inputs)]:
                info = zipfile.ZipInfo(name, date_time=(2026, 10, 8, 0, 0, 0))
                info.create_system = 3; info.external_attr = (stat.S_IFREG | 0o644) << 16
                if name == 'bundle.json': archive.writestr(info, json_bytes(spec))
                elif isinstance(inputs[name], bytes): archive.writestr(info, inputs[name])
                else:
                    with archive.open(info, 'w') as destination, inputs[name].open('rb') as source:
                        while chunk := source.read(1024**2): destination.write(chunk)
        os.chmod(temporary, 0o600)
        with temporary.open('rb') as stream: os.fsync(stream.fileno())
        # link provides no-overwrite publication; both names refer only to our temporary file.
        os.link(temporary, output)
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)
    directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {'path': str(output), 'sha256': digest(output), 'bytes': output.stat().st_size,
            'source_files': sum(n.startswith('payload/') for n in files), 'wheels': len(wheel_rows),
            'environment_id': spec['environment_id'], 'live_ready': False}


def inspect(path, expected_sha256, *, execution='offline-fixture'):
    from .pilot_identity import installation_execution
    installation_execution(execution)
    if execution not in ('pilot-candidate', 'pilot-live'): fixture_only()
    path = Path(path)
    if not re.fullmatch(r'[a-f0-9]{64}', expected_sha256 or ''): raise ConfigError('trusted runtime digest required')
    st = _regular(path)
    if st.st_size > MAX_BUNDLE or digest(path) != expected_sha256: raise ConfigError('runtime artifact digest/quota mismatch')
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [safe_name(i.filename) for i in entries]
            if len(names) != len(set(names)) or sum(i.file_size for i in entries) > MAX_BUNDLE:
                raise ConfigError('duplicate or oversized runtime archive')
            for info in entries:
                if info.file_size > 64 * 1024**2 or not stat.S_ISREG(info.external_attr >> 16) or info.flag_bits & 1:
                    raise ConfigError('runtime archive must contain bounded regular files only')
            info = archive.getinfo('bundle.json')
            if info.file_size > 65536: raise ConfigError('runtime manifest too large')
            spec = json.loads(archive.read(info), object_pairs_hook=unique_fields,
                              parse_constant=lambda _: (_ for _ in ()).throw(ConfigError('invalid runtime JSON')))
            keys = {'format','execution','live_ready','version','target','interpreter_sha256','wheels','files',
                    'external_prerequisites','system_service_installed','catalogs_included','signed','environment_id'}
            if (not isinstance(spec, dict) or set(spec) != keys or type(spec['format']) is not int or spec['format'] != 1
                    or spec['execution'] != execution or spec['live_ready'] is not False
                    or any(spec[k] is not False for k in ('signed','system_service_installed','catalogs_included'))
                    or spec['target'] != TARGET or not re.fullmatch(r'v7\.\d+\.\d+', spec['version'])
                    or not isinstance(spec['files'], dict) or not isinstance(spec['wheels'], list)
                    or spec['environment_id'] != environment_id(spec)):
                raise ConfigError('invalid runtime manifest contract')
            if set(names) != {'bundle.json', *spec['files']}:
                raise ConfigError('runtime member inventory differs from manifest')
            from .live_release import INSTALLATION, VERSION, POLICY_NAME, policy_bytes
            policy_member = 'payload/' + POLICY_NAME
            if execution == INSTALLATION:
                if spec['version'] != VERSION or policy_member not in names or archive.read(policy_member) != policy_bytes():
                    raise ConfigError('live pilot release policy differs from reviewed artifact')
            elif policy_member in names:
                raise ConfigError('non-live artifacts cannot carry a live policy')
            source_set = {n.removeprefix('payload/') for n in names if n.startswith('payload/')}
            if not set(ROOT_SOURCES) | {'requirements.txt', 'requirements-hashed.lock'} <= source_set:
                raise ConfigError('missing runtime launcher/lock closure')
            for name in spec['files']:
                if name.startswith('payload/'):
                    relative = name.removeprefix('payload/')
                    if relative not in (*ROOT_SOURCES, 'requirements.txt', 'requirements-hashed.lock', POLICY_NAME) and not (
                            relative.startswith(('bot/', 'rolling/', 'poolrotater_host/')) and relative.endswith('.py')):
                        raise ConfigError('runtime payload member outside allowlist')
                elif not name.startswith('wheelhouse/') or not name.endswith('.whl'):
                    raise ConfigError('runtime bundle contains unexpected content')
                row = spec['files'][name]
                if set(row) != {'sha256','bytes'} or type(row['bytes']) is not int or row['bytes'] != archive.getinfo(name).file_size:
                    raise ConfigError('invalid runtime member size')
                with archive.open(name) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']:
                        raise ConfigError('runtime member content mismatch')
            locked = pins(archive.read('payload/requirements-hashed.lock').decode())
            ordinary = archive.read('payload/requirements.txt').decode().splitlines()
            if set(ordinary) != {w['name'] + '==' + w['version'] for w in spec['wheels']}:
                # The historical plain lock uses underscores for clr_loader and typing_extensions.
                normalized = {re.sub(r'[-_.]+','-', line.split('==')[0]).lower() + '==' + line.split('==')[1]
                              for line in ordinary if line.count('==') == 1}
                if len(normalized) != len(ordinary) or normalized != {w['name'] + '==' + w['version'] for w in spec['wheels']}:
                    raise ConfigError('plain and hashed runtime requirements differ')
            if len(spec['wheels']) != len(locked): raise ConfigError('incomplete runtime dependency manifest')
            seen = set()
            for row in spec['wheels']:
                if set(row) != {'name','version','sha256','file','bytes','expanded_bytes','requires'} or row['name'] in seen:
                    raise ConfigError('invalid runtime wheel manifest')
                seen.add(row['name'])
                if (type(row['bytes']) is not int or row['bytes'] <= 0 or type(row['expanded_bytes']) is not int
                        or not 0 < row['expanded_bytes'] <= 256 * 1024**2 or not isinstance(row['requires'],list)
                        or not all(isinstance(r,str) for r in row['requires'])):
                    raise ConfigError('invalid runtime wheel size/metadata')
                if {k: row[k] for k in ('name','version','sha256')} != locked.get(row['name']):
                    raise ConfigError('runtime wheel pin differs from lock')
                member = 'wheelhouse/' + safe_name(row['file'])
                if spec['files'].get(member) != {'sha256':row['sha256'],'bytes':row['bytes']}:
                    raise ConfigError('runtime wheel member mismatch')
            if seen != set(locked) or {n for n in names if n.startswith('wheelhouse/')} != {'wheelhouse/' + w['file'] for w in spec['wheels']}:
                raise ConfigError('runtime wheel closure mismatch')
            return spec
    except ConfigError: raise
    except Exception: raise ConfigError('invalid runtime archive') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', default='v7.0.0')
    parser.add_argument('--catalog-cache', type=Path, help='opt in to the curated protected catalog dependency closure')
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.cache, args.output, version=args.version, catalog_cache=args.catalog_cache), indent=2))


if __name__ == '__main__': main()
