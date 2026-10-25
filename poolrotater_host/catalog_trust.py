"""Optional TUF verification of local catalog kits; no activation or network IO.

Bootstrap trust is explicitly supplied, never obtained from the candidate/cache.
Temporary verified files live only for the duration of the returned context.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import gzip
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import stat
import tarfile
import tempfile
from urllib.parse import unquote, urlsplit

from .catalog_export import MAX_FILE, MODES, inspect_bundle, read_manifest, sha256
from .config import ConfigError
from .layout import atomic_json, file_lease, unique_fields

PINS = {'tuf': '7.0.1', 'securesystemslib': '1.5.1', 'cryptography': '50.0.2',
        'cffi': '2.1.1', 'pycparser': '3.0', 'urllib3': '2.8.0'}
ROOT_LIMIT = 64 * 1024
METADATA_LIMIT = 256 * 1024
MANIFEST_LIMIT = 64 * 1024
ORIGIN = 'https://local-catalog.invalid/'  # Routed exclusively to local files.


def dependencies():
    """Lazy optional imports: absence/version drift refuses verification only."""
    try:
        if any(version(name) != pinned for name, pinned in PINS.items()):
            raise ConfigError('catalog verification dependency versions differ from reviewed pins')
        from tuf.ngclient import Updater
        from tuf.ngclient.config import UpdaterConfig
        from tuf.ngclient.fetcher import FetcherInterface
        from tuf.api import exceptions
    except (ImportError, PackageNotFoundError) as exc:
        raise ConfigError('install the hash-pinned optional catalog verification dependencies') from exc
    return Updater, UpdaterConfig, FetcherInterface, exceptions


def no_links(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ConfigError('catalog paths must not contain symlinks')
    return path


def bounded_file(path, limit):
    path = no_links(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    handle = os.fdopen(fd, 'rb')
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or not 0 < st.st_size <= limit:
            raise ConfigError('catalog input is not a bounded regular unlinked file')
    except BaseException:
        handle.close()
        raise
    return handle


@dataclass(frozen=True)
class BootstrapTrust:
    data: bytes
    fixture: bool = False

    def __post_init__(self):
        if type(self.data) is not bytes or not 0 < len(self.data) <= ROOT_LIMIT:
            raise ConfigError('explicit bounded bootstrap root bytes are required')
        # Direct bytes are an explicit trust binding provided by the caller. Fixture
        # marking neither weakens TUF verification nor authorizes live operation.
        if type(self.fixture) is not bool or (self.fixture and os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1'):
            raise ConfigError('fixture trust requires offline isolation')

    @property
    def identity(self):
        return hashlib.sha256(self.data).hexdigest()

    @classmethod
    def protected_file(cls, path):
        path = no_links(path)
        for p in (path, *path.parents):
            st = p.stat()
            if st.st_uid != 0 or st.st_mode & 0o022 or (os.geteuid() != 0 and os.access(p, os.W_OK)):
                raise ConfigError('bootstrap root and ancestors must be protected from updater writes')
        with bounded_file(path, ROOT_LIMIT) as f:
            return cls(f.read(ROOT_LIMIT + 1))


@dataclass(frozen=True)
class VerificationLimits:
    archive_bytes: int = MAX_FILE
    expanded_bytes: int = 2 * MAX_FILE
    reserve_bytes: int = 5 * 1024**3

    def __post_init__(self):
        if (type(self.archive_bytes) is not int or not 0 < self.archive_bytes <= MAX_FILE
                or type(self.expanded_bytes) is not int or not 0 < self.expanded_bytes <= 2 * MAX_FILE
                or type(self.reserve_bytes) is not int or self.reserve_bytes < 5 * 1024**3):
            raise ConfigError('catalog quotas may tighten but not weaken default limits')


@dataclass(frozen=True)
class VerifiedCatalog:
    folder: Path
    manifest: dict
    bootstrap_sha256: str
    fixture_trust: bool


def _private_directory(path, *, create=False):
    path = no_links(path)
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    st = path.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise ConfigError('catalog cache/workspace must be owned and private (0700)')
    return path


def _cache_check(cache):
    # TUF alone creates root.json -> root_history/N.root.json. No other links.
    for index, p in enumerate(cache.rglob('*')):
        if index >= 256:
            raise ConfigError('catalog metadata cache file budget exceeded')
        st = p.lstat()
        relative = p.relative_to(cache).as_posix()
        if p.is_symlink():
            if relative != 'metadata/root.json' or not re.fullmatch(r'root_history/[1-9][0-9]{0,9}\.root\.json', os.readlink(p)):
                raise ConfigError('unsafe catalog metadata cache link')
            continue
        if st.st_uid != os.geteuid() or st.st_mode & 0o022:
            raise ConfigError('catalog metadata cache ownership/permissions changed')
        if stat.S_ISDIR(st.st_mode):
            if relative not in {'metadata', 'metadata/root_history'}:
                raise ConfigError('unexpected catalog cache directory')
        elif (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
              or st.st_size > (ROOT_LIMIT if relative.startswith('metadata/root_history/')
                               else 16 * 1024 if relative == 'metadata/timestamp.json'
                               else 128 * 1024 if relative == 'metadata/snapshot.json'
                               else METADATA_LIMIT)):
            raise ConfigError('unsafe or oversized catalog metadata cache file')
        elif not (relative in {'.lease', 'bootstrap.json', 'metadata/timestamp.json', 'metadata/snapshot.json', 'metadata/targets.json'}
                  or re.fullmatch(r'metadata/root_history/[1-9][0-9]{0,9}\.root\.json', relative)):
            raise ConfigError('unexpected catalog metadata cache file')


def _fetcher(repository, interface, errors):
    from .catalog_delivery import FixtureOrigin
    if isinstance(repository, FixtureOrigin):
        return repository.fetcher(interface, errors)
    repository = no_links(repository)
    if not repository.is_dir():
        raise ConfigError('local catalog repository must be a directory')

    class LocalFetcher(interface):
        def _fetch(self, url):
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.netloc != 'local-catalog.invalid' or parsed.query or parsed.fragment:
                raise errors.DownloadError('local catalog transport refuses external origins')
            name = unquote(parsed.path.lstrip('/'))
            if (not re.fullmatch(r'(metadata|targets)/[a-zA-Z0-9_.\-/]+', name)
                    or any(p in {'', '.', '..'} for p in name.split('/'))):
                raise errors.DownloadError('unsafe local repository path')
            limit = METADATA_LIMIT if name.startswith('metadata/') else MAX_FILE
            try:
                with bounded_file(repository / name, limit) as f:
                    while chunk := f.read(1024**2):
                        yield chunk
            except FileNotFoundError as exc:
                raise errors.DownloadHTTPError('local repository entry absent', 404) from exc
    return LocalFetcher()


class _ExpandedReader:
    def __init__(self, stream, maximum):
        self.stream, self.remaining = stream, maximum

    def read(self, size):
        if size < 0 or size > self.remaining:
            raise ConfigError('catalog decompression budget exceeded')
        result = self.stream.read(size)
        self.remaining -= len(result)
        return result


def _extract(archive, output, manifest, limits):
    """Strict flat USTAR: inspect every header, byte, pad and trailing block."""
    expected = {f['name']: f for f in manifest['files']}
    seen = set()
    budget = manifest['archive']['expanded_bytes'] + 20 * 1024
    with archive.open('rb') as compressed, gzip.GzipFile(fileobj=compressed) as decompressed:
        reader = _ExpandedReader(decompressed, budget)
        while True:
            header = reader.read(512)
            if header == bytes(512):
                if reader.read(512) != bytes(512):
                    raise ConfigError('invalid catalog tar end marker')
                while reader.remaining:
                    if any(reader.read(min(512, reader.remaining))):
                        raise ConfigError('unexpected trailing catalog archive data')
                    if decompressed.peek(1) == b'':
                        break
                # Read at most one byte past the finite decompression budget.
                if decompressed.read(1):
                    raise ConfigError('catalog decompression budget exceeded')
                break
            if len(header) != 512 or header[257:263] != b'ustar\0':
                raise ConfigError('catalog archive requires bounded USTAR headers')
            member = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
            if (member.name not in expected or member.name in seen or member.type != tarfile.REGTYPE
                    or member.mode & 0o7111 or member.linkname or member.size != expected[member.name]['bytes']
                    or member.size > limits.expanded_bytes):
                raise ConfigError('unsafe, duplicate or unexpected catalog archive entry')
            seen.add(member.name)
            digest = hashlib.sha256()
            with (output / member.name).open('xb') as f:
                remaining = member.size
                while remaining:
                    data = reader.read(min(1024**2, remaining))
                    if not data:
                        raise ConfigError('truncated catalog archive file')
                    f.write(data); digest.update(data); remaining -= len(data)
            if digest.hexdigest() != expected[member.name]['sha256']:
                raise ConfigError('catalog archive file digest mismatch')
            if any(reader.read((-member.size) % 512)):
                raise ConfigError('invalid catalog archive padding')
        if seen != set(expected):
            raise ConfigError('catalog archive required companion missing')


def _target(updater, name, *, limit, expected=None):
    info = updater.get_targetinfo(name)
    if info is None or type(info.length) is not int or not 0 < info.length <= limit or set(info.hashes) != {'sha256'}:
        raise ConfigError('catalog target missing or outside supported hash/length limits')
    if expected and (info.length != expected['bytes'] or info.hashes['sha256'] != expected['sha256']):
        raise ConfigError('signed target and catalog manifest disagree')
    return info


@contextmanager
def verified_local_catalog(repository, cache, trust, mode, *, workspace=None, limits=None):
    """Authenticate local targets and yield a validated temporary reader kit.

    Never changes an installed catalog. Cache versions persist across calls for
    TUF rollback protection; each cache is bound to one explicit bootstrap root.
    """
    if not isinstance(trust, BootstrapTrust) or type(mode) is not str or mode not in MODES:
        raise ConfigError('explicit bootstrap trust and supported catalog mode required')
    limits = limits or VerificationLimits()
    if not isinstance(limits, VerificationLimits):
        raise ConfigError('invalid catalog verification quotas')
    Updater, Config, Fetcher, errors = dependencies()
    cache = _private_directory(cache, create=True)
    workspace = _private_directory(workspace or cache.parent)
    try:
        with ExitStack() as stack:
            with file_lease(cache / '.lease'):
                _cache_check(cache)
                binding = cache / 'bootstrap.json'
                expected = {'format': 1, 'sha256': trust.identity}
                if binding.exists():
                    with bounded_file(binding, 1024) as f:
                        if json.load(f, object_pairs_hook=unique_fields) != expected:
                            raise ConfigError('catalog cache belongs to a different bootstrap trust')
                else:
                    atomic_json(binding, expected)
                metadata = cache / 'metadata'; metadata.mkdir(mode=0o700, exist_ok=True)
                fetcher = _fetcher(repository, Fetcher, errors)
                origin = getattr(repository, 'url', ORIGIN)
                updater = Updater(str(metadata), origin + 'metadata/',
                                  target_base_url=origin + 'targets/', bootstrap=trust.data,
                                  fetcher=fetcher,
                                  config=Config(max_root_rotations=16, max_delegations=0,
                                                root_max_length=ROOT_LIMIT, timestamp_max_length=16 * 1024,
                                                snapshot_max_length=128 * 1024, targets_max_length=METADATA_LIMIT))
                updater.refresh()
                temp = stack.enter_context(tempfile.TemporaryDirectory(prefix='.catalog-verify-', dir=workspace))
                folder = Path(temp)
                info = _target(updater, mode + '/manifest.json', limit=MANIFEST_LIMIT)
                if hasattr(fetcher, 'register'): fetcher.register(info)
                updater.download_target(info, filepath=str(folder / 'manifest.json'))
                manifest = read_manifest(folder / 'manifest.json')
                if manifest['mode'] != mode:
                    raise ConfigError('signed catalog mode differs from requested preset')
                archive = manifest['archive']
                if archive['bytes'] > limits.archive_bytes or archive['expanded_bytes'] > limits.expanded_bytes:
                    raise ConfigError('catalog target exceeds configured storage quota')
                for f in manifest['files']:
                    _target(updater, mode + '/' + f['name'], limit=MAX_FILE, expected=f)
                info = _target(updater, mode + '/bundle.tar.gz', limit=limits.archive_bytes, expected=archive)
                if hasattr(fetcher, 'register'): fetcher.register(info)
                available = os.statvfs(folder).f_bavail * os.statvfs(folder).f_frsize
                if available < limits.reserve_bytes + 3 * archive['bytes'] + archive['expanded_bytes'] + MANIFEST_LIMIT:
                    raise ConfigError('catalog verification would consume disk reserve')
                updater.download_target(info, filepath=str(folder / 'bundle.tar.gz'))
                _extract(folder / 'bundle.tar.gz', folder, manifest, limits)
                inspected = inspect_bundle(folder)
                for path in folder.iterdir():
                    path.chmod(0o400)
                _cache_check(cache)
                verified = VerifiedCatalog(folder, inspected, trust.identity, trust.fixture)
                # Release cache lock before callers select/use the private result.
            yield verified
    except ConfigError:
        raise
    except (errors.RepositoryError, errors.DownloadError, OSError, ValueError, EOFError, tarfile.TarError, UnicodeError) as exc:
        raise ConfigError('catalog trust/validation failed: ' + type(exc).__name__) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, type=Path)
    parser.add_argument('--cache', required=True, type=Path)
    parser.add_argument('--trust-root', required=True, type=Path)
    parser.add_argument('--mode', required=True, choices=MODES)
    args = parser.parse_args(argv)
    try:
        trust = BootstrapTrust.protected_file(args.trust_root)
        with verified_local_catalog(args.repository, args.cache, trust, args.mode) as kit:
            print(json.dumps({'content_id': kit.manifest['content_id'], 'mode': args.mode,
                              'bootstrap_sha256': kit.bootstrap_sha256,
                              'activated': False, 'manifest': kit.manifest}, sort_keys=True))
    except (ConfigError, OSError) as exc:
        parser.exit(2, (str(exc) if isinstance(exc, ConfigError) else type(exc).__name__) + '\n')
    return 0


def verified_target(repository, cache, trust, name, *, maximum=65536):
    """Authenticate a bounded application target using the same bootstrap/cache.

    Exclusion targets have independent application revisions; TUF metadata replay
    protection is shared with catalogs. Returned bytes are not store authority.
    """
    if (not isinstance(trust, BootstrapTrust) or name not in ('exclusions.json','exclusions-v2.json','exclusions-loved.json','exclusions-taiko.json')
            or type(maximum) is not int or not 0 < maximum <= 65536):
        raise ConfigError('explicit trust and bounded exclusion target required')
    Updater, Config, Fetcher, errors = dependencies()
    cache = _private_directory(cache, create=True)
    try:
        with file_lease(cache / '.lease'):
            _cache_check(cache)
            binding = cache / 'bootstrap.json'; expected = {'format': 1, 'sha256': trust.identity}
            if binding.exists():
                with bounded_file(binding, 1024) as f:
                    if json.load(f, object_pairs_hook=unique_fields) != expected: raise ConfigError('catalog cache bootstrap changed')
            else: atomic_json(binding, expected)
            metadata = cache / 'metadata'; metadata.mkdir(mode=0o700, exist_ok=True)
            fetcher = _fetcher(repository, Fetcher, errors); origin = getattr(repository, 'url', ORIGIN)
            updater = Updater(str(metadata), origin+'metadata/', target_base_url=origin+'targets/',
                              bootstrap=trust.data, fetcher=fetcher,
                              config=Config(max_root_rotations=16, max_delegations=0, root_max_length=ROOT_LIMIT,
                                            timestamp_max_length=16*1024, snapshot_max_length=128*1024,
                                            targets_max_length=METADATA_LIMIT))
            updater.refresh(); info = _target(updater, name, limit=maximum)
            if hasattr(fetcher, 'register'): fetcher.register(info)
            with tempfile.TemporaryDirectory(prefix='.feed-', dir=cache.parent) as tmp:
                path = Path(tmp) / 'feed.json'; updater.download_target(info, filepath=str(path))
                with bounded_file(path, maximum) as f: result = f.read()
            _cache_check(cache)
            return result
    except ConfigError: raise
    except (errors.RepositoryError, errors.DownloadError, OSError, ValueError, UnicodeError) as exc:
        raise ConfigError('exclusion trust/validation failed: '+type(exc).__name__) from exc


if __name__ == '__main__':
    raise SystemExit(main())
