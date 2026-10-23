"""End-to-end private booking flow using only local HTTP and fake provider IO."""
import copy
import http.client
import json
import threading
import uuid
from urllib.parse import parse_qs, urlsplit

from poolrotater_host.bookings.agent import Agent
from poolrotater_host.bookings.native import Coordinator, operation_id
from poolrotater_host.bookings.hub import Repository as HubBookingRepository
from poolrotater_host.bookings.server import PortalServer
from poolrotater_host.bookings.web import COOKIE, OAUTH_COOKIE, Portal
from poolrotater_host.diagnostic_logs.common import transaction
from poolrotater_host.layout import atomic_json
from poolrotater_host.managed_host.engine import Engine
from poolrotater_host.managed_host.hub import Repository as ManagedRepository
from poolrotater_host.managed_host.state import State
from poolrotater_host.network.contracts import NetworkError, canonical, secret
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.server import HubServer, tls
from poolrotater_host.network.store import Store
from poolrotater_host.network.transport import Transport
from poolrotater_host.provider_transport import TrustSnapshot
from booking_tls_fixture import certificates


INSTALLATION = str(uuid.UUID(int=602))
GENERATION = str(uuid.UUID(int=603))
OWNER_ID = 707


class Clock:
    def __init__(self, value=1000):
        self.value = value

    def __call__(self):
        return self.value


class OAuth:
    def authorization_url(self, state):
        return 'https://osu.ppy.sh/oauth/authorize?state=' + state

    def exchange(self, code):
        users = {'owner-code': (OWNER_ID, 'Invented Owner'),
                 'friend-code': (808, 'Invented Friend')}
        user_id, username = users[code]
        return {'id': user_id, 'username': username}


class Process:
    def poll(self):
        return None


class HubRoster:
    def __init__(self):
        self.owned = set()

    def roster_progress(self):
        return (1, 1, False)


class Broker:
    def __init__(self):
        self.generation = 'fake-worker-generation'
        self.hub = HubRoster()
        self.players = {999}
        self.known = True
        self.uncertain = False


class Peer:
    def __init__(self):
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append(args)
        return None


class Worker:
    def __init__(self, booking_id, session_generation, owner_id=OWNER_ID):
        self.process = Process()
        self._fresh = True
        self.status = {
            'ready': True,
            'room_id': 9876,
            'booking': {
                'id': booking_id,
                'session_generation': session_generation,
                'match': None,
                'roster_known': True,
                'players': [{'user_id': owner_id, 'username': 'Invented Owner'}],
            },
        }
        self.broker = Broker()
        self.broker.hub.owned.add(9876)
        self.broker.players = {owner_id, 999}
        self.peer = Peer()

    def fresh(self):
        return self._fresh


class HostClient:
    def __init__(self, root, binding, token, transport, managed_state):
        self.root = root
        self.binding = copy.deepcopy(binding)
        self.token = token
        self._transport = transport
        self.managed_state = managed_state
        self.supported_presets = ['standard-fixed']

    def status(self):
        return {'configured': True, 'enrolled': True,
                'account_binding_matches': True, 'pending_operation': None}

    def profile(self):
        return {'origin': 'https://127.0.0.1'}

    def credential(self):
        return {**self.binding, 'credential': self.token,
                'credential_expires_at': 10**9}

    def pending(self, profile):
        return None

    def transport(self, profile):
        return self._transport


class LoseOneReply:
    def __init__(self, transport):
        self.transport = transport
        self.lose_next = False
        self.calls = []
        self.responses = []
        self.errors = []

    def request(self, method, path, *, body, token):
        self.calls.append(copy.deepcopy(body))
        try:
            result = self.transport.request(method, path, body=body, token=token)
        except Exception as error:
            self.errors.append((type(error).__name__, getattr(error, 'code', None),
                                getattr(error, 'status', None)))
            raise
        self.responses.append(copy.deepcopy(result))
        if self.lose_next:
            self.lose_next = False
            raise NetworkError('hub_transport_failed', 503)
        return result


class Registry:
    def __init__(self):
        self.rooms = {}

    def snapshot(self):
        return {'rooms': copy.deepcopy(self.rooms)}


class Owner:
    def __init__(self, root, state):
        self.runtime = str(root)
        self.managed = type('Managed', (), {'state': state})()
        self.registry = Registry()
        self.workers = {}
        self.configs = {'room1': {'_authenticated_user_id': 999}}


