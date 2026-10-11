"""Whole-operation HTTPS transport; the prepared upstream entry is disabled.

Spawn avoids inheriting OAuth lease descriptors or forking the threaded TLS server.
Only the fixed worker receives request secrets, over multiprocessing's private pipe.
No arbitrary child target, DNS name, URL, proxy or caller headers are accepted.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import math
import multiprocessing
import os
import re
import signal
import ssl
import threading
import time
from urllib.parse import urlsplit

from .oauth import AuthError
from .provider_contract import exchange_fixture, exchange_upstream, prepare, require_live_provider

MAX_TRUST = 2 * 1024**2
MAX_IPC = 8 * 1024**2  # Bounded JSON re-encoding of the <=1 MiB provider reply.


def _offline():
    if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1':
        raise AuthError('offline_verified_tls_required')


def _boundary(port, execution=None):
    if port is None: require_live_provider(execution)
    else:
        if execution == 'provider-live-pilot': raise AuthError('fixed_provider_required')
        _offline()


@dataclass(frozen=True)
class TrustSnapshot:
    pem: bytes = field(repr=False)

    def context(self):
        if type(self.pem) is not bytes or not 0 < len(self.pem) <= MAX_TRUST:
            raise AuthError('provider_trust_refused')
        if not re.fullmatch(rb'(?:\s*-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*)+', self.pem):
            raise AuthError('provider_trust_refused')
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.set_alpn_protocols(['http/1.1'])
            context.load_verify_locations(cadata=self.pem.decode('ascii'))
            if not 0 < len(context.get_ca_certs()) <= 1024: raise ValueError
            return context
        except Exception: raise AuthError('provider_trust_refused') from None

    def summary(self):
        context = self.context()
        return {'sha256': hashlib.sha256(self.pem).hexdigest(),
                'ca_count': len(context.get_ca_certs()), 'hostname_verified': True,
                'minimum_tls': '1.2', 'environment_overrides_used': False}

    @classmethod
    def system(cls, *, prepared=False):
        """Reference-platform CA file, ignoring SSL_CERT_FILE/DIR and proxies.

        This is a read-only trust inventory, not a successful live HTTPS test.
        Platforms without the compiled OpenSSL CA file refuse rather than falling
        back to operator environment variables or an unverified context.
        """
        if type(prepared) is not bool: raise AuthError('provider_trust_refused')
        if not prepared: _offline()
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cafile=ssl.get_default_verify_paths().openssl_cafile)
            certs = context.get_ca_certs(binary_form=True)
            if not 0 < len(certs) <= 1024: raise ValueError
            pem = ''.join(ssl.DER_cert_to_PEM_cert(cert) for cert in certs).encode('ascii')
            result = cls(pem); result.context(); return result
        except Exception: raise AuthError('provider_trust_refused') from None


def retry_after(headers, *, now=None):
    """Return delay; None halts this process's gate for an excessive valid delay.

    Missing/malformed/duplicate headers back off for 60 s. A valid date/number is
    never truncated to an earlier retry; >24 h halts instead. No request retries.
    """
    values = headers.get_all('Retry-After', [])
    if len(values) != 1 or not isinstance(values[0], str): return 60.0
    value = values[0].strip()
    if len(value) > 80: return None if value.isascii() and value.isdigit() else 60.0
    try:
        if value.isascii() and value.isdigit(): delay = int(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None: raise ValueError
            delay = math.ceil(date.timestamp() - (time.time() if now is None else now))
        return None if delay > 86400 else float(max(0, delay))
    except (ValueError, TypeError, OverflowError): return 60.0


class RequestGate:
    """One in-flight request; monotonic spacing/cooldown shared by local adapters.

    Lifetime counters are bounded and contain no identities, paths or credentials.
    This gate is process-local and explicitly refuses inheritance across fork.
    """
    def __init__(self, *, minimum_interval=.05):
        if type(minimum_interval) not in (float, int) or not 0 < minimum_interval <= 60:
            raise AuthError('invalid_provider_pacing')
        self.interval = float(minimum_interval)
        self._pid = os.getpid(); self._lock = threading.Lock()
        self._next = 0.; self._blocked = 0.
        self._counts = {'attempted': 0, 'dispatched': 0, 'rate_limited': 0}
        self._counter_lock = threading.Lock()

    def _count(self, name):
        with self._counter_lock: self._counts[name] = min(2**63 - 1, self._counts[name] + 1)

    def snapshot(self):
        with self._counter_lock: return dict(self._counts)

    @contextmanager
    def admission(self, deadline):
        _offline()
        if os.getpid() != self._pid: raise AuthError('provider_gate_process_changed')
        self._count('attempted')
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise AuthError('provider_deadline_exceeded')
        try:
            wait = max(self._next, self._blocked) - time.monotonic()
            if wait >= deadline - time.monotonic(): raise AuthError('provider_pacing_deferred')
            if wait > 0: time.sleep(wait)
            if time.monotonic() >= deadline: raise AuthError('provider_deadline_exceeded')
            self._next = time.monotonic() + self.interval
            self._count('dispatched')
            yield
        finally: self._lock.release()

    def limited(self, delay):
        # Called while admission holds the lock, before another request can enter.
        self._blocked = max(self._blocked, float('inf') if delay is None else time.monotonic() + delay)
        self._count('rate_limited')


_gates = {}  # Retain cooldown even when all adapters for this origin are discarded.
_gates_lock = threading.Lock()


def _worker(pipe, port, trust, request, timeout, parent_pid, execution=None):
    """Fixed child entry; no output except one bounded private response frame."""
    try:
        with open(os.devnull, 'wb') as null:
            os.dup2(null.fileno(), 1); os.dup2(null.fileno(), 2)
        # Linux reference platform: a dead requester cannot leave a credential-
        # carrying DNS/TLS worker running. Close the spawn/prctl race before I/O.
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise AuthError('provider_child_setup_refused')
        if os.getppid() != parent_pid: raise AuthError('provider_parent_changed')
        _boundary(port, execution)
        cooldown = 0.
        def status(code, headers):
            nonlocal cooldown
            if code == 429: cooldown = retry_after(headers)
        host, target_port = ('osu.ppy.sh', 443) if port is None else ('127.0.0.1', port)
        connection = http.client.HTTPSConnection(host, target_port, context=trust.context(), timeout=timeout)
        exchange = exchange_upstream if port is None else exchange_fixture
        kwargs = {'execution': execution} if port is None else {}
        code, data = exchange(connection, request, timeout=timeout, on_status=status, **kwargs)
        result = {'status': code, 'data': data, 'cooldown': cooldown}
    except AuthError as error: result = {'error': error.code}
    except Exception: result = {'error': 'provider_transport_uncertain'}
    try:
        encoded = json.dumps(result, allow_nan=False).encode('utf-8')
        if len(encoded) > MAX_IPC: encoded = b'{"error":"provider_reply_too_large"}'
        pipe.send_bytes(encoded)
    finally: pipe.close()


def _owned_exchange(port, trust, request, deadline, *, execution=None):
    """Parent enforces deadline even while libc DNS or HTTP parsing blocks.

    No poll/join before unconditional kill: the unreaped child retains PID identity.
    Cleanup is an additional <=1 s reap allowance, not another request allowance.
    Interruption propagates only after child cleanup. The fixed child spawns none.
    """
    _boundary(port, execution)
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    args = (send, port, trust, request, 5, os.getpid())
    if execution is not None: args += (execution,)
    process = context.Process(target=_worker, args=args, daemon=True)
    try:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM))
        try: process.start()
        finally: signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        send.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not receive.poll(remaining): raise AuthError('provider_deadline_exceeded')
        # Child send_bytes is bounded. poll sees the frame header before its whole
        # body: a recv may block on a stalled writer, so use a socket-independent
        # bounded pipe reader with the same deadline rather than recv_bytes here.
        import select
        import struct
        fd = receive.fileno()
        def read(count):
            chunks = []; total = 0
            while total < count:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([fd], [], [], left)[0]:
                    raise AuthError('provider_deadline_exceeded')
                chunk = os.read(fd, min(16384, count - total))
                if not chunk: raise AuthError('provider_transport_uncertain')
                chunks.append(chunk); total += len(chunk)
            return b''.join(chunks)
        length = struct.unpack('!i', read(4))[0]
        if not 0 < length <= MAX_IPC: raise AuthError('provider_transport_uncertain')
        value = json.loads(read(length))
        if time.monotonic() >= deadline: raise AuthError('provider_deadline_exceeded')
        if 'error' in value: raise AuthError(value['error'])
        return value
    except AuthError: raise
    except Exception: raise AuthError('provider_transport_uncertain') from None
    finally:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM))
        try:
            send.close(); receive.close()
            if process.pid is not None:
                try: os.kill(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.join(timeout=1)
                if process.exitcode is None: raise AuthError('provider_child_cleanup_uncertain')
            process.close()
        finally: signal.pthread_sigmask(signal.SIG_SETMASK, mask)


class DeadlineFixtureTransport:
    offline_fixture = True

    def __init__(self, origin, trust, *, timeout=2, gate=None):
        _offline()
        try:
            parts = urlsplit(origin)
            port, hostname = parts.port, parts.hostname
        except Exception: raise AuthError('offline_verified_tls_required') from None
        if (parts.scheme != 'https' or hostname != '127.0.0.1' or not port
                or parts.netloc != f'127.0.0.1:{port}' or parts.path or parts.query or parts.fragment):
            raise AuthError('offline_verified_tls_required')
        if type(trust) is not TrustSnapshot: raise AuthError('provider_trust_refused')
        trust.context()
        self.port, self.trust, self.timeout = port, trust, timeout
        from .provider_pacing import PersistentRequestGate, ProviderRequestGate
        if gate is not None and type(gate) not in (RequestGate, PersistentRequestGate, ProviderRequestGate): raise AuthError('invalid_provider_pacing')
        with _gates_lock:
            if gate is None:
                gate = _gates.get(self.port)
                if gate is None:
                    if len(_gates) >= 256: raise AuthError('provider_gate_capacity_refused')
                    gate = _gates[self.port] = RequestGate()
        self.gate = gate

    def require_authority(self, root, client_id, *, user_id=None):
        if hasattr(self.gate, 'require_authority'): self.gate.require_authority(root, client_id, user_id=user_id)

    def bind_verified(self, user_id):
        if hasattr(self.gate, 'bind_verified'): self.gate.bind_verified(user_id)

    @contextmanager
    def operation(self, method, path, *, form=None, bearer=None):
        _offline()
        if type(self.timeout) not in (int, float) or not 0 < self.timeout <= 5:
            raise AuthError('invalid_provider_timeout')
        deadline = time.monotonic() + self.timeout
        request = prepare(method, path, form=form, bearer=bearer)
        with self.gate.admission(deadline):
            permit = _Permit(self.port, self.trust, self.gate, request, deadline)
            try: yield permit
            finally: permit.active = False

    def request(self, method, path, *, form=None, bearer=None):
        with self.operation(method, path, form=form, bearer=bearer) as permit:
            return permit.request(method, path, form=form, bearer=bearer)

    @contextmanager
    def api_operation(self, method, path, *, bearer, params=None, json_body=None):
        _offline()
        from .provider_api_contract import prepare_api
        if type(self.timeout) not in (int, float) or not 0 < self.timeout <= 5:
            raise AuthError('invalid_provider_timeout')
        deadline = time.monotonic() + self.timeout
        request = prepare_api(method, path, bearer=bearer, params=params, json_body=json_body)
        with self.gate.admission(deadline):
            permit = _Permit(self.port, self.trust, self.gate, request, deadline)
            try: yield permit
            finally: permit.active = False


class _Permit:
    """One exact request, on the admitting thread and within its context/deadline."""
    def __init__(self, port, trust, gate, request, deadline):
        self.port, self.trust, self.gate, self.prepared, self.deadline = port, trust, gate, request, deadline
        self.thread, self.pid = threading.get_ident(), os.getpid()
        self.active = True; self.used = False

    def check(self):
        _boundary(self.port, getattr(self.gate, 'execution', None))
        if not self.active or self.used or self.thread != threading.get_ident() or self.pid != os.getpid():
            raise AuthError('provider_admission_refused')
        if time.monotonic() >= self.deadline: raise AuthError('provider_deadline_exceeded')

    def request(self, method, path, *, form=None, bearer=None):
        self.check()
        if prepare(method, path, form=form, bearer=bearer) != self.prepared:
            raise AuthError('provider_admission_mismatch')
        return self._dispatch()

    def api_request(self, method, path, *, bearer, params=None, json_body=None):
        self.check()
        from .provider_api_contract import prepare_api
        if prepare_api(method, path, bearer=bearer, params=params, json_body=json_body) != self.prepared:
            raise AuthError('provider_admission_mismatch')
        return self._dispatch()

    def _dispatch(self):
        self.used = True
        persistent = hasattr(self.gate, 'exchange_started')
        if persistent: self.gate.exchange_started()
        try:
            kwargs = {'execution': self.gate.execution} if self.port is None else {}
            value = _owned_exchange(self.port, self.trust, self.prepared, self.deadline, **kwargs)
        except BaseException:
            if persistent: self.gate.uncertain()
            raise
        if persistent: self.gate.replied(value['status'], value['cooldown'])
        elif value['status'] == 429: self.gate.limited(value['cooldown'])
        return value['status'], value['data']
