"""Lifetime-authorized fixed RefereeHub adapter; never replays invocations."""
import math
import logging
import queue
import threading
import time
from dataclasses import dataclass

from .oauth import AuthError
from .referee_contract import negotiation
from .referee_protocol import MAX_COMMAND, encoded
from .referee_wire import ChildSession


log = logging.getLogger(__name__)
# External wire errors and exception messages may contain untrusted data.
# Only these source-owned codes enter parent audit logs; never event payloads,
# bearer material, response bodies, endpoints or upstream error text.
_FAILURE_LOG_CODES = frozenset((
    'referee_transport_uncertain', 'referee_reply_refused', 'referee_closed',
    'referee_event_refused', 'referee_event_overflow', 'referee_child_cleanup_uncertain',
    'referee_unauthorized', 'referee_rate_limited', 'referee_unavailable',
    'referee_deadline_exceeded', 'referee_pipe_closed', 'referee_packet_refused',
    'referee_upgrade_deadline', 'referee_upgrade_refused', 'referee_child_setup_refused',
    'referee_parent_changed', 'referee_handshake_deadline', 'referee_handshake_refused',
    'referee_receive_deadline', 'referee_socket_closed', 'referee_stopped',
    'referee_message_refused', 'referee_message_too_large', 'referee_record_flood',
    'referee_record_deadline', 'referee_frame_refused', 'referee_frame_deadline',
    'verified_owner_required', 'provider_trust_refused', 'qualified_provider_transport_required',
))


def _log_failure(error, generation, session=None):
    code = error.code if type(error) is AuthError and error.code in _FAILURE_LOG_CODES else 'referee_transport_uncertain'
    # Generation is receiver-owned, not an upstream identity or payload.
    number = generation if type(generation) is int and 0 <= generation < 10**19 else 0
    log.warning('shared referee transport interrupted: code=%s generation=%d', code, number)
    if session is not None:
        try:
            detail=session.diagnostics()
            status=detail.get('exit_code')
            uptime=detail.get('uptime_seconds')
            if ((status is None or type(status) is int and -128<=status<=255)
                    and type(uptime) in (int,float) and math.isfinite(uptime) and 0<=uptime<=10**9):
                log.warning('referee child observation at interruption: generation=%d exit_code=%s uptime_seconds=%.2f',
                            number,status,uptime)
        except Exception:
            log.warning('referee child observation unavailable: generation=%d',number)


@dataclass(frozen=True)
class _ProviderEvent:
    """Local receiver provenance; never populated from upstream JSON fields."""
    name: str
    payload: dict
    generation: int
    sequence: int


_ROSTER_EVENTS = frozenset(('UserJoined', 'UserLeft', 'UserKicked'))


def referee_port(auth):
    """Closed destination selection; caller cannot supply an upstream origin."""
    from .provider_preflight import FixedProviderTransport, PilotProviderTransport, LivePilotTransport
    from .provider_transport import DeadlineFixtureTransport
    auth.require_authorization_available()
    if (auth.execution in ('provider-disabled', 'provider-pilot', 'provider-live-pilot')
            and type(auth.transport) in (FixedProviderTransport, PilotProviderTransport, LivePilotTransport)
            and auth.transport.execution == auth.execution):
        return None  # ChildSession repeats the fixed spectator release fence.
    if auth.execution == 'provider-fixture' and type(auth.transport) is DeadlineFixtureTransport:
        return auth.transport.port
    raise AuthError('qualified_provider_transport_required')


