"""Authenticated wire room adapters for fixed preparation or invented services.

Fixed-provider authority refuses before transport construction or worker startup.
The invented service supplies payloads behind real TLS/JSON SignalR connections;
room evidence uses qualified HTTPS and the same persistent admission authority.
"""
from contextlib import contextmanager
import copy
import os

from .config import ConfigError
from .oauth import AuthError
from .provider_authorization import ProviderOAuth
from .provider_contract import OWNED_PATH, ROOM_LIMIT, require_live_provider
from .provider_room_state import binding, enroll, require_namespace
from .reconciliation import _room, _journal, journal_path
from .layout import read_json


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
    token = _bearer(auth)
    code, value = auth._request('GET', path, token=token)
    if code == 401:
        auth.invalidate_access_token(token)
        code, value = auth._request('GET', path, token=_bearer(auth))
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

    def make_room(self, *args, **kwargs):
        from bot.hub import HubInvocationUncertain
        generation = self._connect_generation
        value = self.client.make_room(*args, **kwargs)
        if generation != self._connect_generation:
            raise HubInvocationUncertain('referee_generation_changed')
        return self._checked_snapshot(value, generation)

    def _checked_snapshot(self, value, generation, room_id=None):
        """Validate referee membership and independent API creator evidence.

        No mutation is replayed if either response is uncertain. A missing
        top-level creator in the referee contract never weakens ownership checks.
        """
        from bot.hub import HubInvocationUncertain
        snapshot = _snapshot(value, self.auth._active, room_id)
        try:
            code, detail = _fetch(self.auth, '/api/v2/rooms/' + str(snapshot['room_id']))
            if code != 200: raise AuthError('ownership_room_evidence_unavailable')
            evidence = _room(detail, self.auth._active, snapshot['room_id'])
            if not evidence['active'] or evidence['ended']:
                raise AuthError('ownership_room_evidence_refused')
        except Exception:
            raise HubInvocationUncertain('referee_room_owner_requires_review') from None
        if generation != self._connect_generation:
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
        try:
            value = self.room(room_id)
            if _room(value, self.auth._active, room_id)['ended']: return 'missing', None
            return 'ok', value
        except AuthError:
            # 404/omission/error are never positive evidence of absence.
            return 'unavailable', None

    def preflight_start(self, user_id):
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
        enroll(root, expected)

    from offline_host_controller import _host_controller
    with _host_controller(root, release, api=api, hub=hub, authentication=auth,
                          before_start=before_start, prepared=prepared) as supervisor:
        yield supervisor
