"""Isolated tests for the host booking exchange agent."""
import copy
import uuid

import pytest

from poolrotater_host.bookings.agent import Agent, diagnostics, start
from poolrotater_host.bookings.local import LocalSessions
from poolrotater_host.layout import atomic_json, file_lease, read_json
from poolrotater_host.network.contracts import NetworkError


HOST = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
INSTALLATION = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
GENERATION = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
OWNER = 101
BASE = {'sr_min': 3.5, 'sr_max': 4.5, 'countdown_seconds': 30}


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value


class ManagedState:
    def __init__(self, *, phase='stopped', desired='stopped', room_id=None,
                 visibility='private', pending=None, failure=None):
        self.binding = {'host_id': HOST, 'installation_id': INSTALLATION,
                        'generation': GENERATION}
        self.current = {'phase': phase, 'desired': desired, 'room_id': room_id,
                        'pending': pending, 'failure': failure,
                        'config': {**BASE, 'visibility': visibility, 'preset': 'standard-fixed'}}
        self.reads = []

    def room(self, alias, *, private=False):
        self.reads.append((alias, private))
        return copy.deepcopy(self.current)


class Transport:
    def __init__(self, behavior=None):
        self.behavior = behavior
        self.bodies = []

    def request(self, method, path, *, body, token):
        assert method == 'POST'
        assert path == '/v1/hosts/self/bookings'
        assert token == 'host-secret'
        self.bodies.append(copy.deepcopy(body))
        if callable(self.behavior):
            return self.behavior(body)
        return {'ok': True, 'sequence': body['sequence'], 'leases': [], 'commands': []}


class Client:
    def __init__(self, root, managed, transport=None, *, configured=True, enrolled=True):
        self.root = root
        self.managed_state = managed
        self.supported_presets = ['standard-fixed']
        self._transport = transport or Transport()
        self._configured = configured
        self._enrolled = enrolled

    def status(self):
        return {'configured': self._configured, 'enrolled': self._enrolled,
                'account_binding_matches': True, 'pending_operation': None}

    def profile(self):
        return {'origin': 'https://hub.example.test'}

    def credential(self):
        return {'host_id': HOST, 'installation_id': INSTALLATION, 'generation': GENERATION,
                'credential': 'host-secret', 'credential_expires_at': 9999999}

    def pending(self, profile):
        return None

    def transport(self, profile):
        return self._transport


def setup_root(root, *, enabled=True):
    (root / 'state').mkdir(mode=0o700, exist_ok=True)
    (root / 'config').mkdir(mode=0o700, exist_ok=True)
    atomic_json(root / 'config/private-bookings.json',
                {'format': 1, 'enabled': enabled, 'aliases': ['room1'] if enabled else []})


def make_agent(root, *, managed=None, transport=None, clock=None, observation_provider=None):
    setup_root(root)
    clock = clock or Clock()
    managed = managed or ManagedState()
    client = Client(root, managed, transport)
    store = LocalSessions(root, managed.binding, clock=clock)
    agent = Agent(client, store=store, clock=clock, observation_provider=observation_provider)
    return agent, client, store, managed, clock


def lease(alias='room1'):
    booking_id = str(uuid.uuid4())
    return {
        'protocol_version': 1,
        'id': booking_id,
        'booking_id': booking_id,
        'user_id': OWNER,
        'owner_id': OWNER,
        'designated_friend_id': None,
        'host_id': HOST,
        'installation_id': INSTALLATION,
        'generation': GENERATION,
        'alias': alias,
        'session_generation': uuid.uuid4().hex,
        'preset': 'standard-fixed',
        'settings': copy.deepcopy(BASE),
        'password': 'prb_test_booking_password',
        'phase': 'preparing',
        'revision': 0,
    }


def test_disabled_policy_starts_no_thread_or_network_exchange(tmp_path):
    setup_root(tmp_path, enabled=False)
    client = Client(tmp_path, ManagedState())
    assert start(client, object()) is None


def test_malformed_policy_does_not_start_exchange_thread(tmp_path):
    (tmp_path / 'state').mkdir(mode=0o700, exist_ok=True)
    (tmp_path / 'config').mkdir(mode=0o700, exist_ok=True)
    atomic_json(tmp_path / 'config/private-bookings.json',
                {'format': 1, 'enabled': 'yes', 'aliases': ['room1']})
    assert start(Client(tmp_path, ManagedState()), object()) is None


