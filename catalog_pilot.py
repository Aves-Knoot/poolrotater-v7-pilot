#!/usr/bin/env python3
"""Non-root installation and real HTTPS catalog download pilot; no osu! access."""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from urllib.parse import urlsplit

if not __package__:
    # -I deliberately omits the script directory. Load only the helper bundled
    # beside this independently archive-verified entry, never ambient imports.
    sys.path.insert(0, str(Path(__file__).absolute().parent))

try:
    from .pilot import (HASH, MAX_RELEASE, MODES, PilotError, decode, digest, name,
                        no_links, preflight, regular, run, write)
except ImportError:
    from pilot import (HASH, MAX_RELEASE, MODES, PilotError, decode, digest, name,
                       no_links, preflight, regular, run, write)

KIND = 'https-catalog-download-pilot'
PACKAGES = {'poolrotater-host', 'cffi', 'cryptography', 'pycparser',
            'securesystemslib', 'tuf', 'urllib3'}


def verify(folder, expected):
    folder = no_links(folder)
    if type(expected) is not str or not HASH.fullmatch(expected):
        raise PilotError('independent_manifest_sha256_required')
    path = regular(folder / 'release.json', 512 * 1024)
    if digest(path) != expected:
        raise PilotError('release_manifest_hash_mismatch')
    m = decode(path.read_bytes())
    keys = {'format', 'kind', 'version', 'live_ready', 'target', 'connection', 'files', 'wheels'}
    if (type(m) is not dict or set(m) != keys or type(m['format']) is not int
            or m['format'] != 1 or m['kind'] != KIND or m['live_ready'] is not False
            or m['target'] != {'os': 'Ubuntu 24.04', 'architecture': 'x86_64', 'python': '3.12'}
            or m['version'] != '7.0.0.dev27' or type(m['files']) is not dict
            or type(m['wheels']) is not list):
        raise PilotError('unsupported_download_pilot_manifest')
    c = m['connection']
    if (type(c) is not dict or set(c) != {'origin', 'bootstrap_sha256', 'ca_sha256',
                                         'content_ids', 'metadata_expires'}
            or any(type(c[k]) is not str or not HASH.fullmatch(c[k])
                   for k in ('bootstrap_sha256', 'ca_sha256'))
            or type(c['content_ids']) is not dict or set(c['content_ids']) != set(MODES)
            or any(type(v) is not str or not HASH.fullmatch(v) for v in c['content_ids'].values())
            or type(c['metadata_expires']) is not str):
        raise PilotError('invalid_download_connection')
    try:
        p = urlsplit(c['origin']); address = ipaddress.ip_address(p.hostname)
        host = '[' + str(address) + ']' if address.version == 6 else str(address)
        valid = (p.scheme == 'https' and p.port is not None and 1 <= p.port <= 65535
                 and c['origin'] == f'https://{host}:{p.port}/'
                 and not p.username and not p.password and not p.query and not p.fragment
                 and not address.is_unspecified and not address.is_multicast and not address.is_link_local)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise PilotError('explicit_https_ip_origin_required')
    actual = set(); allowed_dirs = set(); total = 0
    for relative, row in m['files'].items():
        name(relative)
        allowed_dirs.update(str(p) for p in Path(relative).parents if str(p) != '.')
        if (type(row) is not dict or set(row) != {'sha256', 'bytes'}
                or type(row['bytes']) is not int or not 0 < row['bytes'] <= MAX_RELEASE
                or type(row['sha256']) is not str or not HASH.fullmatch(row['sha256'])):
            raise PilotError('invalid_release_file_identity')
        file = regular(folder / relative)
        if file.stat().st_size != row['bytes'] or digest(file) != row['sha256']:
            raise PilotError('release_content_mismatch')
        total += row['bytes']
    if total > MAX_RELEASE:
        raise PilotError('release_budget_exceeded')
    for file in folder.rglob('*'):
        no_links(file)
        relative = file.relative_to(folder).as_posix()
        if file.is_dir():
            if relative not in allowed_dirs: raise PilotError('unexpected_release_directory')
        else:
            regular(file); actual.add(relative)
    if actual != {'release.json', *m['files']}:
        raise PilotError('release_inventory_mismatch')
    mandatory = {'catalog_pilot.py', 'pilot.py', 'README.md', 'SOURCE_POLICY.md',
                 'THIRD_PARTY_NOTICES.md', 'requirements-hashed.lock', 'bootstrap.json', 'ca.crt'}
    if not mandatory <= set(m['files']) or any(p.endswith(('.db', '.tar.gz')) for p in m['files']):
        raise PilotError('download_pilot_must_not_bundle_databases')
    seen = set()
    for w in m['wheels']:
        if (type(w) is not dict or set(w) != {'name', 'version', 'file', 'sha256'}
                or w['name'] not in PACKAGES or w['name'] in seen
                or type(w['version']) is not str or not re.fullmatch(r'[A-Za-z0-9.+-]+', w['version'])
                or '/' in name(w['file']) or not w['file'].endswith('.whl')
                or m['files'].get('wheelhouse/' + w['file'], {}).get('sha256') != w['sha256']):
            raise PilotError('invalid_wheel_identity')
        seen.add(w['name'])
    if (seen != PACKAGES or {p for p in m['files'] if p.startswith('wheelhouse/')}
            != {'wheelhouse/' + w['file'] for w in m['wheels']}
            or next(w for w in m['wheels'] if w['name'] == 'poolrotater-host')['version'] != m['version']):
        raise PilotError('download_dependency_closure_mismatch')
    lock = ''.join(f"{w['name']}=={w['version']} --hash=sha256:{w['sha256']}\n"
                   for w in sorted(m['wheels'], key=lambda w: w['name']))
    if (folder / 'requirements-hashed.lock').read_text() != lock:
        raise PilotError('offline_lock_mismatch')
    if digest(folder / 'bootstrap.json') != c['bootstrap_sha256'] or digest(folder / 'ca.crt') != c['ca_sha256']:
        raise PilotError('download_trust_mismatch')
    return m


