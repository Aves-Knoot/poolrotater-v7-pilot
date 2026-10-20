"""Authenticated wire room adapters for fixed preparation or invented services.

Fixed-provider authority refuses before transport construction or worker startup.
The invented service supplies payloads behind real TLS/JSON SignalR connections;
room evidence uses qualified HTTPS and the same persistent admission authority.
"""
from contextlib import contextmanager
import copy
import logging
import os
import time

from .config import ConfigError
from .oauth import AuthError
from .provider_authorization import ProviderOAuth
from .provider_contract import OWNED_PATH, ROOM_LIMIT, require_live_provider
from .provider_room_state import binding, enroll, require_namespace
from .reconciliation import _room, _journal, journal_path
from .layout import read_json

log = logging.getLogger(__name__)
OWNER_READ_ATTEMPTS = 3
OWNER_ADMISSION_BUDGET = 75.
TEMPORARY_OWNER_STATUSES = frozenset((404,408,429,500,502,503,504))
TEMPORARY_OWNER_ERRORS = frozenset(('provider_deadline_exceeded','provider_transport_uncertain'))


def _authority(auth):
    if type(auth) is not ProviderOAuth: raise AuthError('qualified_provider_authority_required')
    if auth.execution in ('provider-disabled', 'provider-pilot', 'provider-live-pilot'): require_live_provider(auth.execution)
    auth.require_authorization_available()
    if auth.execution == 'provider-fixture' and os.environ.get('POOLROTATER_PROVISIONING_SANDBOX') != '1':
        raise AuthError('installed_offline_fixture_required')


def _service(auth, backend):
    _authority(auth)
    if auth.execution in ('provider-disabled', 'provider-pilot', 'provider-live-pilot'):
        if backend is not None: raise AuthError('fixed_provider_backend_required')
        return
    from .testing.room_service import InventedRoomService
    if (type(backend) is not InventedRoomService
            or backend.provider.server.server_port != auth.transport.port):
        raise AuthError('qualified_room_fixture_required')


def _bearer(auth):
    _authority(auth)
    guard = getattr(auth, '_room_guard', auth._local_guard)
    if guard is not None: guard()
    return auth.access_token()


def _fetch(auth, path):
    """Read-only 401 refresh once; never replay a referee mutation."""
    def request(token):
        # OAuth's generic wrapper deliberately masks all transport failures.
        # For these fixed room GETs retain typed authority/cleanup failures so
        # verification never mistakes them for a retryable network timeout.
        try: result=auth.transport.request('GET',path,bearer=token)
        except AuthError: raise
        except Exception: raise AuthError('transport_reply_uncertain') from None
        if (type(result) is not tuple or len(result)!=2 or type(result[0]) is not int
                or not 100<=result[0]<=599):raise AuthError('invalid_provider_reply')
        return result
    token = _bearer(auth)
    code, value = request(token)
    if code == 401:
        auth.invalidate_access_token(token)
        code, value = request(_bearer(auth))
    return code, value


def _snapshot(value, user_id, room_id=None):
    from bot.hub import HubInvocationUncertain
    if (type(value) is not dict or type(value.get('room_id')) is not int
            or not 0 < value['room_id'] < 10**19
            or (room_id is not None and value['room_id'] != room_id)
            # RoomJoinedResponse identifies the caller in referees, not a
            # top-level user_id. Keep rejecting conflicting legacy evidence.
            or ('user_id' in value and (type(value['user_id']) is not int or value['user_id'] != user_id))
            or type(value.get('chat_channel_id')) is not int or value['chat_channel_id'] <= 0
            or type(value.get('players')) is not list
            or len(value['players']) > 1000
            or any(type(p) is not dict or type(p.get('user_id')) is not int for p in value['players'])
            or type(value.get('referees')) is not list
            or not any(type(p) is dict and p.get('user_id') == user_id for p in value['referees'])):
        raise HubInvocationUncertain('referee_snapshot_requires_review')
    return copy.deepcopy(value)