def test_exchange_state_retries_identical_pending_body_and_hides_transport_errors(tmp_path):
    calls = []

    def behavior(body):
        calls.append(copy.deepcopy(body))
        if len(calls) == 1:
            raise NetworkError('hub_transport_failed', 503)
        return {'ok': True, 'sequence': body['sequence'], 'leases': [], 'commands': []}

    transport = Transport(behavior)
    agent, _, _, _, _ = make_agent(tmp_path, transport=transport)
    first = agent.step()
    assert first['phase'] == 'retrying'
    assert first['error'] == 'hub-unavailable'
    persisted = read_json(tmp_path / 'state/booking-agent.json')
    assert persisted['sequence'] == 1 and persisted['pending'] == calls[0]

    second = agent.step()
    assert second['phase'] == 'connected'
    assert calls[1] == calls[0]
    assert read_json(tmp_path / 'state/booking-agent.json') == {'sequence': 1, 'pending': None}
    assert 'hub_transport_failed' not in repr(diagnostics(tmp_path))


def test_explicit_stale_exchange_rejection_clears_only_that_body_then_uses_fresh_sequence(tmp_path):
    calls = []

    def behavior(body):
        calls.append(copy.deepcopy(body))
        if len(calls) == 1:
            raise NetworkError('booking_exchange_expired', 409)
        return {'ok': True, 'sequence': body['sequence'], 'leases': [], 'commands': []}

    transport = Transport(behavior)
    agent, _, _, _, clock = make_agent(tmp_path, transport=transport)
    first = agent.step()
    assert (first['phase'], first['retry_seconds']) == ('retrying', 5)
    saved = read_json(tmp_path / 'state/booking-agent.json')
    assert saved == {'sequence': 1, 'pending': None}

    clock.value += 301
    second = agent.step()
    assert second['phase'] == 'connected'
    assert calls[0]['sequence'] == 0
    assert calls[1]['sequence'] == 1
    assert calls[1]['sent_at'] > calls[0]['sent_at']
    assert read_json(tmp_path / 'state/booking-agent.json') == {'sequence': 2, 'pending': None}


def test_lease_installs_only_for_explicit_private_stopped_alias_without_room_mutation(tmp_path):
    booking = lease()
    transport = Transport(lambda body: {'ok': True, 'sequence': body['sequence'],
                                       'leases': [booking], 'commands': []})
    agent, _, store, managed, _ = make_agent(tmp_path, transport=transport)
    result = agent.step()
    assert result['phase'] == 'connected'
    installed = store.get(booking['id'])
    assert installed['lease']['password'] == booking['password']
    assert installed['lease']['alias'] == 'room1'
    assert managed.reads == [('room1', True)]
    assert managed.current['desired'] == 'stopped'
    assert managed.current['room_id'] is None


def test_already_installed_exact_lease_can_be_reported_for_private_running_room(tmp_path):
    booking = lease()
    managed = ManagedState(phase='running', desired='running', room_id=765)
    transport = Transport(lambda body: {'ok': True, 'sequence': body['sequence'],
                                       'leases': [booking], 'commands': []})
    agent, _, store, _, _ = make_agent(tmp_path, transport=transport, managed=managed)
    store.install(booking, BASE)
    result = agent.step()
    assert result['phase'] == 'connected'
    assert len(store.leases()) == 1
    assert managed.current['room_id'] == 765


def test_exact_lease_replay_reuses_persisted_base_after_local_settings_change(tmp_path):
    booking = lease()
    managed = ManagedState()
    transport = Transport(lambda body: {'ok': True, 'sequence': body['sequence'],
                                       'leases': [booking], 'commands': []})
    agent, _, store, _, _ = make_agent(tmp_path, transport=transport, managed=managed)
    store.install(booking, BASE)
    configure = {
        'protocol_version': 1, 'operation_id': str(uuid.uuid4()),
        'booking_id': booking['id'], 'host_id': HOST, 'installation_id': INSTALLATION,
        'generation': GENERATION, 'alias': booking['alias'],
        'session_generation': booking['session_generation'], 'sequence': 0,
        'user_id': OWNER, 'action': 'configure', 'payload': {'sr_min': 4.0},
    }
    receipt = store.receive(configure)
    store.acknowledge(receipt['operation_id'], 'applied', effective_settings={'sr_min': 4.0})
    changed = store.get(booking['id'])['settings']
    managed.current['config']['sr_min'] = 4.0

    assert agent.step()['phase'] == 'connected'
    assert store.get(booking['id'])['settings'] == changed
    assert store.get(booking['id'])['base_settings'] == BASE