class FakeProvider:
    """Apply managed Engine operations to State without upstream room IO."""
    def __init__(self, state, owner):
        self.state = state
        self.owner = owner
        self.worker_generation = 'a' * 32
        self.fail_close = False

    def preflight(self, operation):
        return None

    def ready(self, operation):
        return True

    def apply(self, operation):
        alias = operation['alias']
        if operation['action'] == 'configure':
            return {'application': 'recreate'}
        if operation['action'] == 'open':
            self.state.reserve(alias, self.worker_generation, operation=operation['id'])
            self.state.mark_dispatched(alias, self.worker_generation)
            self.state.observe_owned(alias, self.worker_generation, 9876)
            self.owner.registry.rooms[alias] = {
                'ownership_unknown': False, 'operation': 'owned',
                'room_id': 9876, 'generation': self.worker_generation,
            }
            return {'room_id': 9876, 'application': 'recreate'}
        if operation['action'] == 'close':
            if self.fail_close:
                raise RuntimeError('simulated close uncertainty')
            room = self.state.room(alias, private=True)
            self.state.confirmed_closed(alias, self.worker_generation, room['room_id'])
            self.owner.registry.rooms[alias] = {
                'ownership_unknown': False, 'operation': 'closed',
                'room_id': None, 'generation': self.worker_generation,
            }
            self.owner.workers.pop(alias, None)
            return {'room_id': 9876, 'application': 'lifecycle'}
        raise AssertionError(operation['action'])