class ProviderRoomHub:
    offline_fixture = False
    requires_event_provenance = True
    creation_observer_supported = True
    split_creation_verification = True

    def __init__(self, auth, backend=None):
        _service(auth, backend)
        self.auth, self.backend = auth, backend
        self.offline_fixture = auth.execution == 'provider-fixture'
        from .provider_referee import ProviderRefereeClient
        self.client = ProviderRefereeClient(auth)
        self._shared = None

    @property
    def _connect_generation(self): return self.client._connect_generation

    @property
    def shared(self): return self._shared

    @shared.setter
    def shared(self, value):
        self._shared = value
        self.client.events = value.events

    def is_connected(self): return self.client.is_connected()
    def wait_connected(self, timeout=30): return self.client.wait_connected(timeout)
    def roster_progress(self): return self.client.roster_progress()
    def acknowledge_event(self, item): self.client.acknowledge_event(item)
    def stop(self): self.client.stop()

    def connect(self):
        self.client.connect()

    def reconnect(self):
        """Explicit fixture event; stale memberships must rejoin before mutation."""
        self.client.reconnect()

    def make_room(self, *args, _on_created=None, **kwargs):
        snapshot, generation = self.begin_make_room(*args, _on_created=_on_created, **kwargs)
        return self.finish_make_room(snapshot, generation)

    def refresh_ready(self):
        """No HTTP request and no mutation; keep the old room during cooldown."""
        from .provider_pacing import ProviderAdmissionDeferred
        self.client._boundary()
        try:
            return self.auth.transport.gate.read_delay() < min(.5, self.auth.transport.timeout / 4)
        except ProviderAdmissionDeferred:
            return False
        except AuthError as error:
            if error.code == 'provider_pacing_deferred': return False
            raise

    def begin_make_room(self, *args, _on_created=None, **kwargs):
        """One referee mutation and durable candidate capture under hub lock."""
        from bot.hub import HubInvocationUncertain
        if _on_created is not None and not callable(_on_created):
            raise AuthError('referee_creation_record_requires_review')
        generation = self._connect_generation
        value = self.client.make_room(*args, **kwargs)
        # This is candidate evidence only. Persist the returned ID before any
        # follow-up read, generation check or full snapshot rejection can lose
        # it. The callback cannot grant room membership or mutation authority.
        rid=value.get('room_id') if type(value) is dict else None
        if type(rid) is int and 0<rid<10**19:
            channel=value.get('chat_channel_id')
            channel=channel if type(channel) is int and 0<channel<10**19 else None
            log.info('referee creation reply received before owner verification: room_id=%d chat_channel_id=%s',rid,channel)
        if _on_created is not None and type(rid) is int and 0<rid<10**19:
            try:_on_created(rid,generation)
            except Exception:
                raise HubInvocationUncertain('referee_creation_record_requires_review') from None
        if generation != self._connect_generation:
            raise HubInvocationUncertain('referee_generation_changed')
        return _snapshot(value, self.auth._active), generation

    def finish_make_room(self, snapshot, generation, *, cancelled=None):
        """Independent read phase; RoomHub calls this outside its routing lock."""
        return self._checked_snapshot(snapshot, generation, wait_for_admission=True,
                                      cancelled=cancelled)

    def _checked_snapshot(self, value, generation, room_id=None, *,
                          wait_for_admission=False, cancelled=None):
        """Validate referee membership and independent API creator evidence.

        No mutation is replayed if either response is uncertain. A missing
        top-level creator in the referee contract never weakens ownership checks.
        """
        from bot.hub import HubInvocationUncertain
        snapshot = _snapshot(value, self.auth._active, room_id)
        from .provider_pacing import ProviderAdmissionDeferred
        from .service_diagnostics import failure_details
        deadline = time.monotonic() + OWNER_ADMISSION_BUDGET
        attempt = 0
        waiting = False
        while attempt < OWNER_READ_ATTEMPTS:
            retry=False; status=None
            try:
                if (generation!=self._connect_generation or not self.client.is_connected()
                        or cancelled is not None and cancelled()):
                    raise HubInvocationUncertain('referee_generation_changed')
                if wait_for_admission:
                    if time.monotonic() >= deadline:
                        raise AuthError('referee_room_owner_requires_review')
                    self.client._boundary()
                    try:
                        delay = self.auth.transport.gate.read_delay()
                    except ProviderAdmissionDeferred:
                        delay = self.auth.transport.timeout
                    if delay >= min(.5, self.auth.transport.timeout / 4):
                        if not waiting:
                            log.warning('room owner verification waiting for existing admission cooldown: room_id=%d', snapshot['room_id'])
                            waiting = True
                        time.sleep(min(.5, max(0., deadline-time.monotonic())))
                        continue
                attempt += 1
                code, detail = _fetch(self.auth, '/api/v2/rooms/' + str(snapshot['room_id']))
                status=code
                if code != 200:
                    retry=code in TEMPORARY_OWNER_STATUSES
                    raise AuthError('ownership_room_evidence_unavailable',http_status=code)
                evidence = _room(detail, self.auth._active, snapshot['room_id'])
                if not evidence['active'] or evidence['ended']:
                    retry=not evidence['active'] and not evidence['ended']
                    raise AuthError('ownership_room_evidence_refused')
            except HubInvocationUncertain:raise
            except Exception as error:
                if status is None and type(getattr(error,'http_status',None)) is int:
                    status=error.http_status
                if isinstance(error,ProviderAdmissionDeferred):retry=True
                elif isinstance(error,AuthError) and error.code in TEMPORARY_OWNER_ERRORS:retry=True
                diagnostic=failure_details(error)
                log.warning('room owner verification deferred/refused: room_id=%d attempt=%d code=%s http_status=%s',
                    snapshot['room_id'],attempt,diagnostic['failure_code'],status)
                if retry and attempt<OWNER_READ_ATTEMPTS and time.monotonic()<deadline:
                    time.sleep(.25*max(1,attempt))
                    continue
                # Only our stable code and numeric status cross worker IPC.
                # Never attach a provider body, snapshot, credential or raw error.
                raise HubInvocationUncertain(str(AuthError('referee_room_owner_requires_review',
                    http_status=status))) from None
            break
        if generation != self._connect_generation or not self.client.is_connected():
            raise HubInvocationUncertain('referee_generation_changed')
        return snapshot

    def invoke(self, method, args, **kwargs):
        from bot.hub import HubInvocationUncertain
        from rolling.broker import HUB_METHODS
        if method not in HUB_METHODS: raise AuthError('referee_method_refused')
        generation = self._connect_generation
        value = self.client.invoke(method, args, **kwargs)
        if generation != self._connect_generation:
            raise HubInvocationUncertain('referee_generation_changed')
        return self._checked_snapshot(value, generation, int(args[0])) if method == 'JoinRoom' else value


