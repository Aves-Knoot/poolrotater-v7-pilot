"""Download and verify one prepared catalog; never install live host authority."""
from __future__ import annotations

import hashlib
import ctypes
from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading

from .catalog_delivery import HttpsOrigin
from .catalog_export import MODES, inspect_bundle
from .catalog_trust import (BootstrapTrust, ROOT_LIMIT, _private_directory,
                            bounded_file, no_links, verified_local_catalog)
from .config import ConfigError
from .layout import atomic_json, file_lease

_CREATION_LOCK = threading.RLock()


def _private_creation(function):
    @wraps(function)
    def run(*args, **kwargs):
        # TUF persists some metadata with ordinary open(..., 'wb'). Preserve the
        # caller's policy while making creation stricter, including a donor login
        # with umask 002. Serialize our scopes and restore the original policy.
        with _CREATION_LOCK:
            previous = os.umask(0o777)
            os.umask(previous | 0o077)
            try:
                return function(*args, **kwargs)
            finally:
                os.umask(previous)
    return run


def _publish(source, destination):
    # Linux reference target: atomic no-replace publication even if another local
    # process creates an empty destination after the final existence check.
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        raise ConfigError('catalog destination publication refused')


@_private_creation
def download_catalog(*, url, cache, trust_root, trust_sha256, ca_file, ca_sha256,
                     destination, mode, timeout=5., deadline=30.):
    """Publish an owner-private, complete verified kit at a NEW destination.

    Durable caches retain TUF rollback versions and resumable transfers. Failure
    publishes no kit; existing destinations are never adopted/replaced. Bootstrap
    and CA trust must arrive through an independently trusted maintainer channel.
    This does not make a catalog store, authorization receipt or room installation.
    """
    if type(mode) is not str or mode not in MODES:
        raise ConfigError('supported catalog mode required')
    if not isinstance(trust_sha256, str) or not re.fullmatch('[a-f0-9]{64}', trust_sha256):
        raise ConfigError('independently confirmed catalog bootstrap digest required')
    with bounded_file(trust_root, ROOT_LIMIT) as handle:
        data = handle.read()
    if hashlib.sha256(data).hexdigest() != trust_sha256:
        raise ConfigError('catalog bootstrap digest mismatch')
    trust = BootstrapTrust(data)
    destination = no_links(destination)
    parent = _private_directory(destination.parent)
    cache = no_links(cache)
    if (cache == destination or cache.is_relative_to(destination)
            or destination.is_relative_to(cache)):
        raise ConfigError('download destination and durable cache must be separate')
    if os.path.lexists(destination):
        raise ConfigError('catalog download requires a new destination')
    _private_directory(cache, create=True)
    for name in ('metadata-cache', 'transport-cache'):
        _private_directory(cache / name, create=True)
    # Validate explicit TLS input before any transfer; room credentials are never read.
    origin = HttpsOrigin(url, cache / 'transport-cache', timeout=timeout,
                         deadline=deadline, ca_file=ca_file, ca_sha256=ca_sha256)
    with file_lease(parent / '.catalog-download.lock'):
        if os.path.lexists(destination):
            raise ConfigError('catalog download requires a new destination')
        with tempfile.TemporaryDirectory(prefix='.catalog-download-', dir=parent) as temporary:
            stage = Path(temporary)
            with verified_local_catalog(origin, cache / 'metadata-cache', trust, mode,
                                        workspace=stage) as verified:
                required = sum(p.stat().st_size for p in verified.folder.iterdir())
                if shutil.disk_usage(parent).free < 5 * 1024**3 + required:
                    raise ConfigError('catalog publication would consume disk reserve')
                kit = stage / 'result'
                kit.mkdir(mode=0o700)
                shutil.copytree(verified.folder, kit / 'catalog')
                manifest = inspect_bundle(kit / 'catalog')
                if manifest != verified.manifest:
                    raise ConfigError('downloaded catalog changed during publication')
                receipt = {'format': 1, 'mode': mode, 'content_id': manifest['content_id'],
                           'bootstrap_sha256': trust.identity, 'ca_sha256': ca_sha256,
                           'files': manifest['files'], 'archive': manifest['archive'],
                           'activated': False, 'live_ready': False}
                atomic_json(kit / 'download.json', receipt)
                # Sync copied content before atomically publishing its directory.
                for path in (kit / 'catalog').iterdir():
                    with path.open('rb') as handle:
                        os.fsync(handle.fileno())
                for directory in (kit / 'catalog', kit):
                    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
                if os.path.lexists(destination):
                    raise ConfigError('catalog download destination appeared during transfer')
                _publish(kit, destination)
                fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(fd)
                finally: os.close(fd)
    return {'ok': True, **receipt, 'transfer': origin.feedback.get('stats', {}),
            'local_collection_or_analysis': False, 'service_installed': False,
            'start_allowed': False}