def test_new_lease_uses_requested_preset_defaults_when_slot_preset_differs(tmp_path):
    booking = lease()
    booking['preset'] = 'mania-4k'
    managed = ManagedState()
    transport = Transport(lambda body: {'ok': True, 'sequence': body['sequence'],
                                       'leases': [booking], 'commands': []})
    agent, client, store, _, _ = make_agent(tmp_path, transport=transport, managed=managed)
    client.supported_presets = ['mania-4k']

    assert agent.step()['phase'] == 'connected'
    installed = store.get(booking['id'])
    assert installed['lease']['preset'] == 'mania-4k'
    assert installed['base_settings']['sr_min'] == 3.0
    assert installed['base_settings']['sr_max'] == 4.0


def test_new_lease_waits_for_update_gate_without_clearing_exchange_body(tmp_path):
    booking = lease()
    transport = Transport(lambda body: {'ok': True, 'sequence': body['sequence'],
                                       'leases': [booking], 'commands': []})
    agent, _, store, _, _ = make_agent(tmp_path, transport=transport)
    maintenance = tmp_path / 'state/rolling/supervisor-maintenance.json'
    maintenance.parent.mkdir(mode=0o700, exist_ok=True)
    atomic_json(maintenance, {'format': 1, 'phase': 'committing'})

    result = agent.step()
    saved = read_json(tmp_path / 'state/booking-agent.json')
    assert (result['phase'], result['error'], result['retry_seconds']) == (
        'retrying', 'policy-preparation-required', 5)
    assert saved['pending'] == transport.bodies[0]
    assert store.leases(include_ended=True) == []

    maintenance.unlink()
    assert agent.step()['phase'] == 'connected'
    assert transport.bodies[1] == transport.bodies[0]
    assert store.get(booking['id'])['lease']['id'] == booking['id']


def test_exact_lease_replay_during_update_gate_performs_no_install_mutation(tmp_path):
    booking = lease()
    transport = Transport(lambda body: {'ok': True, 'sequence': body['sequence'],
                                       'leases': [booking], 'commands': []})
    agent, _, store, _, _ = make_agent(tmp_path, transport=transport)
    store.install(booking, BASE)
    store.install = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('replayed install'))
    maintenance = tmp_path / 'state/rolling/supervisor-maintenance.json'
    maintenance.parent.mkdir(mode=0o700, exist_ok=True)
    atomic_json(maintenance, {'format': 1, 'phase': 'committing'})

    assert agent.step()['phase'] == 'connected'
    assert len(transport.bodies) == 1


def test_shared_admission_lock_defers_only_new_lease_install(tmp_path):
    booking = lease()
    transport = Transport(lambda body: {'ok': True, 'sequence': body['sequence'],
                                       'leases': [booking], 'commands': []})
    agent, _, store, _, _ = make_agent(tmp_path, transport=transport)
    lock_path = tmp_path / 'state/booking-admission.lock'
    with file_lease(lock_path):
        result = agent.step()
        pending = read_json(tmp_path / 'state/booking-agent.json')
        assert (result['phase'], result['retry_seconds']) == ('retrying', 5)
        assert pending['pending'] == transport.bodies[0]
        assert store.leases(include_ended=True) == []
    assert agent.step()['phase'] == 'connected'
    assert transport.bodies[1] == transport.bodies[0]


@pytest.mark.parametrize('managed', [
    ManagedState(phase='running', desired='running', room_id=765),
    ManagedState(phase='waiting_boundary', desired='stopped'),
    ManagedState(phase='stopped', desired='stopped', visibility='public'),
    ManagedState(phase='stopped', desired='stopped', pending='operation'),
    ManagedState(phase='stopped', desired='stopped', failure='native_error'),
])
def test_unowned_running_public_pending_or_failed_room_cannot_accept_lease(tmp_path, managed):
    booking = lease()
    transport = Transport(lambda body: {'ok': True, 'sequence': body['sequence'],
                                       'leases': [booking], 'commands': []})
    agent, _, store, _, _ = make_agent(tmp_path, transport=transport, managed=managed)
    result = agent.step()
    assert result['phase'] == 'requires-review'
    assert result['live_rooms_affected'] is False
    assert store.leases(include_ended=True) == []