def private(folder):
    folder = no_links(folder); st = folder.stat()
    if not folder.is_dir() or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise PilotError('private_owned_download_directory_required')
    return folder


def receipt(destination, expected):
    private(destination)
    path = regular(destination / 'installation.json', 65536)
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PilotError('unsafe_download_installation_receipt')
    r = decode(path.read_bytes())
    if (type(r) is not dict or set(r) != {'format', 'kind', 'manifest_sha256', 'uid', 'phase', 'live_ready'}
            or type(r['format']) is not int or r['format'] != 1 or r['kind'] != KIND
            or r['manifest_sha256'] != expected or type(r['uid']) is not int or r['uid'] != os.geteuid()
            or r['phase'] not in ('installing', 'downloading', 'complete') or r['live_ready'] is not False):
        raise PilotError('foreign_or_changed_download_installation')
    return r


def dependencies(destination, manifest):
    for path in (destination / 'env', destination / 'env/bin', destination / 'tmp'):
        private(path)
    python = destination / 'env/bin/python'
    values = decode(run(destination, python, '-I', '-B', '-m', 'pip', 'list', '--format=json'))
    versions = {v['name'].lower().replace('_', '-'): v['version'] for v in values}
    wanted = {w['name']: w['version'] for w in manifest['wheels']}
    if any(versions.get(n) != v for n, v in wanted.items()) or set(versions) - set(wanted) - {'pip'}:
        raise PilotError('installed_dependency_versions_changed')
    run(destination, python, '-I', '-B', '-m', 'pip', 'check')
    run(destination, python, '-I', '-B', '-c',
        "from poolrotater_host.provider_contract import require_live_provider\n"
        "from poolrotater_host.oauth import AuthError\n"
        "try: require_live_provider()\n"
        "except AuthError as e: assert e.code == 'live_provider_disabled'\n"
        "else: raise AssertionError('live gate opened')\n")