def _timeout(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise AuthError('referee_timeout_refused')
    return value


class ProviderRefereeClient:
    def __init__(self, auth):
        from .provider_rooms import _authority
        _authority(auth)
        self.auth = auth
        self.events = queue.Queue(maxsize=128)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._attempt = threading.Event()
        self._manager = None
        self._session = None
        self._failure = None
        self._cleanup_failed = False
        self._connect_generation = 0
        self._next_id = 0
        self._pending = {}
        self._event_sequence = 0
        self._roster_revision = 0
        self._roster_pending = {}

    def make_room(self, name, beatmap_id, ruleset_id=0):
        from bot.hub import RefereeHub
        return RefereeHub.make_room(self, name, beatmap_id, ruleset_id)

    def _boundary(self):
        if self._cleanup_failed: raise AuthError('referee_child_cleanup_uncertain')
        if not self.auth._verified or self.auth._active is None:
            raise AuthError('verified_owner_required')
        guard = getattr(self.auth, '_room_guard', self.auth._local_guard)
        if guard is not None: guard()
        if self._stop.is_set(): raise AuthError('referee_stopped')

    def _stage(self, kind, command=None):
        gate = self.auth.transport.gate
        # Authority checking may acquire the persistent ledger lease. Do it
        # before admission; opening already holds the lifetime authority mutex.
        port = referee_port(self.auth) if command is None else None
        deadline = time.monotonic()+5
        if command is None:
            from .process_startup import preflight
            preflight()
        with gate.admission(deadline):
            self._boundary()
            gate.exchange_started(kind='referee')
            try:
                if command is None:
                    kwargs = {'execution': self.auth.execution} if port is None else {}
                    self._session = ChildSession(port, self.auth.transport.trust, self._token, **kwargs)
                else: self._session.send(command, deadline)
                value = self._session.receive(deadline, self._stop)
                if value.get('kind') != kind: raise AuthError('referee_reply_refused')
            except BaseException:
                gate.uncertain()
                raise
            gate.replied(value['status'], value.get('cooldown', 0))
        if value['status'] == 401:
            self.auth.invalidate_access_token(self._token)
            raise AuthError('referee_unauthorized')
        if value['status'] == 429: raise AuthError('referee_rate_limited')
        if value['status'] != (200 if kind == 'negotiated' else 101):
            raise AuthError('referee_unavailable')
        return value

    def _open(self):
        from .provider_rooms import _bearer
        with self.auth._mutex:
            self._boundary()
            self._token = _bearer(self.auth)
            try:
                value = self._stage('negotiated')
                negotiation(value['data'])
                self._stage('upgraded', {'kind':'upgrade'})
                deadline = time.monotonic()+5
                self._session.send({'kind':'handshake'}, deadline)
                if self._session.receive(deadline, self._stop) != {'kind':'ready'}:
                    raise AuthError('referee_handshake_refused')
                self._boundary()
            finally: self._token = None
            with self._lock:
                self._connect_generation += 1
                self._roster_pending = {key: item for key, item in self._roster_pending.items()
                                        if item.generation == self._connect_generation}
                self._connected.set()

    def _event(self, name, payload, generation):
        # Never acquire SharedReferee.lock: a broker can hold it while waiting
        # for a completion that this receiver must be able to deliver.
        encoded(payload, 32768)
        if self.events.qsize() >= 128: raise AuthError('referee_event_overflow')
        with self._lock:
            self._event_sequence += 1
            item = _ProviderEvent(name, payload, generation, self._event_sequence)
            if name in _ROSTER_EVENTS:
                self._roster_revision += 1
                self._roster_pending[item.sequence] = item
        try: self.events.put_nowait(item)
        except queue.Full: raise AuthError('referee_event_overflow') from None

    def roster_progress(self):
        with self._lock:
            return (self._connect_generation, self._roster_revision,
                    any(item.generation == self._connect_generation for item in self._roster_pending.values()),
                    self._event_sequence)

    def acknowledge_event(self, item):
        # Exact receipt identity, not a high-water ACK: another queue consumer
        # can finish a later event while an earlier popped event awaits routing.
        with self._lock:
            if self._roster_pending.get(item.sequence) is item:
                del self._roster_pending[item.sequence]

    def _message(self, value, generation):
        from bot.hub import EVENTS
        kind = value['type']
        if kind == 3:
            with self._lock:
                result = self._pending.pop(value['invocationId'], None)
            if result is not None: result.put_nowait(value)
        elif kind == 1:
            args = value['arguments']
            if value['target'] not in EVENTS:
                # Native signalrcore ignores unsolicited methods without an
                # EVENTS subscriber. Revalidate the wire envelope before doing
                # the same; never route unknown payloads or grant authority.
                from .referee_protocol import message
                message(value)
                return
            if len(args) != 1 or type(args[0]) is not dict:
                raise AuthError('referee_event_refused')
            self._event(value['target'], args[0], generation)
        elif kind == 7:
            if not value.get('allowReconnect', False): self._stop.set()
            raise AuthError('referee_closed')

    def _retire(self):
        # Child reaping must succeed before another connection can be opened.
        with self._lock:
            self._connected.clear()
            pending, self._pending = self._pending, {}
            session = self._session
        for result in pending.values(): result.put_nowait(None)
        if session is not None:
            try: session.close()
            except Exception:
                self._cleanup_failed = True
                self._stop.set()
                raise
            with self._lock:
                if self._session is session: self._session = None

    def _run(self):
        established = False
        while not self._stop.is_set():
            generation = self._connect_generation
            try:
                self._open()
                generation = self._connect_generation
                if established: self._event('HubReconnected', {'generation':generation}, generation)
                established = True
                self._attempt.set()
                while not self._stop.is_set():
                    value = self._session.receive(time.monotonic()+65, self._stop)
                    if value.get('kind') != 'message': raise AuthError('referee_reply_refused')
                    self._message(value['value'], generation)
            except Exception as error:
                self._failure = error if type(error) is AuthError else AuthError('referee_transport_uncertain')
                _log_failure(error, generation, self._session)
            finally:
                try: self._retire()
                except Exception:
                    self._failure = AuthError('referee_child_cleanup_uncertain');self._stop.set()
                    _log_failure(self._failure, generation, self._session)
                self._attempt.set()
            if not established: break
            try: self._event('HubDisconnected', {'generation':generation}, generation)
            except AuthError: self._stop.set()
            if self._stop.wait(.2): break

    def connect(self, timeout=30):
        from .provider_rooms import _bearer
        _timeout(timeout, 30)
        if self._cleanup_failed: raise AuthError('referee_child_cleanup_uncertain')
        with self.auth._mutex: _bearer(self.auth)
        with self._lock:
            if self._connected.is_set(): return
            if self._manager is None or not self._manager.is_alive():
                self._stop.clear();self._attempt.clear();self._failure=None
                self._manager = threading.Thread(target=self._run, name='referee-manager', daemon=True)
                self._manager.start()
        if not self._attempt.wait(timeout):
            self.stop();raise AuthError('referee_connect_deadline')
        if not self.is_connected(): raise self._failure or AuthError('referee_disconnected')

    def is_connected(self): return self._connected.is_set()

    def wait_connected(self, timeout=30):
        return self._connected.wait(_timeout(timeout, 30))

    def reconnect(self):
        from .provider_rooms import _bearer
        with self.auth._mutex: _bearer(self.auth)
        generation = self._connect_generation
        self._retire()
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            if self.is_connected() and self._connect_generation>generation:return
            if self._manager is None or not self._manager.is_alive():break
            self._stop.wait(.02)
        raise self._failure or AuthError('referee_connect_deadline')

    def stop(self):
        self._stop.set()
        self._retire()
        manager = self._manager
        if manager is not None and manager is not threading.current_thread():
            manager.join(timeout=35)
            if manager.is_alive(): raise AuthError('referee_manager_cleanup_uncertain')

    def invoke(self, method, args, timeout=15):
        from bot.hub import HubDisconnected, HubInvocationUncertain
        from rolling.broker import HUB_METHODS
        from .provider_rooms import _authority
        _timeout(timeout, 15)
        if (type(method) is not str or method not in HUB_METHODS | {'MakeRoom'}
                or type(args) is not list or len(args)>64): raise AuthError('referee_method_refused')
        encoded({'type':1,'target':method,'arguments':args,'invocationId':'9'*19}, MAX_COMMAND)
        with self.auth._mutex:
            _authority(self.auth);self._boundary()
            with self._lock:
                if not self.is_connected() or self._session is None: raise HubDisconnected('referee_disconnected')
                self._next_id += 1
                if self._next_id >= 10**19: raise AuthError('referee_invocation_exhausted')
                identity = str(self._next_id)
                generation = self._connect_generation
                session = self._session
                result = queue.Queue(maxsize=1)
                self._pending[identity] = result
            deadline = time.monotonic()+timeout
            try:
                session.send({'kind':'invoke','value':{'type':1,'target':method,'arguments':args,
                                                     'invocationId':identity}}, deadline)
                value = result.get(timeout=max(0, deadline-time.monotonic()))
                if value is None or generation != self._connect_generation:
                    raise HubInvocationUncertain('referee_invocation_uncertain')
            except Exception:
                self._retire()
                raise HubInvocationUncertain('referee_invocation_uncertain') from None
            if 'error' in value: raise RuntimeError('referee_command_rejected')
            return value.get('result')
