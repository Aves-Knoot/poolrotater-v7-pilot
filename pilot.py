#!/usr/bin/env python3
"""Verified, non-root offline installation rehearsal. No live provider or service."""
from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

FORMAT = 1
RESERVE = 5 * 1024**3
MAX_RELEASE = 256 * 1024**2
MAX_CATALOG = 512 * 1024**2
HASH = re.compile(r'[a-f0-9]{64}\Z')
MODES = ('standard', 'mania4', 'mania7')


class PilotError(ValueError):
    pass


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PilotError('duplicate_json_field')
        result[key] = value
    return result


def decode(data):
    return json.loads(data, object_pairs_hook=unique,
                     parse_constant=lambda _: (_ for _ in ()).throw(PilotError('invalid_json_number')))


def no_links(path):
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise PilotError('symlink_path_refused')
    return path


def regular(path, maximum=MAX_RELEASE, *, allow_empty=False):
    path = no_links(path)
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
            or not (0 if allow_empty else 1) <= st.st_size <= maximum):
        raise PilotError('bounded_regular_file_required')
    return path


def digest(path):
    with regular(path, MAX_CATALOG, allow_empty=True).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def name(value):
    if (type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_./+-]+', value)
            or str(PurePosixPath(value)) != value or value.startswith('/')
            or any(p in ('', '.', '..') for p in value.split('/'))):
        raise PilotError('unsafe_member_path')
    return value


def verify(folder, expected):
    folder = no_links(folder)
    if type(expected) is not str or not HASH.fullmatch(expected):
        raise PilotError('independent_manifest_sha256_required')
    path = regular(folder / 'release.json', 512 * 1024)
    if digest(path) != expected:
        raise PilotError('release_manifest_hash_mismatch')
    manifest = decode(path.read_bytes())
    keys = {'format', 'kind', 'version', 'live_ready', 'public_release_approved',
            'target', 'catalog_trust', 'files', 'wheels', 'catalog_modes'}
    if (type(manifest) is not dict or set(manifest) != keys
            or type(manifest['format']) is not int or manifest['format'] != FORMAT
            or manifest['kind'] != 'offline-pilot-rehearsal'
            or manifest['live_ready'] is not False or manifest['public_release_approved'] is not False
            or manifest['target'] != {'os': 'Ubuntu 24.04', 'architecture': 'x86_64', 'python': '3.12'}
            or manifest['catalog_trust'] != 'independently-supplied-release-digest; no deployed signing authority'
            or manifest['catalog_modes'] != list(MODES)
            or type(manifest['version']) is not str or not re.fullmatch(r'7\.0\.0\.dev[0-9]+', manifest['version'])
            or type(manifest['files']) is not dict or type(manifest['wheels']) is not list):
        raise PilotError('unsupported_release_manifest')
    expected_files = {'release.json', *manifest['files']}
    actual_files = set()
    allowed_dirs = set()
    total = 0
    for relative, row in manifest['files'].items():
        name(relative)
        allowed_dirs.update(str(p) for p in PurePosixPath(relative).parents if str(p) != '.')
        if (type(row) is not dict or set(row) != {'sha256', 'bytes'}
                or type(row['sha256']) is not str or not HASH.fullmatch(row['sha256'])
                or type(row['bytes']) is not int or not 0 < row['bytes'] <= MAX_RELEASE):
            raise PilotError('invalid_release_file_identity')
        file = regular(folder / relative)
        if file.stat().st_size != row['bytes'] or digest(file) != row['sha256']:
            raise PilotError('release_content_mismatch')
        total += row['bytes']
    if total > MAX_RELEASE:
        raise PilotError('release_budget_exceeded')
    for file in folder.rglob('*'):
        if file.is_symlink():
            raise PilotError('symlink_release_member')
        relative = file.relative_to(folder).as_posix()
        if file.is_dir():
            if relative not in allowed_dirs:
                raise PilotError('unexpected_release_directory')
        else:
            regular(file)
            actual_files.add(relative)
    if actual_files != expected_files:
        raise PilotError('release_inventory_mismatch')
    mandatory = {'pilot.py', 'README.md', 'requirements-hashed.lock', 'catalog-kit.zip',
                 'catalog-runtime.zip', 'THIRD_PARTY_NOTICES.md', 'SOURCE_POLICY.md'}
    if not mandatory <= set(manifest['files']):
        raise PilotError('release_missing_required_files')
    seen = set()
    for wheel in manifest['wheels']:
        if (type(wheel) is not dict or set(wheel) != {'name', 'version', 'file', 'sha256'}
                or type(wheel['name']) is not str or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', wheel['name'])
                or type(wheel['version']) is not str or not re.fullmatch(r'[A-Za-z0-9.+-]+', wheel['version'])
                or wheel['name'] in seen or not name(wheel['file']).endswith('.whl')
                or '/' in wheel['file']):
            raise PilotError('invalid_wheel_identity')
        seen.add(wheel['name'])
        row = manifest['files'].get('wheelhouse/' + wheel['file'])
        if row is None or row['sha256'] != wheel['sha256']:
            raise PilotError('wheel_identity_mismatch')
    if (seen != {'poolrotater-host', 'cffi', 'cryptography', 'pycparser', 'securesystemslib', 'tuf', 'urllib3'}
            or {p for p in manifest['files'] if p.startswith('wheelhouse/')}
            != {'wheelhouse/' + w['file'] for w in manifest['wheels']}):
        raise PilotError('offline_dependency_closure_mismatch')
    host = next(w for w in manifest['wheels'] if w['name'] == 'poolrotater-host')
    if host['version'] != manifest['version']:
        raise PilotError('host_version_mismatch')
    lock = ''.join(f"{w['name']}=={w['version']} --hash=sha256:{w['sha256']}\n"
                   for w in sorted(manifest['wheels'], key=lambda w: w['name']))
    if (folder / 'requirements-hashed.lock').read_text() != lock:
        raise PilotError('offline_lock_mismatch')
    return manifest