CHECK = '''
import json,sys
from pathlib import Path
from poolrotater_host.catalog_export import inspect_bundle,pool_from_bundle
base=Path(sys.argv[1]);connection=json.loads(sys.argv[2]);modes=[]
for mode in ('standard','mania4','mania7'):
 kit=base/mode;manifest=inspect_bundle(kit/'catalog');receipt=json.loads((kit/'download.json').read_bytes())
 assert manifest['content_id']==connection['content_ids'][mode]
 assert receipt=={'format':1,'mode':mode,'content_id':manifest['content_id'],
  'bootstrap_sha256':connection['bootstrap_sha256'],'ca_sha256':connection['ca_sha256'],
  'files':manifest['files'],'archive':manifest['archive'],'activated':False,'live_ready':False}
 pool=pool_from_bundle(kit/'catalog',sr_min=0,sr_max=20,limit=200)
 assert pool.maps and all(m.beatmap_id!=1549496 for m in pool.maps)
 assert all(pool.next_map() is not None for _ in range(3))
 modes.append({'mode':mode,'content_id':manifest['content_id'],'eligible_maps':len(pool.maps),
  'companions':manifest['capabilities']['companions'],'mods':manifest['capabilities']['mods'],
  'styles':manifest['capabilities']['styles']})
print(json.dumps({'ok':True,'modes':modes,'excluded_map':1549496,'catalogs_activated':False}))
'''


def check(folder, destination, expected, manifest=None):
    manifest = verify(folder, expected) if manifest is None else manifest
    r = receipt(destination, expected)
    if r['phase'] != 'complete': raise PilotError('download_installation_incomplete')
    dependencies(destination, manifest)
    private(destination / 'catalogs')
    result = decode(run(destination, destination / 'env/bin/python', '-I', '-B', '-c',
                        CHECK, destination / 'catalogs', __import__('json').dumps(manifest['connection'])))
    evidence = regular(destination / 'downloads.json', 65536)
    if evidence.stat().st_uid != os.geteuid() or stat.S_IMODE(evidence.stat().st_mode) != 0o600:
        raise PilotError('unsafe_download_evidence')
    transfers = decode(evidence.read_bytes())
    if type(transfers) is not dict or set(transfers) != set(MODES): raise PilotError('invalid_download_evidence')
    for mode in MODES:
        item = transfers[mode]
        if (item.get('ok') is not True or item.get('content_id') != manifest['connection']['content_ids'][mode]
                or item.get('mode') != mode or item.get('bootstrap_sha256') != manifest['connection']['bootstrap_sha256']
                or item.get('ca_sha256') != manifest['connection']['ca_sha256']
                or any(item.get(k) is not False for k in ('activated', 'live_ready', 'local_collection_or_analysis',
                                                        'service_installed', 'start_allowed'))):
            raise PilotError('invalid_download_evidence')
    return {**result, 'kind': KIND, 'version': manifest['version'], 'manifest_sha256': expected,
            'origin': manifest['connection']['origin'], 'metadata_expires': manifest['connection']['metadata_expires'],
            'dependencies_verified': True, 'downloaded_from_owner_vps': True,
            'transfer': {m: transfers[m]['transfer'] for m in MODES},
            'local_collection_or_analysis': False, 'service_installed': False,
            'account_authorized': False, 'live_gate_closed': True, 'live_ready': False}


def download(folder, destination, manifest, mode, target):
    c = manifest['connection']
    return decode(run(destination, destination / 'env/bin/python', '-I', '-B', '-m', 'poolrotater_host',
        'catalog-download', '--origin', c['origin'], '--cache', destination / 'cache',
        '--trust-root', folder / 'bootstrap.json', '--trust-sha256', c['bootstrap_sha256'],
        '--ca-file', folder / 'ca.crt', '--ca-sha256', c['ca_sha256'],
        '--destination', target, '--mode', mode, timeout=180))


