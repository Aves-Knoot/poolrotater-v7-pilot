"""Bounded conditional/resumable delivery with separate fixture and HTTPS entries.

Transport caches have no installation authority. TUF verifies every returned
target and metadata document again; conditional requests never extend expiry.
No proxies, redirects, credentials or automatic retries. Pilot HTTPS requires
an explicit IP origin and independently digest-confirmed CA bytes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import random
import re
import ssl
import socket
import threading
import time
from urllib.parse import urlsplit

from .catalog_export import MAX_FILE, sha256
from .catalog_trust import METADATA_LIMIT, _private_directory, bounded_file, dependencies
from .config import ConfigError
from .layout import atomic_json, file_lease, unique_fields, load
from .runtime_package import fixture_only

CHUNK = 64 * 1024


@dataclass(frozen=True)
class FixtureOrigin:
    url: str
    cache: Path
    timeout: float = 5.0
    deadline: float = 30.0
    ca_file: Path | None = None
    feedback: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self):
        fixture_only()
        p = urlsplit(self.url)
        if (p.scheme not in ('http', 'https') or p.hostname not in ('127.0.0.1', '::1')
                or p.username or p.password or not p.port or p.query or p.fragment
                or p.path != '/' or self.url != self.url.rstrip('/') + '/'):
            raise ConfigError('explicit credential-free loopback fixture origin required')
        if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 < v <= 60
               for v in (self.timeout, self.deadline)):
            raise ConfigError('fixture delivery timeouts must be finite and bounded')
        object.__setattr__(self, 'cache', Path(self.cache).absolute())
        _private_directory(self.cache, create=True)
        with file_lease(self.cache / '.lease'):
            binding = self.cache / 'origin.json'
            expected = {'format': 1, 'origin': self.url}
            if binding.exists():
                if _json(binding) != expected: raise ConfigError('delivery cache origin changed')
            else: atomic_json(binding, expected)

    def fetcher(self, interface, errors):
        origin = self
        class Fetcher(interface):
            def __init__(self):
                self.targets = {}
                self.retry_after = 0
                self.stats = {'requests': 0, 'bytes': 0, 'not_modified': 0,
                              'resumed': 0, 'cached_targets': 0}
                origin.feedback['stats'] = self.stats

            def register(self, info):
                if not 0 < info.length <= MAX_FILE or set(info.hashes) != {'sha256'}:
                    raise ConfigError('delivery requires signed SHA256 target length')
                dirname, _, basename = info.path.rpartition('/')
                name = (dirname + '/' if dirname else '') + info.hashes['sha256'] + '.' + basename
                self.targets['targets/' + name] = info

            def _fetch(self, url):
                if not url.startswith(origin.url): raise errors.DownloadError('fixture origin changed')
                name = url[len(origin.url):]
                if (not re.fullmatch(r'(metadata|targets)/[a-zA-Z0-9_.\-/]+', name)
                        or any(part in ('', '.', '..') for part in name.split('/'))):
                    raise errors.DownloadError('unsafe fixture delivery path')
                if name.startswith('targets/') and name not in self.targets:
                    raise errors.DownloadError('target delivery requires verified metadata identity')
                with file_lease(origin.cache / '.lease'):
                    try:
                        yield from self._transfer(name)
                    except (OSError, ValueError, http.client.HTTPException) as exc:
                        raise errors.DownloadError('fixture transfer failed: ' + type(exc).__name__) from exc

            def _transfer(self, name):
                key = hashlib.sha256(name.encode()).hexdigest()
                data = origin.cache / (key + '.data')
                partial = origin.cache / (key + '.part')
                record = origin.cache / (key + '.json')
                entries = list(origin.cache.iterdir())
                if len(entries) > 192: raise errors.DownloadError('delivery cache file budget exceeded')
                for p in entries:
                    if p.name not in ('.lease', 'origin.json') and not re.fullmatch(r'[a-f0-9]{64}\.(data|part|json)', p.name):
                        raise errors.DownloadError('unattributable delivery cache entry')
                    if p.is_symlink() or not p.is_file() or p.stat().st_nlink != 1 or p.stat().st_mode & 0o077:
                        raise errors.DownloadError('unsafe delivery cache entry')
                info = self.targets.get(name)
                maximum = info.length if info else METADATA_LIMIT
                state = _json(record) if record.exists() else {}
                if state and (not isinstance(state,dict) or set(state) != {'name', 'etag', 'bytes', 'sha256', 'complete'}
                              or state['name'] != name or type(state['complete']) is not bool
                              or type(state['bytes']) is not int or not 0 <= state['bytes'] <= maximum
                              or not re.fullmatch('[a-f0-9]{64}', state['sha256'])
                              or not _etag(state['etag'])):
                    raise errors.DownloadError('invalid delivery cache record')
                if info and data.exists():
                    with bounded_file(data, maximum) as f:
                        if data.stat().st_size == info.length and sha256(data) == info.hashes['sha256']:
                            self.stats['cached_targets'] += 1
                            while chunk := f.read(CHUNK): yield chunk
                            return
                    data.unlink()
                headers = {'Accept-Encoding': 'identity', 'Connection': 'close'}
                offset = 0
                if info and state and not state['complete'] and partial.exists():
                    with bounded_file(partial, maximum) as f:
                        if partial.stat().st_size == state['bytes'] and sha256(partial) == state['sha256']:
                            offset = state['bytes']
                    if offset and offset < info.length:
                        headers.update(Range=f'bytes={offset}-', **{'If-Range': state['etag']})
                    else: offset = 0
                elif not info and state and state['complete'] and data.exists():
                    with bounded_file(data, maximum):
                        if data.stat().st_size != state['bytes'] or sha256(data) != state['sha256']:
                            raise errors.DownloadError('conditional metadata cache changed')
                    headers['If-None-Match'] = state['etag']
                free = os.statvfs(origin.cache).f_bavail * os.statvfs(origin.cache).f_frsize
                if free < 5 * 1024**3 + maximum:
                    raise errors.DownloadError('delivery would consume disk reserve')
                p = urlsplit(origin.url)
                started = time.monotonic()
                remaining = lambda: max(.001, min(origin.timeout, origin.deadline - (time.monotonic() - started)))
                if p.scheme == 'https':
                    context = origin.tls_context()
                    connection = http.client.HTTPSConnection(p.hostname, p.port, timeout=remaining(), context=context)
                else: connection = http.client.HTTPConnection(p.hostname, p.port, timeout=remaining())
                response = None
                def expire():
                    sock = connection.sock
                    if sock is None and response is not None and response.fp is not None:
                        sock = getattr(getattr(response.fp, 'raw', None), '_sock', None)
                    if sock is not None:
                        try: sock.shutdown(socket.SHUT_RDWR)
                        except OSError: pass
                    connection.close()
                timer = threading.Timer(origin.deadline, expire)
                timer.daemon = True; timer.start()
                try:
                    self.stats['requests'] += 1
                    connection.request('GET', '/' + name, headers=headers)
                    response = connection.getresponse()
                    if response.status == 429:
                        from .provider_transport import retry_after
                        delay = retry_after(response.headers)
                        if delay is None: origin.feedback['halted'] = True
                        else: origin.feedback['retry_after'] = delay
                        raise errors.DownloadHTTPError('fixture delivery cooldown', 429)
                    if response.status == 304:
                        if info or 'If-None-Match' not in headers or response.getheader('ETag') != state['etag']:
                            raise errors.DownloadError('invalid conditional response')
                        self.stats['not_modified'] += 1
                        with bounded_file(data, maximum) as f:
                            while chunk := f.read(CHUNK): yield chunk
                        return
                    if response.status not in (200, 206):
                        raise errors.DownloadHTTPError('fixture delivery refused response', response.status)
                    if response.getheader('Content-Encoding', 'identity') != 'identity' or response.getheader('Transfer-Encoding'):
                        raise errors.DownloadError('unsupported delivery encoding')
                    length = response.getheader('Content-Length', '')
                    if not re.fullmatch('[0-9]{1,12}', length) or not 0 < int(length) <= maximum:
                        raise errors.DownloadError('missing or oversized response length')
                    etag = response.getheader('ETag', '')
                    if not _etag(etag): raise errors.DownloadError('stable strong object validator required')
                    if response.status == 206:
                        if (not offset or etag != state['etag']
                                or response.getheader('Content-Range') != f'bytes {offset}-{info.length-1}/{info.length}'
                                or int(length) != info.length-offset):
                            partial.unlink(missing_ok=True); record.unlink(missing_ok=True)
                            raise errors.DownloadError('resumed identity/range mismatch; partial discarded')
                        self.stats['resumed'] += 1
                    else: offset = 0
                    if info and int(length) != info.length-offset:
                        partial.unlink(missing_ok=True); record.unlink(missing_ok=True)
                        raise errors.DownloadError('signed response length mismatch')
                    digest = hashlib.sha256()
                    if offset:
                        with bounded_file(partial, maximum) as f:
                            while chunk := f.read(CHUNK): digest.update(chunk)
                    try:
                        with partial.open('ab' if offset else 'wb') as f:
                            partial.chmod(0o600)
                            received = offset
                            while received < offset + int(length):
                                if time.monotonic()-started >= origin.deadline: raise TimeoutError('delivery deadline')
                                if connection.sock: connection.sock.settimeout(remaining())
                                chunk = response.read1(min(CHUNK, offset + int(length)-received))
                                if not chunk: raise errors.DownloadError('truncated fixture response')
                                f.write(chunk); digest.update(chunk); received += len(chunk); self.stats['bytes'] += len(chunk)
                            f.flush(); os.fsync(f.fileno())
                    except BaseException:
                        if partial.exists() and partial.stat().st_size:
                            atomic_json(record, {'name': name, 'etag': etag, 'bytes': partial.stat().st_size,
                                                 'sha256': sha256(partial), 'complete': False})
                        raise
                    if info and digest.hexdigest() != info.hashes['sha256']:
                        partial.unlink(missing_ok=True); record.unlink(missing_ok=True)
                        raise errors.DownloadError('signed target digest mismatch; partial discarded')
                    os.replace(partial, data)
                    atomic_json(record, {'name': name, 'etag': etag, 'bytes': data.stat().st_size,
                                         'sha256': digest.hexdigest(), 'complete': True})
                    with bounded_file(data, maximum) as f:
                        while chunk := f.read(CHUNK): yield chunk
                finally:
                    timer.cancel(); timer.join(timeout=1); connection.close()
        return Fetcher()

    def tls_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cafile=str(self.ca_file) if self.ca_file else ssl.get_default_verify_paths().openssl_cafile)
        return context


@dataclass(frozen=True)
class HttpsOrigin(FixtureOrigin):
    """Explicit pilot download permission, independent of room/provider authority.

    IP-only origins avoid unbounded DNS resolution in this first network entry.
    A manually confirmed CA digest anchors TLS; TUF bootstrap trust is separate.
    CA bytes are snapshotted once, never rediscovered from environment or a server.
    The cache is bound to both origin and CA, and cannot adopt a fixture cache.
    """
    ca_sha256: str | None = None
    _ca_data: bytes = field(default=b'', init=False, repr=False, compare=False)

    def __post_init__(self):
        try:
            p = urlsplit(self.url)
            address = ipaddress.ip_address(p.hostname)
            port = p.port
            host = '[' + str(address) + ']' if address.version == 6 else str(address)
            canonical = 'https://' + host + (':' + str(port) if port is not None else '') + '/'
            valid = (p.scheme == 'https' and not p.username and not p.password
                     and not p.query and not p.fragment and p.path == '/'
                     and (port is None or 1 <= port <= 65535)
                     and self.url == canonical and not address.is_unspecified
                     and not address.is_multicast and not address.is_link_local)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ConfigError('explicit canonical credential-free HTTPS IP origin required')
        if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 < v <= 60
               for v in (self.timeout, self.deadline)):
            raise ConfigError('HTTPS delivery timeouts must be finite and bounded')
        if (self.ca_file is None or not isinstance(self.ca_sha256, str)
                or not re.fullmatch('[a-f0-9]{64}', self.ca_sha256)):
            raise ConfigError('independently confirmed catalog CA digest required')
        with bounded_file(self.ca_file, 256 * 1024) as handle:
            data = handle.read()
        if hashlib.sha256(data).hexdigest() != self.ca_sha256:
            raise ConfigError('catalog CA digest mismatch')
        object.__setattr__(self, '_ca_data', data)
        try:
            self.tls_context()
        except (ssl.SSLError, UnicodeError, ValueError):
            raise ConfigError('invalid catalog CA certificate') from None
        object.__setattr__(self, 'cache', Path(self.cache).absolute())
        _private_directory(self.cache, create=True)
        with file_lease(self.cache / '.lease'):
            binding = self.cache / 'origin.json'
            expected = {'format': 1, 'execution': 'https-catalog-download',
                        'origin': self.url, 'ca_sha256': self.ca_sha256}
            if binding.exists():
                if _json(binding) != expected:
                    raise ConfigError('delivery cache origin or TLS trust changed')
            else:
                atomic_json(binding, expected)

    def tls_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cadata=self._ca_data.decode('ascii'))
        return context


def _etag(value):
    return isinstance(value, str) and re.fullmatch(r'"[a-zA-Z0-9._-]{1,128}"', value) is not None


def _json(path):
    with bounded_file(path, 65536) as f:
        try: return json.load(f, object_pairs_hook=unique_fields)
        except (ValueError, UnicodeError) as exc: raise ConfigError('invalid delivery state') from exc


class RefreshClient:
    """Durable daily jitter/backoff; updates desired refs, never active workers."""
    def __init__(self, store, origin, *, clock=time.time, jitter=None):
        fixture_only()
        if not isinstance(origin, FixtureOrigin): raise ConfigError('explicit fixture origin required')
        self.store, self.origin, self.clock = store, origin, clock
        self.jitter = jitter or random.SystemRandom().uniform
        self.state = store.path / '.staging/refresh.json'

    def refresh(self, *, force=False):
        return self._refresh(force=force, exclusions=False)

    def refresh_exclusions(self, *, force=False):
        return self._refresh(force=force, exclusions=True)

    def _refresh(self, *, force, exclusions):
        if type(force) is not bool: raise ConfigError('invalid refresh force flag')
        state_path = self.store.path / '.staging/exclusion-refresh.json' if exclusions else self.state
        with file_lease(self.store.path / '.staging/.refresh.lock'):
            now = self.clock()
            if type(now) not in (int, float) or not math.isfinite(now) or now < 0: raise ConfigError('invalid refresh clock')
            state = _json(state_path) if state_path.exists() else {
                'format': 1, 'origin': self.origin.url, 'next': 0, 'failures': 0, 'last_error': None, 'halted':False}
            if (not isinstance(state, dict) or set(state) != {'format', 'origin', 'next', 'failures', 'last_error','halted'}
                    or type(state['format']) is not int or state['format'] != 1 or state['origin'] != self.origin.url
                    or type(state['next']) not in (int, float) or not math.isfinite(state['next']) or state['next'] < 0
                    or type(state['failures']) is not int or not 0 <= state['failures'] <= 100
                    or type(state['halted']) is not bool or state['last_error'] not in (None,'ConfigError','OSError')):
                raise ConfigError('invalid bound refresh schedule')
            if state['halted']: return {'status':'held','reason':'excessive_retry_after','next':None}
            if now < state['next'] and (state['failures'] or not force):
                return {'status': 'waiting', 'next': state['next'], 'last_error': state['last_error']}
            _, config = load(self.store.root)
            from .catalog_store import mode_for
            modes = sorted({mode_for(room) for room in config['rooms']})
            before = self.store.exclusions() if exclusions else self.store.current()
            try:
                after = self.store.accept_exclusions(self.origin) if exclusions else self.store.stage(self.origin, modes)
                if after != before and not exclusions: self.store.commit()
            except (ConfigError, OSError) as exc:
                observed=self.clock()
                if type(observed) in (int,float) and math.isfinite(observed):now=max(now,observed)
                failures = min(100, state['failures'] + 1)
                # A transport 429 is surfaced as a bounded cooldown by origin fetchers.
                wait = max(60, self.origin.feedback.pop('retry_after', 0), min(3600, 30 * 2**min(failures, 7)))
                state.update(next=now + wait + self.jitter(0, wait * .2), failures=failures,
                             last_error='ConfigError' if isinstance(exc,ConfigError) else 'OSError',
                             halted=self.origin.feedback.pop('halted',False))
                atomic_json(state_path, state)
                return {'status': 'held' if state['halted'] else 'retryable', 'installed': bool(self.store.current()['modes']), **state}
            interval = 900 if exclusions else 86400
            state.update(next=now + interval + self.jitter(0, interval*.1), failures=0, last_error=None)
            atomic_json(state_path, state)
            return {'status': 'updated' if after != before else 'unchanged', 'reference': after, 'next': state['next']}