class ProviderRoomApi:
    offline_fixture = False
    chat_access_guard = True

    def create_chat_websocket(self, configs):
        from .provider_chat import create
        return create(self.auth, configs)

    def __init__(self, auth, backend=None):
        _service(auth, backend)
        self.auth, self.backend = auth, backend
        self.offline_fixture = auth.execution == 'provider-fixture'
        from .provider_api import ProviderApiClient
        self.client = ProviderApiClient(auth)
        self.cancel_pending = self.client.cancel_pending

    def room(self, room_id):
        if type(room_id) is not int or not 0 < room_id < 10**19:
            raise AuthError('ownership_room_evidence_refused')
        code, value = _fetch(self.auth, '/api/v2/rooms/' + str(room_id))
        if code != 200: raise AuthError('ownership_room_evidence_unavailable')
        _room(value, self.auth._active, room_id)
        return value

    def room_probe(self, room_id):
        # A busy shared authority supplies no liveness evidence. Return the
        # existing conservative unavailable result promptly so routine idle /
        # pre-start probes cannot starve native control/status heartbeats.
        # Direct room reads, ownership checks and mutations keep their guards.
        if not self.auth._mutex.acquire(blocking=False): return 'unavailable', None
        try:
            value = self.room(room_id)
            if _room(value, self.auth._active, room_id)['ended']: return 'missing', None
            return 'ok', value
        except AuthError:
            # 404/omission/error are never positive evidence of absence.
            return 'unavailable', None
        finally:
            self.auth._mutex.release()

    def preflight_start(self, user_id):
        self._startup_owned_observation=None
        code, values = _fetch(self.auth, OWNED_PATH)
        if code != 200: raise AuthError('ownership_listing_unavailable')
        if type(values) is not list or len(values) > ROOM_LIMIT:
            raise AuthError('ownership_listing_refused')
        rows = [_room(value, user_id) for value in values]
        if len({row['room_id'] for row in rows}) != len(rows):
            raise AuthError('ownership_listing_refused')
        if len(rows) == ROOM_LIMIT or any(not row['ended'] for row in rows):
            raise AuthError('provider_existing_rooms_require_review')
        # A bounded listing is a conservative observed-room check, not proof of
        # account-wide absence. Lost local ownership is independently fenced.
        self._startup_owned_observation={'active_owned_room_ids':[], 'observed_ended_room_ids':[r['room_id'] for r in rows],
                'listing_at_limit':False, 'account_wide_absence_proved':False}

    def __getattr__(self, name):
        from rolling.adapters import Api
        if name not in Api.METHODS: raise AttributeError(name)
        def invoke(*args, **kwargs):
            return self.client.call(name, *args, **kwargs)
        return invoke