def install(folder, destination, expected):
    folder, destination = no_links(folder), no_links(destination)
    manifest = verify(folder, expected); preflight(destination.parent)
    if destination == folder or destination.is_relative_to(folder) or folder.is_relative_to(destination):
        raise PilotError('release_and_installation_must_be_separate')
    if os.path.lexists(destination):
        record = receipt(destination, expected)
        if record['phase'] == 'complete':
            return {**check(folder, destination, expected, manifest), 'rerun': 'verified_no_changes'}
        if record['phase'] != 'downloading':
            raise PilotError('interrupted_environment_install_use_fresh_destination')
        dependencies(destination, manifest)
    else:
        destination.mkdir(mode=0o700)
        record = {'format': 1, 'kind': KIND, 'manifest_sha256': expected, 'uid': os.geteuid(),
                  'phase': 'installing', 'live_ready': False}
        write(destination / 'installation.json', record)
        for child in ('tmp', 'catalogs', 'cache'): (destination / child).mkdir(mode=0o700)
        run(destination, sys.executable, '-I', '-B', '-m', 'venv', destination / 'env', timeout=120)
        (destination / 'env').chmod(0o700); (destination / 'env/bin').chmod(0o700)
        run(destination, destination / 'env/bin/python', '-I', '-B', '-m', 'pip', 'install',
            '--no-index', '--require-hashes', '--no-compile', '--find-links', folder / 'wheelhouse',
            '-r', folder / 'requirements-hashed.lock', timeout=180)
        dependencies(destination, manifest)
        record['phase'] = 'downloading'; write(destination / 'installation.json', record)
        write(destination / 'downloads.json', {})
    lock = destination / '.install.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise PilotError('unsafe_download_lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        record = receipt(destination, expected)
        if record['phase'] == 'complete':
            return {**check(folder, destination, expected, manifest), 'rerun': 'verified_no_changes'}
        private(destination / 'catalogs'); private(destination / 'cache')
        transfers = decode(regular(destination / 'downloads.json', 65536).read_bytes())
        if type(transfers) is not dict or not set(transfers) <= set(MODES): raise PilotError('invalid_download_evidence')
        # Recover the narrow gap between atomic catalog publication and saving
        # its transfer evidence by re-verifying through the bound cache.
        for mode in MODES:
            target = destination / 'catalogs' / mode
            if mode in transfers:
                if not target.is_dir(): raise PilotError('completed_catalog_missing')
                continue
            if os.path.lexists(target):
                private(target)
                with tempfile.TemporaryDirectory(prefix='.recover-', dir=destination / 'catalogs') as temp:
                    scratch = Path(temp) / mode
                    result = download(folder, destination, manifest, mode, scratch)
                    if {p.relative_to(target): digest(p) for p in target.rglob('*') if p.is_file()} != {
                            p.relative_to(scratch): digest(p) for p in scratch.rglob('*') if p.is_file()}:
                        raise PilotError('published_catalog_recovery_mismatch')
            else:
                result = download(folder, destination, manifest, mode, target)
            transfers[mode] = result; write(destination / 'downloads.json', transfers)
        # Validate everything before promoting the installation receipt.
        decode(run(destination, destination / 'env/bin/python', '-I', '-B', '-c', CHECK,
                   destination / 'catalogs', __import__('json').dumps(manifest['connection'])))
        record['phase'] = 'complete'; write(destination / 'installation.json', record)
        return check(folder, destination, expected, manifest)
    finally:
        os.close(fd)


def cache_check(folder, destination, expected):
    manifest = verify(folder, expected); initial = check(folder, destination, expected, manifest)
    with tempfile.TemporaryDirectory(prefix='.cache-check-', dir=private(destination / 'catalogs')) as temp:
        transfers = {mode: download(folder, destination, manifest, mode, Path(temp) / mode)['transfer'] for mode in MODES}
    return {**initial, 'cache_check': transfers}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('verify', 'preflight', 'install', 'check', 'support', 'cache-check'))
    parser.add_argument('--release', type=Path, default=Path(__file__).absolute().parent)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args(argv)
    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < 15: os.nice(15 - os.getpriority(os.PRIO_PROCESS, 0))
        # TUF metadata and venv creation must stay private even with umask 002.
        os.umask(os.umask(0o077) | 0o077)
        if args.command == 'verify':
            m = verify(args.release, args.manifest_sha256)
            result = {'ok': True, 'version': m['version'], 'kind': KIND, 'live_ready': False,
                      'databases_bundled': False, 'origin': m['connection']['origin']}
        elif args.destination is None:
            raise PilotError('explicit_destination_required')
        elif args.command == 'preflight':
            verify(args.release, args.manifest_sha256); result = preflight(args.destination.parent)
        elif args.command == 'install':
            result = install(args.release, args.destination, args.manifest_sha256)
        elif args.command == 'cache-check':
            result = cache_check(args.release, args.destination, args.manifest_sha256)
        else:
            result = check(args.release, args.destination, args.manifest_sha256)
        print(__import__('json').dumps(result, indent=2, sort_keys=True)); return 0
    except (PilotError, OSError, ValueError, KeyError, TypeError) as exc:
        # No downstream stdout/stderr, paths, account values or credentials.
        print(__import__('json').dumps({'ok': False, 'error': str(exc) if isinstance(exc, PilotError)
                                      else 'download_pilot_validation_failed', 'live_ready': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