def _http(port, method, path, *, headers=None, body=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    raw = None if body is None else json.dumps(body, separators=(',', ':')).encode()
    values = dict(headers or {})
    if raw is not None:
        values.setdefault('Content-Type', 'application/json')
    conn.request(method, path, body=raw, headers=values)
    response = conn.getresponse()
    response_headers = {}
    for key, value in response.getheaders():
        if key.casefold() == 'set-cookie':
            response_headers.setdefault('Set-Cookie', []).append(value)
        else:
            response_headers[key] = value
    result = response.status, response_headers, response.read()
    conn.close()
    return result


def _login(port, code='owner-code'):
    status, headers, _ = _http(port, 'GET', '/private-lobbies/oauth/start')
    assert status == 302
    state = parse_qs(urlsplit(headers['Location']).query)['state'][0]
    browser_cookie = headers['Set-Cookie'][0].split(';', 1)[0]
    assert browser_cookie.startswith(OAUTH_COOKIE + '=')
    status, headers, _ = _http(
        port, 'GET', '/private-lobbies/oauth/callback?state=' + state + '&code=' + code,
        headers={'Cookie': browser_cookie})
    assert status == 303
    cookie = next(value.split(';', 1)[0] for value in headers['Set-Cookie']
                  if value.startswith(COOKIE + '='))
    assert cookie.startswith(COOKIE + '=')
    return cookie


def _post(port, cookie, csrf, path, body):
    return _http(port, 'POST', path, headers={
        'Cookie': cookie, 'Origin': 'https://poolrotater.com',
        'X-CSRF-Token': csrf,
    }, body=body)


def test_portal_authenticated_hub_exchange_and_native_engine_lifecycle(tmp_path):
    clock = Clock()
    hub_root = tmp_path / 'hub'
    hub_root.mkdir(mode=0o700)
    cert_root = tmp_path / 'tls'
    cert_root.mkdir(mode=0o700)
    certificates(cert_root)
    initialize(hub_root)
    hub_store = Store(hub_root, clock=clock)
    operator = {'operator_id': str(uuid.UUID(int=601)), 'account_id': 606,
                'installation_id': INSTALLATION, 'generation': GENERATION,
                'directory': True}
    token = secret()
    invitation = hub_store.invite(operator, label='Invented integration host')['invitation']
    enrolled = hub_store.enroll({'protocol_version': 1,
                                 'request_id': str(uuid.UUID(int=607)),
                                 'invitation': invitation, 'credential': token, **operator})
    binding = {'host_id': enrolled['host_id'], 'installation_id': INSTALLATION,
               'generation': GENERATION}
    hub_server = HubServer(('127.0.0.1', 0), hub_store,
                           tls_context=tls(cert_root / 'server.crt', cert_root / 'server.key'))
    hub_thread = threading.Thread(target=hub_server.serve_forever,
                                  kwargs={'poll_interval': .02}, daemon=True)
    hub_thread.start()
    transport = Transport(f'https://127.0.0.1:{hub_server.server_port}',
                          TrustSnapshot((cert_root / 'ca.crt').read_bytes()), timeout=4)
    repo = HubBookingRepository(hub_store).ledger
    hub_settings = {'sr_min': 3.5, 'sr_max': 4.5}
    native_settings = {
        'preset': 'standard-fixed', 'visibility': 'private', 'password': 'operator-pass',
        'sr_min': 3.5, 'sr_max': 4.5, 'auto_difficulty': False,
        'countdown_seconds': 20, 'loved_mode': False, 'stream_enabled': False,
    }
    host_root = tmp_path / 'host'
    host_root.mkdir(mode=0o700)
    (host_root / 'state').mkdir(mode=0o700)
    (host_root / 'config').mkdir(mode=0o700)
    state = State(host_root, **binding, clock=clock)
    state.seed([{'alias': 'room1', 'desired': 'stopped', 'settings': native_settings}])
    state.policy_applied(1)
    owner = Owner(host_root, state)
    owner.registry.rooms['room1'] = {
        'ownership_unknown': False, 'operation': 'closed',
        'room_id': None, 'generation': None,
    }
    provider = FakeProvider(state, owner)
    engine = Engine(state, provider)
    atomic_json(host_root / 'config/private-bookings.json',
                {'format': 1, 'enabled': True, 'aliases': ['room1']})

    # Seed only the managed-room admission proof. Booking capabilities and all
    # booking observations below travel through the authenticated TLS route.
    managed_repo = ManagedRepository(hub_store)
    from poolrotater_host.managed_host.contracts import digest

    def refresh_managed_admission_report(sequence):
        managed_caps = state.snapshot(controller_phase='running')
        from poolrotater_host.managed_host.contracts import capabilities
        capabilities(managed_caps)
        managed_payload = {'protocol_version': 1, 'installation_id': INSTALLATION,
                           'generation': GENERATION, 'sequence': sequence, 'sent_at': clock(),
                           'capabilities': managed_caps, 'receipts': []}
        response = {'ok': True, 'sequence': sequence, 'requests': []}
        with transaction(managed_repo.path) as db:
            db.execute('INSERT OR REPLACE INTO reports VALUES(?,?,?,?,?,?)', (
                binding['host_id'], sequence, digest(managed_payload),
                canonical(managed_caps).decode(), clock(), canonical(response).decode()))

    refresh_managed_admission_report(1)

    portal = Portal(hub_root, repo, origin='https://poolrotater.com', oauth=OAuth(),
                    enabled=True, clock=clock)
    portal_server = PortalServer(('127.0.0.1', 0), portal, fixture=True)
    portal_thread = threading.Thread(target=portal_server.serve_forever,
                                     kwargs={'poll_interval': .02}, daemon=True)
    portal_thread.start()
    try:
        client = HostClient(host_root, binding, token, transport, state)
        flaky = LoseOneReply(transport)
        client._transport = flaky
        from poolrotater_host.bookings.local import LocalSessions
        host_ledger = LocalSessions(host_root, binding, clock=clock)
        host_agent = Agent(client, store=host_ledger, clock=clock)
        assert host_agent.step()['phase'] == 'connected'
        coordinator = Coordinator(owner, clock=clock)
        repo.configure_slot('west', binding['host_id'], 'room1', 'West Pool',
                            ['standard-fixed'], hub_settings, enabled=True)

        port = portal_server.server_port
        owner_cookie = _login(port, 'owner-code')
        friend_cookie = _login(port, 'friend-code')
        owner_me = json.loads(_http(port, 'GET', '/private-lobbies/api/me',
                                    headers={'Cookie': owner_cookie})[2])
        friend_me = json.loads(_http(port, 'GET', '/private-lobbies/api/me',
                                     headers={'Cookie': friend_cookie})[2])
        from concurrent.futures import ThreadPoolExecutor
        attempts = [
            (owner_cookie, owner_me['csrf_token'], OWNER_ID, str(uuid.UUID(int=604))),
            (friend_cookie, friend_me['csrf_token'], 808, str(uuid.UUID(int=605))),
        ]
        def reserve(attempt):
            cookie, csrf, _, request_id = attempt
            return attempt, _post(port, cookie, csrf, '/private-lobbies/api/reserve', {
                'request_id': request_id, 'slot_id': 'west',
                'preset': 'standard-fixed', 'settings': {}, 'designated_friend_id': None,
            })
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, attempts))
        created = [(attempt, result) for attempt, result in outcomes if result[0] == 201]
        refused = [(attempt, result) for attempt, result in outcomes if result[0] == 409]
        assert len(created) == len(refused) == 1, [
            (result[0], result[2]) for _, result in outcomes]
        winning, result = created[0]
        owner_cookie, owner_csrf, owner_id, _ = winning
        friend_cookie, friend_csrf, friend_id, _ = next(
            attempt for attempt, _ in outcomes if attempt is not winning)
        booking = json.loads(result[2])['booking']
        booking_id = booking['id']
        assert repo.public_slots()[0]['state'] == 'preparing'

        # Hub accepts the exchange but the response is lost. A newly created
        # Agent retries its durable identical body and installs that one lease.
        flaky.lose_next = True
        assert host_agent.step()['phase'] == 'retrying'
        accepted_body = flaky.calls[-1]
        host_agent = Agent(client, store=host_ledger, clock=clock)
        assert host_agent.step()['phase'] == 'connected'
        assert flaky.calls[-1] == accepted_body
        assert flaky.responses[-1] == flaky.responses[-2]
        lease = host_ledger.get(booking_id)['lease']
        assert len(lease['session_generation']) == 32
        assert lease['user_id'] == owner_id

        coordinator._one(coordinator.sessions.get(booking_id))
        assert engine.step()['phase'] == 'applied'
        coordinator._one(coordinator.sessions.get(booking_id))
        assert engine.step()['phase'] == 'applied'
        worker = Worker(booking_id, lease['session_generation'], owner_id)
        owner.workers['room1'] = worker
        clock.value += 1
        coordinator.tick()
        assert host_agent.step()['phase'] == 'connected'
        assert coordinator.sessions.get(booking_id)['state']['phase'] == 'active'
        current = json.loads(_http(port, 'GET', '/private-lobbies/api/session?id=' + booking_id,
                                   headers={'Cookie': owner_cookie})[2])['booking']
        assert current['phase'] == 'active'

        # Process restart plus unknown native ownership cannot release a live slot.
        coordinator = Coordinator(owner, clock=clock)
        host_agent = Agent(client, store=host_ledger, clock=clock)
        owner.registry.rooms['room1']['ownership_unknown'] = True
        clock.value += 1
        coordinator.tick()
        assert host_agent.step()['phase'] == 'connected'
        assert coordinator.sessions.get(booking_id)['state']['phase'] == 'active'
        assert state.limits()['capacity_used'] == 1
        assert repo.public_slots()[0]['state'] == 'active'
        owner.registry.rooms['room1']['ownership_unknown'] = False

        # Verified roster actions exercise owner grant, controller settings and
        # the hub's owner-only promotion rule across the host command exchange.
        worker.status['booking']['players'] = [
            {'user_id': owner_id, 'username': 'Invented Owner'},
            {'user_id': friend_id, 'username': 'Invented Friend'},
        ]
        worker.broker.players = {owner_id, friend_id, 999}
        clock.value += 1
        coordinator.tick()
        assert host_agent.step()['phase'] == 'connected'
        current = json.loads(_http(port, 'GET', '/private-lobbies/api/session?id=' + booking_id,
                                   headers={'Cookie': owner_cookie})[2])['booking']
        status, _, body = _post(port, owner_cookie, owner_csrf, '/private-lobbies/api/action', {
            'request_id': str(uuid.UUID(int=608)), 'booking_id': booking_id,
            'expected_revision': current['revision'], 'action': 'grant',
            'payload': {'user_id': friend_id},
        })
        grant_id = json.loads(body)['operation']['id']
        assert status == 202
        assert host_agent.step()['phase'] == 'connected'
        pending, = coordinator.sessions.pending(booking_id)
        assert pending['operation_id'] == grant_id
        coordinator._one(coordinator.sessions.get(booking_id))
        assert coordinator.sessions.get(booking_id)['roles'][friend_id] == 'controller'
        assert host_agent.step()['phase'] == 'connected'
        current = json.loads(_http(port, 'GET', '/private-lobbies/api/session?id=' + booking_id,
                                   headers={'Cookie': friend_cookie})[2])['booking']
        assert current['role'] == 'controller'
        status, _, body = _post(port, friend_cookie, friend_csrf, '/private-lobbies/api/action', {
            'request_id': str(uuid.UUID(int=609)), 'booking_id': booking_id,
            'expected_revision': current['revision'], 'action': 'configure',
            'payload': {'sr_min': 4.0},
        })
        configure_id = json.loads(body)['operation']['id']
        assert status == 202
        status, _, body = _post(port, friend_cookie, friend_csrf, '/private-lobbies/api/action', {
            'request_id': str(uuid.UUID(int=610)), 'booking_id': booking_id,
            'expected_revision': current['revision'] + 1, 'action': 'grant',
            'payload': {'user_id': owner_id},
        })
        assert status == 403 and json.loads(body)['error'] == 'owner_required'
        assert host_agent.step()['phase'] == 'connected'
        pending, = coordinator.sessions.pending(booking_id)
        assert pending['operation_id'] == configure_id
        coordinator._one(coordinator.sessions.get(booking_id))
        assert worker.peer.calls[-1] == ('booking-control', configure_id)
        effective = {**coordinator.sessions.get(booking_id)['settings'], 'sr_min': 4.0}
        coordinator.sessions.acknowledge(configure_id, 'applied',
                                         application={'application': 'live'},
                                         effective_settings=effective)
        assert host_agent.step()['phase'] == 'connected'
        current = json.loads(_http(port, 'GET', '/private-lobbies/api/session?id=' + booking_id,
                                   headers={'Cookie': owner_cookie})[2])['booking']
        assert current['settings']['sr_min'] == 4.0

        # A long map plus a playing friend keeps the session active after the
        # owner leaves. Once the map completes, 1,800 seconds of emptiness closes.
        started = clock()
        match = {'id': 'integration-long-map', 'started_at': started,
                 'expected_end': started + 3600, 'ended_at': None,
                 'completed': False, 'aborted': False}
        worker.status['booking']['players'] = [{'user_id': friend_id,
                                                'username': 'Invented Friend'}]
        worker.broker.players = {friend_id, 999}
        worker.status['booking']['match'] = match
        clock.value += 1900  # Beyond the ordinary 1,800-second inactivity deadline.
        coordinator.tick()
        assert host_agent.step()['phase'] == 'connected'
        assert coordinator.sessions.get(booking_id)['state']['phase'] == 'active'
        completed = {**match, 'ended_at': clock(), 'expected_end': clock(), 'completed': True}
        worker.status['booking']['match'] = completed
        worker.status['booking']['players'] = []
        worker.broker.players = {999}
        clock.value += 1
        coordinator.tick()
        assert host_agent.step()['phase'] == 'connected'
        clock.value += 1800
        coordinator.tick()
        assert host_agent.step()['phase'] == 'connected'
        assert coordinator.sessions.get(booking_id)['state']['phase'] == 'closing'
        assert state.limits()['capacity_used'] == 1
        assert engine.step()['phase'] == 'applied'

        # Confirmed upstream closure and exact restoration reach the hub before
        # its slot is reusable. The next owner gets a different session binding.
        clock.value += 1
        coordinator.tick()
        assert host_agent.step()['phase'] == 'connected'
        restore_id = operation_id(booking_id, 'restore')
        assert state.operation(restore_id)['phase'] == 'received'
        assert engine.step()['phase'] == 'applied'
        clock.value += 1
        coordinator.tick()
        assert coordinator.sessions.get(booking_id)['cleanup_complete'] is True
        assert state.limits()['capacity_used'] == 0
        assert state.room('room1', private=True)['config'] == native_settings
        assert host_agent.step()['phase'] == 'connected'
        refresh_managed_admission_report(2)
        assert repo.public_slots()[0]['state'] == 'available', repo.public_slots()[0]
        status, _, raw = _post(port, friend_cookie, friend_csrf, '/private-lobbies/api/reserve', {
            'request_id': str(uuid.UUID(int=611)), 'slot_id': 'west',
            'preset': 'standard-fixed', 'settings': {}, 'designated_friend_id': None,
        })
        second = json.loads(raw)['booking']
        assert status == 201 and second['id'] != booking_id
        second_exchange = host_agent.step()
        assert second_exchange['phase'] == 'connected', (second_exchange, flaky.errors[-1:])
        next_lease = host_ledger.get(second['id'])['lease']
        assert next_lease['alias'] == lease['alias']
        assert next_lease['session_generation'] != lease['session_generation']
    finally:
        portal_server.shutdown()
        portal_server.server_close()
        portal_thread.join(3)
        hub_server.shutdown()
        hub_server.server_close()
        hub_thread.join(3)