@contextmanager
def provider_host_controller(root, release, *, authentication, api, hub):
    """Installed provider authority with actual native synthetic worker processes."""
    auth = authentication
    _authority(auth)
    from .installed_authorization import _runtime_guard
    prepared = auth.execution in ('provider-disabled', 'provider-pilot', 'provider-live-pilot')
    runtime_guard = _runtime_guard(root, release, prepared=prepared)
    if (auth.root != root or type(api) is not ProviderRoomApi or type(hub) is not ProviderRoomHub
            or api.auth is not auth or hub.auth is not auth or api.backend is not hub.backend):
        raise AuthError('provider_controller_binding_required')
    _service(auth, hub.backend)

    def before_start(account):
        runtime_guard()
        expected = binding(root, auth.binding, account.user_id)
        if require_namespace(root, expected):
            journal = read_json(journal_path(root))
            aliases = {room['alias'] for room in auth._current()['rooms']}
            _journal(journal, aliases)
            if any(state.get('room_id') or state.get('ownership_unknown')
                   or state.get('operation') in ('make_pending', 'close_pending')
                   for state in journal['rooms'].values()):
                raise ConfigError('provider room ownership requires stopped reconciliation')
        api.preflight_start(account.user_id)
        rooms=auth._current()['rooms']
        if any(r['preset']=='catch-fixed' for r in rooms):
            owned=api._startup_owned_observation
            if not isinstance(owned,dict):raise ConfigError('fresh Catch capacity observation required')
            # Fresh provider-owned listing plus clean local ownership precedes
            # every MakeRoom. The normal four-room installation budget stays.
            from .layout import atomic_json
            import time
            atomic_json(root/'state/catch-startup-admission.json',{'format':1,
                **auth.binding,'checked_at':time.time(),**owned,
                'configured_rooms':len(rooms),'local_room_budget':4,
                'configured_aliases':[r['alias'] for r in rooms],
                'native_catch_aliases':[r['alias'] for r in rooms if r['preset']=='catch-fixed'],
                'ownership_recovery_pending':False,'room_creation_dispatched':False})
        enroll(root, expected)

    from offline_host_controller import _host_controller
    with _host_controller(root, release, api=api, hub=hub, authentication=auth,
                          before_start=before_start, prepared=prepared) as supervisor:
        yield supervisor