def test_normalizes_commands_and_reports_ended_cleanup_without_password(tmp_path):
    booking = lease()
    agent, client, store, _, clock = make_agent(tmp_path)
    store.install(booking, BASE)
    closed = {
        'ready': False, 'room_id': None, 'incarnation': None, 'roster_known': False,
        'players': [], 'match': None, 'closed': True, 'ownership_known': True,
        'updated_at': clock.value,
    }
    store.advance(booking['id'], closed)
    store.complete_cleanup(booking['id'])
    active_booking = lease()
    store.install(active_booking, BASE)

    command = {
        'protocol_version': 1, 'operation_id': str(uuid.uuid4()),
        'booking_id': active_booking['id'], 'installation_id': INSTALLATION,
        'generation': GENERATION, 'alias': 'room1',
        'session_generation': active_booking['session_generation'],
        'sequence': 0, 'user_id': OWNER, 'action': 'start', 'payload': {},
        'request_id': str(uuid.uuid4()), 'expected_revision': 0,
        'role_revision': 0, 'role_snapshot': {OWNER: 'owner'},
    }
    transport = Transport(lambda body: {'ok': True, 'sequence': body['sequence'],
                                       'leases': [], 'commands': [command]})
    client._transport = transport
    agent.client = client
    result = agent.step()
    assert result['phase'] == 'connected'
    sent = transport.bodies[0]
    assert sent['capabilities']['active_booking_ids'] == [active_booking['id']]
    report = next(item for item in sent['observations'] if item['booking_id'] == booking['id'])
    assert report['observation']['phase'] == 'ended'
    assert report['observation']['cleanup_complete'] is True
    assert report['observation']['closed'] is True
    assert report['observation']['effective_settings'] == {}
    assert report['observation']['settings_revision'] == store.get(booking['id'])['settings_revision']
    assert 'password' not in repr(report)
    assert store.get(booking['id'])['lease'].get('password') is None
    assert store.get(active_booking['id'])['operation_receipts'][0]['phase'] == 'pending'


def test_reports_private_queue_current_map_and_unavailable_phase(tmp_path):
    agent, _, store, _, clock = make_agent(tmp_path)
    booking = lease()
    store.install(booking, BASE)
    row = store.get(booking['id'])
    supplied = {
        'phase': 'unavailable', 'current_map': {
            'beatmap_id': 456, 'label': 'Private selection', 'stars': 4.25, 'hit_length': 123,
        },
        'queue': [{'beatmap_id': 789, 'label': 'Queued private selection',
                   'stars': 5.5, 'hit_length': 234}],
        'queue_known': True,
        'failure_code': 'booking_preparation_failed',
    }
    observation = agent._reported_observation(row, supplied, 0, clock.value)
    assert observation['phase'] == 'unavailable'
    assert observation['current_map']['label'] == 'Private selection'
    assert observation['queue'][0]['label'] == 'Queued private selection'
    assert observation['queue_known'] is True
    assert observation['failure_code'] == 'booking_preparation_failed'


def test_activity_snapshot_uses_host_lifecycle_deadline_and_hides_stale_status(tmp_path):
    agent, _, store, _, clock = make_agent(tmp_path)
    booking = lease()
    store.install(booking, BASE)

    def native_observation(*, roster, match=None, updated_at=None):
        return {'ready': True, 'room_id': 998, 'incarnation': 'owned-room',
                'roster_known': True, 'players': roster, 'match': match,
                'closed': False, 'ownership_known': True,
                'updated_at': clock.value if updated_at is None else updated_at}

    owner = {'user_id': OWNER, 'username': 'Invented Owner'}
    first = native_observation(roster=[owner])
    store.advance(booking['id'], first)
    clock.value += 100
    quiet = native_observation(roster=[owner])
    store.advance(booking['id'], quiet)
    report = agent._reported_observation(store.get(booking['id']), {
        'phase': 'active', 'room_id': 998, 'incarnation': 'owned-room',
        'roster': [owner], 'roster_known': True, 'ready': True, 'closed': False,
        'ownership_known': True, 'updated_at': clock.value,
    }, 1, clock.value)
    assert report['activity'] == {'condition': 'no_gameplay', 'closes_at': 2800.0}

    clock.value += 100
    running_match = {'id': 'invented-long-map', 'started_at': clock.value,
                     'expected_end': clock.value + 4000, 'ended_at': None,
                     'completed': False, 'aborted': False}
    playing = native_observation(roster=[owner], match=running_match)
    store.advance(booking['id'], playing)
    report = agent._reported_observation(store.get(booking['id']), {
        'phase': 'active', 'room_id': 998, 'incarnation': 'owned-room',
        'roster': [owner], 'roster_known': True, 'ready': True, 'closed': False,
        'ownership_known': True, 'updated_at': clock.value, 'match': running_match,
    }, 2, clock.value)
    assert report['activity'] == {'condition': 'map_in_progress', 'closes_at': clock.value + 4120}

    stale = agent._reported_observation(store.get(booking['id']), {
        'phase': 'active', 'room_id': 998, 'incarnation': 'owned-room',
        'roster': [owner], 'roster_known': True, 'ready': True, 'closed': False,
        'ownership_known': True, 'updated_at': clock.value,
    }, 3, clock.value + 31)
    assert stale['activity'] == {'condition': 'unknown', 'closes_at': None}

    clock.value += 31
    empty = native_observation(roster=[])
    store.advance(booking['id'], empty)
    report = agent._reported_observation(store.get(booking['id']), {
        'phase': 'active', 'room_id': 998, 'incarnation': 'owned-room',
        'roster': [], 'roster_known': True, 'ready': True, 'closed': False,
        'ownership_known': True, 'updated_at': clock.value,
    }, 4, clock.value)
    assert report['activity'] == {'condition': 'empty', 'closes_at': 2800.0}