def preflight(parent):
    if os.geteuid() == 0:
        raise PilotError('run_rehearsal_as_non_root')
    if platform.system() != 'Linux' or platform.machine() != 'x86_64' or sys.version_info[:2] != (3, 12):
        raise PilotError('requires_linux_x86_64_python_3_12')
    distro = platform.freedesktop_os_release()
    if distro.get('ID') != 'ubuntu' or distro.get('VERSION_ID') != '24.04':
        raise PilotError('pilot_reference_os_is_ubuntu_24_04')
    parent = no_links(parent)
    if not parent.is_dir() or parent.stat().st_uid != os.geteuid() or not os.access(parent, os.W_OK):
        raise PilotError('explicit_owned_writable_parent_required')
    if shutil.disk_usage(parent).free < RESERVE:
        raise PilotError('five_gib_disk_reserve_required')
    return {'ok': True, 'platform': 'Ubuntu 24.04 x86_64 Python 3.12',
            'live_ready': False, 'service_installed': False, 'oauth_required': False}


def write(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.pilot-', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(canonical(value)); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)


def receipt(destination, expected, *, completed=True):
    destination = no_links(destination)
    st = destination.stat()
    if not destination.is_dir() or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise PilotError('private_owned_rehearsal_required')
    path = regular(destination / 'installation.json', 64 * 1024)
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PilotError('unsafe_installation_receipt')
    value = decode(path.read_bytes())
    if (type(value) is not dict or set(value) != {'format', 'kind', 'manifest_sha256', 'uid', 'phase', 'live_ready'}
            or type(value['format']) is not int or value['format'] != FORMAT
            or value['kind'] != 'offline-pilot-rehearsal' or value['manifest_sha256'] != expected
            or type(value['uid']) is not int or value['uid'] != os.geteuid()
            or value['live_ready'] is not False or value['phase'] not in ('installing', 'complete')
            or (completed and value['phase'] != 'complete')):
        raise PilotError('foreign_changed_or_incomplete_installation')
    return value


def run(destination, *args, timeout=240):
    temporary = no_links(destination / 'tmp')
    st = temporary.stat()
    if not temporary.is_dir() or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise PilotError('private_rehearsal_temporary_directory_required')
    environment = {'PATH': '/usr/bin:/bin', 'HOME': str(destination), 'LANG': 'C.UTF-8',
                   'PYTHONDONTWRITEBYTECODE': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1',
                   'PIP_NO_INDEX': '1', 'PIP_CONFIG_FILE': '/dev/null', 'MALLOC_ARENA_MAX': '2',
                   'TMPDIR': str(temporary)}
    result = subprocess.run(list(map(str, args)), cwd=destination, env=environment,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # No stderr/stdout echo: downstream messages may include local paths.
        raise PilotError('rehearsal_child_failed')
    return result.stdout


def installed_check(folder, destination, expected, manifest=None, *, completed=True):
    manifest = verify(folder, expected) if manifest is None else manifest
    receipt(destination, expected, completed=completed)
    python = destination / 'env/bin/python'
    # The environment is private operator-owned rehearsal code, not root-sealed
    # service authority. Reject symlinked managed directories before execution.
    for path in (destination / 'env', destination / 'env/bin', destination / 'host'):
        no_links(path)
        if not path.is_dir() or path.stat().st_uid != os.geteuid():
            raise PilotError('unsafe_rehearsal_directory')
    packages = decode(run(destination, python, '-I', '-B', '-m', 'pip', 'list', '--format=json'))
    versions = {p['name'].lower().replace('_', '-'): p['version'] for p in packages}
    wanted = {w['name']: w['version'] for w in manifest['wheels']}
    if any(versions.get(n) != v for n, v in wanted.items()) or set(versions) - set(wanted) - {'pip'}:
        raise PilotError('installed_dependency_versions_changed')
    run(destination, python, '-I', '-B', '-m', 'pip', 'check')
    check = decode(run(destination, python, '-I', '-B', '-m', 'poolrotater_host', 'check', '--root', destination / 'host'))
    evidence = regular(destination / 'rehearsal.json', 65536)
    if evidence.stat().st_uid != os.geteuid() or stat.S_IMODE(evidence.stat().st_mode) != 0o600:
        raise PilotError('unsafe_rehearsal_evidence')
    simulation = decode(evidence.read_bytes())
    # Fixed dispatch must refuse before network or authorization effects.
    run(destination, python, '-I', '-B', '-c',
        "from poolrotater_host.provider_contract import require_live_provider\n"
        "from poolrotater_host.oauth import AuthError\n"
        "try: require_live_provider()\n"
        "except AuthError as e: assert e.code == 'live_provider_disabled'\n"
        "else: raise AssertionError('live gate opened')\n")
    if (not check['ok'] or check['live_ready'] is not False or len(check['rooms']) != 4
            or simulation.get('live_ready') is not False or simulation.get('execution') != 'offline'
            or len(simulation['rooms']) != 4 or any(len(r['played']) != 3 for r in simulation['rooms'])):
        raise PilotError('four_preset_rehearsal_failed')
    return {'ok': True, 'kind': 'offline-pilot-rehearsal', 'version': manifest['version'],
            'manifest_sha256': expected, 'four_presets': True, 'rounds_per_preset': 3,
            'dependencies_verified': True, 'live_gate_closed': True, 'live_ready': False,
            'service_installed': False, 'account_authorized': False,
            'catalogs': 'invented rehearsal maps; use catalog-check for the separate real exports'}


def install(folder, destination, expected):
    folder, destination = no_links(folder), no_links(destination)
    manifest = verify(folder, expected)
    preflight(destination.parent)
    if destination == folder or destination.is_relative_to(folder) or folder.is_relative_to(destination):
        raise PilotError('release_and_installation_must_be_separate')
    if destination.exists():
        receipt(destination, expected)
        return {**installed_check(folder, destination, expected, manifest), 'rerun': 'verified_no_changes'}
    destination.mkdir(mode=0o700)
    record = {'format': FORMAT, 'kind': 'offline-pilot-rehearsal', 'manifest_sha256': expected,
              'uid': os.geteuid(), 'phase': 'installing', 'live_ready': False}
    write(destination / 'installation.json', record)
    (destination / 'tmp').mkdir(mode=0o700)
    # Final path from the start: moving a venv would leave stale entrypoint paths.
    # An interrupted install is retained and refuses adoption. Use a new empty
    # destination after inspecting/removing only your own incomplete rehearsal.
    run(destination, sys.executable, '-I', '-B', '-m', 'venv', destination / 'env')
    python = destination / 'env/bin/python'
    run(destination, python, '-I', '-B', '-m', 'pip', 'install', '--no-index',
        '--no-cache-dir', '--only-binary=:all:', '--require-hashes', '--find-links', folder / 'wheelhouse',
        '-r', folder / 'requirements-hashed.lock')
    run(destination, python, '-I', '-B', '-m', 'poolrotater_host', 'setup', '--root', destination / 'host')
    run(destination, python, '-I', '-B', '-m', 'poolrotater_host.testing.fixtures', '--root', destination / 'host')
    simulation = decode(run(destination, python, '-I', '-B', '-m', 'poolrotater_host', 'simulate',
                            '--root', destination / 'host', '--rounds', '3'))
    write(destination / 'rehearsal.json', simulation)
    result = installed_check(folder, destination, expected, manifest, completed=False)
    record['phase'] = 'complete'
    write(destination / 'installation.json', record)
    return result


def unpack_catalogs(archive_path, workspace):
    """Exact bounded zip and tar extraction. Never uses extractall or source SQL."""
    with zipfile.ZipFile(regular(archive_path)) as archive:
        entries = archive.infolist()
        expected = {m + '/' + f for m in MODES for f in ('manifest.json', 'bundle.tar.gz')}
        if (len(entries) != len(expected) or {name(i.filename) for i in entries} != expected
                or sum(i.file_size for i in entries) > MAX_RELEASE):
            raise PilotError('catalog_kit_inventory_mismatch')
        for info in entries:
            if (not stat.S_ISREG(info.external_attr >> 16) or info.flag_bits & 1
                    or not 0 < info.file_size <= MAX_RELEASE):
                raise PilotError('invalid_catalog_kit_member')
            if info.filename.endswith('manifest.json') and info.file_size > 65536:
                raise PilotError('oversized_catalog_manifest')
            target = workspace / info.filename
            target.parent.mkdir(mode=0o700, exist_ok=True)
            with archive.open(info) as src, target.open('xb') as dst:
                shutil.copyfileobj(src, dst, length=1024**2)
    for mode in MODES:
        folder = workspace / mode
        manifest = decode((folder / 'manifest.json').read_bytes())
        # Full application manifest validation follows in the installed child.
        # Check extraction identities/quota before trusting archive names/lengths.
        if type(manifest) is not dict or manifest.get('mode') != mode or type(manifest.get('files')) is not list:
            raise PilotError('invalid_catalog_manifest')
        rows = {}
        for row in manifest['files']:
            if (type(row) is not dict or row.get('name') not in ('catalog.db', 'dt.db', 'farm.db')
                    or row['name'] in rows or type(row.get('bytes')) is not int
                    or not 0 < row['bytes'] <= MAX_CATALOG
                    or type(row.get('sha256')) is not str or not HASH.fullmatch(row['sha256'])):
                raise PilotError('invalid_catalog_file_identity')
            rows[row['name']] = row
        if not 1 <= len(rows) <= 2 or sum(r['bytes'] for r in rows.values()) > MAX_CATALOG:
            raise PilotError('catalog_expansion_budget_exceeded')
        seen = set()
        # Bound decompression before tar parses extension headers or sparse
        # metadata. The M2 exporter emits small fixed USTAR headers only.
        with tempfile.TemporaryFile(dir=workspace) as expanded:
            total = 0
            with gzip.open(folder / 'bundle.tar.gz', 'rb') as zipped:
                while chunk := zipped.read(1024**2):
                    total += len(chunk)
                    if total > sum(r['bytes'] for r in rows.values()) + 65536:
                        raise PilotError('catalog_tar_expansion_budget_exceeded')
                    expanded.write(chunk)
            expanded.seek(0)
            with tarfile.open(fileobj=expanded, mode='r|') as archive:
                for info in archive:
                    if (info.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or info.sparse is not None
                            or info.pax_headers or info.name not in rows or info.name in seen
                            or info.size != rows[info.name]['bytes']):
                        raise PilotError('invalid_catalog_archive_member')
                    seen.add(info.name)
                    with archive.extractfile(info) as src, (folder / info.name).open('xb') as dst:
                        shutil.copyfileobj(src, dst, length=1024**2)
                    if digest(folder / info.name) != rows[info.name]['sha256']:
                        raise PilotError('catalog_file_hash_mismatch')
        if seen != set(rows):
            raise PilotError('catalog_archive_missing_files')


CATALOG_CHECK = '''
import json,sys
from pathlib import Path
from poolrotater_host.catalog_export import inspect_bundle,pool_from_bundle
results=[]
for mode in ('standard','mania4','mania7'):
 folder=Path(sys.argv[1])/mode
 manifest=inspect_bundle(folder)
 pool=pool_from_bundle(folder,sr_min=0,sr_max=20,limit=200)
 assert pool.maps and pool.next_map() is not None
 assert all(m.beatmap_id!=1549496 for m in pool.maps)
 results.append({'mode':mode,'content_id':manifest['content_id'],
  'eligible_maps':len(pool.maps),'companions':manifest['capabilities']['companions'],
  'styles':manifest['capabilities']['styles'],'mods':manifest['capabilities']['mods']})
print(json.dumps({'ok':True,'modes':results,'live_ready':False,
 'catalogs_activated':False,'managed_trust_created':False,'excluded_map':1549496}))
'''


def catalog_check(folder, destination, expected):
    folder, destination = no_links(folder), no_links(destination)
    verify(folder, expected); receipt(destination, expected)
    for path in (destination / 'env', destination / 'env/bin'):
        no_links(path)
    with tempfile.TemporaryDirectory(prefix='catalog-rehearsal-', dir=destination) as temporary:
        workspace = Path(temporary)
        unpack_catalogs(folder / 'catalog-kit.zip', workspace)
        return decode(run(destination, destination / 'env/bin/python', '-I', '-B', '-c',
                          CATALOG_CHECK, workspace, timeout=300))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('verify', 'preflight', 'install', 'check', 'catalog-check', 'support'))
    parser.add_argument('--release', type=Path, default=Path(__file__).absolute().parent)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args(argv)
    try:
        priority = os.getpriority(os.PRIO_PROCESS, 0)
        if priority < 15:
            os.nice(15 - priority)
        if args.command == 'verify':
            manifest = verify(args.release, args.manifest_sha256)
            result = {'ok': True, 'version': manifest['version'], 'files_verified': len(manifest['files']),
                      'live_ready': False, 'public_release_approved': False}
        else:
            if args.destination is None:
                raise PilotError('explicit_destination_required')
            folder, destination = no_links(args.release), no_links(args.destination)
            if args.command == 'preflight':
                verify(folder, args.manifest_sha256)
                result = preflight(destination.parent)
            elif args.command == 'install':
                result = install(folder, destination, args.manifest_sha256)
            else:
                # Serialize checks without accepting symlink/hardlink lock files.
                receipt(destination, args.manifest_sha256)
                lock = destination / '.check.lock'
                fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
                try:
                    st = os.fstat(fd)
                    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid()
                            or stat.S_IMODE(st.st_mode) != 0o600):
                        raise PilotError('unsafe_rehearsal_lock')
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if args.command == 'catalog-check':
                        result = catalog_check(folder, destination, args.manifest_sha256)
                    else:
                        result = installed_check(folder, destination, args.manifest_sha256)
                finally:
                    os.close(fd)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, tarfile.TarError,
            subprocess.SubprocessError) as error:
        code = str(error) if type(error) is PilotError else type(error).__name__
        print(json.dumps({'ok': False, 'error': code, 'live_ready': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