def test_unknown_failure_code_is_refused_without_disclosing_provider_text(tmp_path):
    booking = lease()
    secret = 'provider exception containing private account details'
    agent, _, store, _, _ = make_agent(
        tmp_path, observation_provider=lambda: [{'booking_id': booking['id'],
            'observation': {'updated_at': 1000, 'failure_code': secret}}])
    store.install(booking, BASE)

    result = agent.step()
    assert result['phase'] == 'requires-review'
    assert result['error'] == 'local-state-refused'
    diagnostic = read_json(agent.diagnostic_path)
    assert secret not in repr(diagnostic)
    assert not agent.client.transport(None).bodies


def test_ended_report_clears_private_map_and_queue_details(tmp_path):
    booking = lease()
    agent, _, store, _, clock = make_agent(tmp_path)
    store.install(booking, BASE)
    closed = {'ready': False, 'room_id': None, 'incarnation': None,
              'roster_known': False, 'players': [], 'match': None,
              'closed': True, 'ownership_known': True, 'updated_at': clock.value}
    store.advance(booking['id'], closed)
    store.complete_cleanup(booking['id'])

    row = store.get(booking['id'])
    supplied = {'phase': 'unavailable', 'current_map': {
        'beatmap_id': 456, 'label': 'Private selection', 'stars': 4.25, 'hit_length': 123},
        'queue': [{'beatmap_id': 789, 'label': 'Private queued selection',
                   'stars': 5.5, 'hit_length': 234}], 'queue_known': True}
    observation = agent._reported_observation(row, supplied, 1, clock.value + 1)
    assert observation['phase'] == 'ended'
    assert observation['current_map'] is None
    assert observation['queue'] == []
    assert observation['queue_known'] is False


def test_ended_cleanup_incomplete_remains_active_and_blocks_alias_replacement(tmp_path):
    agent, _, store, _, clock = make_agent(tmp_path)
    booking = lease()
    store.install(booking, BASE)
    closed = {'ready': False, 'room_id': None, 'incarnation': None,
              'roster_known': False, 'players': [], 'match': None,
              'closed': True, 'ownership_known': True, 'updated_at': clock.value}
    store.advance(booking['id'], closed)
    rows = store.leases()
    assert len(rows) == 1 and rows[0]['state']['phase'] == 'ended'
    body = agent._capabilities({'room1'}, ['standard-fixed'], store)
    assert body['active_booking_ids'] == [booking['id']]
    with pytest.raises(NetworkError) as blocked:
        store.install(lease(), BASE)
    assert blocked.value.code == 'booking_alias_in_use'


def test_top_level_operation_receipts_are_bound_to_local_booking(tmp_path):
    agent, _, store, _, _ = make_agent(tmp_path)
    booking = lease()
    store.install(booking, BASE)
    operation_id = str(uuid.uuid4())
    command = {
        'protocol_version': 1, 'operation_id': operation_id,
        'booking_id': booking['id'], 'host_id': HOST,
        'installation_id': INSTALLATION, 'generation': GENERATION,
        'alias': booking['alias'], 'session_generation': booking['session_generation'],
        'sequence': 0, 'user_id': OWNER, 'action': 'start', 'payload': {},
    }
    store.receive(command)
    store.acknowledge(operation_id, 'failed', error='native_operation_requires_review')

    receipt, = agent._exchange_receipts(store)
    assert receipt == {
        'operation_id': operation_id, 'booking_id': booking['id'],
        'session_generation': booking['session_generation'], 'alias': booking['alias'],
        'phase': 'failed', 'error': 'native_operation_requires_review',
    }
