"""Offline integration tests for the private booking/native-room seam.

These tests use a temporary runtime and a fake managed room adapter. They never
construct the provider client or start a room worker.
"""
import copy
import uuid
from pathlib import Path

import pytest

from poolrotater_host.bookings import native
from poolrotater_host.layout import atomic_json
from poolrotater_host.network.contracts import NetworkError


HOST = str(uuid.UUID(int=1))
INSTALLATION = str(uuid.UUID(int=2))
GENERATION = str(uuid.UUID(int=3))


class Clock:
    def __init__(self, value=1000):
        self.value = value

    def __call__(self):
        return self.value


class FakeManagedState:
    """Small adapter matching the native State methods used by Coordinator."""
    def __init__(self, binding):
        self.binding = copy.deepcopy(binding)
        self.rooms = {
            f'room{i}': {
                'desired': 'stopped', 'revision': 0, 'room_generation': None,
                'pending': None, 'room_id': None, 'phase': 'stopped',
                'config': {'preset': 'standard-fixed', 'visibility': 'private',
                           'password': '', 'sr_min': 0, 'sr_max': 10},
            }
            for i in range(1, 5)
        }
        self.operations = {}
        self.enqueued = []

    def room(self, alias, private=False):
        return copy.deepcopy(self.rooms[alias])

    def operation(self, operation_id):
        if operation_id not in self.operations:
            raise NetworkError('managed_operation_not_found', 404)
        return copy.deepcopy(self.operations[operation_id])

    def enqueue(self, value):
        old = self.operations.get(value['id'])
        if old:
            return copy.deepcopy(old)
        self.operations[value['id']] = {
            'id': value['id'], 'phase': 'received', 'action': value['action'],
            'patch': copy.deepcopy(value['patch']), 'alias': value['alias'],
        }
        self.rooms[value['alias']]['pending'] = value['id']
        self.enqueued.append(copy.deepcopy(value))
        return copy.deepcopy(self.operations[value['id']])

    def resolve(self, operation_id, phase='applied'):
        operation = self.operations[operation_id]
        operation['phase'] = phase
        room = self.rooms[operation['alias']]
        room['pending'] = None
        if phase == 'applied':
            if operation['action'] == 'configure':
                room['config'].update(copy.deepcopy(operation['patch']))
                room['revision'] += 1
            elif operation['action'] == 'open':
                room.update(desired='running', phase='running', room_id=9876,
                            room_generation='native-incarnation')
            elif operation['action'] == 'close':
                room.update(desired='stopped', phase='stopped', room_id=None,
                            room_generation=None)


class FakeProcess:
    def poll(self):
        return None


class FakeWorker:
    def __init__(self, booking_id, session_generation, owned):
        self.process = FakeProcess()
        self.status = {
            'ready': True, 'room_id': 9876,
            'booking': {'id': booking_id, 'session_generation': session_generation,
                        'match': None, 'roster_known': True,
                        'players': [{'user_id': 11, 'username': 'owner'}]},
        }
        self._owned = owned
        self.fresh_calls = 0

    def fresh(self):
        self.fresh_calls += 1
        return True


class FakeOwner:
    def __init__(self, root, state):
        self.runtime = str(root)
        self.managed = type('Managed', (), {'state': state})()
        self.registry = type('Registry', (), {})()
        self.workers = {}
        self._journal = {'rooms': {}}
        self.registry.snapshot = lambda: copy.deepcopy(self._journal)


class FakeBroker:
    def __init__(self):
        self.generation = 'worker-generation'
        self.hub = FakeHub()
        self.players = {11}
        self.known = True
        self.uncertain = False


class FakeHub:
    def __init__(self):
        self.owned = {9876}

    def roster_progress(self):
        return (1, 1, False)


def make_lease(alias='room1'):
    return {
        'id': str(uuid.UUID(int=10)), 'user_id': 11,
        'designated_friend_id': None, 'host_id': HOST,
        'installation_id': INSTALLATION, 'generation': GENERATION,
        'alias': alias, 'session_generation': 'a' * 32,
        'preset': 'standard-fixed', 'settings': {'sr_min': 1},
        'password': 'temporary-private-password',
    }


def setup(tmp_path, clock=None):
    clock = clock or Clock()
    (tmp_path / 'state').mkdir(exist_ok=True)
    tmp_path.chmod(0o700)
    (tmp_path / 'state').chmod(0o700)
    binding = {'host_id': HOST, 'installation_id': INSTALLATION,
               'generation': GENERATION}
    state = FakeManagedState(binding)
    owner = FakeOwner(tmp_path, state)
    coordinator = native.Coordinator(owner, clock=clock)
    lease = make_lease()
    base = {'sr_min': 0, 'sr_max': 10}
    coordinator.sessions.install(lease, base)
    return clock, state, owner, coordinator, lease


def enable(root, aliases=('room1',)):
    (Path(root) / 'config').mkdir(exist_ok=True)
    (Path(root) / 'config').chmod(0o700)
    atomic_json(Path(root) / 'config/private-bookings.json',
                {'format': 1, 'enabled': True, 'aliases': list(aliases)})


def test_overlay_is_disabled_by_default_and_never_takes_over_public_alias(tmp_path):
    cfg = [{'_instance_name': 'room1', 'preset': 'standard-fixed',
            'visibility': 'public', 'sr_min': 1}]
    original = copy.deepcopy(cfg)
    tmp_path.chmod(0o700)
    (tmp_path / 'state').mkdir(exist_ok=True)
    (tmp_path / 'state').chmod(0o700)
    assert native.overlay(tmp_path, cfg) == original
    assert cfg == original

    # Even under explicit enablement, native startup cannot take over a public
    # or running operator room as a private booking.
    _, state, _, coordinator, lease = setup(tmp_path)
    state.rooms['room1']['config']['visibility'] = 'public'
    state.rooms['room1']['desired'] = 'running'
    with pytest.raises(NetworkError):
        coordinator._original(lease)


def test_prepare_open_are_durable_stages_and_precreation_closed_is_not_lease_end(tmp_path):
    clock, state, owner, coordinator, lease = setup(tmp_path)
    enable(tmp_path)
    owner._journal['rooms']['room1'] = {
        'ownership_unknown': False, 'operation': 'closed', 'room_id': None,
        'generation': None,
    }

    observed = coordinator._one(coordinator.sessions.get(lease['id']))
    assert observed['closed'] is False
    prepare_id = native.operation_id(lease['id'], 'prepare')
    assert [x['id'] for x in state.enqueued] == [prepare_id]
    assert state.enqueued[0]['action'] == 'configure'
    assert state.enqueued[0]['patch']['visibility'] == 'private'
    assert state.enqueued[0]['patch']['preset'] == lease['preset']

    # Repeated poll while native acknowledgement is uncertain does not create
    # a second configure or open operation.
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert [x['id'] for x in state.enqueued] == [prepare_id]
    state.resolve(prepare_id)
    coordinator._one(coordinator.sessions.get(lease['id']))
    open_id = native.operation_id(lease['id'], 'open')
    assert state.enqueued[-1]['id'] == open_id
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert sum(item['id'] == open_id for item in state.enqueued) == 1

    state.resolve(open_id)
    owner._journal['rooms']['room1'] = {
        'ownership_unknown': False, 'operation': 'owned', 'room_id': 9876,
        'generation': 'native-incarnation',
    }
    worker = FakeWorker(lease['id'], lease['session_generation'], {9876})
    worker.broker = FakeBroker()
    worker.peer = type('Peer', (), {'call': lambda self, *args, **kwargs: None})()
    owner.workers['room1'] = worker
    owner.configs = {'room1': {'_authenticated_user_id': 99}}
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert coordinator.sessions.get(lease['id'])['state']['phase'] == 'active'
    assert state.rooms['room1']['config']['sr_min'] == 1
    assert state.rooms['room1']['config']['sr_max'] == 10
    assert state.rooms['room1']['config']['visibility'] == 'private'


def test_native_failure_keeps_slot_held_and_original_defaults_are_restored(tmp_path):
    clock, state, owner, coordinator, lease = setup(tmp_path)
    enable(tmp_path)
    state.rooms['room1']['config'] = {
        'preset': 'standard-fixed', 'visibility': 'private', 'password': '',
        'sr_min': 2, 'sr_max': 9, 'countdown_seconds': 40,
    }
    state.resolve = state.resolve  # retain the adapter's controllable receipt seam
    coordinator._one(coordinator.sessions.get(lease['id']))
    prepare_id = native.operation_id(lease['id'], 'prepare')
    state.resolve(prepare_id, 'uncertain')
    coordinator._one(coordinator.sessions.get(lease['id']))
    row = coordinator.sessions.get(lease['id'])
    assert row['cleanup_complete'] is False
    assert row['state']['phase'] == 'preparing'
    assert (tmp_path / 'state/booking-native.sqlite3').exists()
    saved = coordinator._original(lease)
    assert saved['sr_min'] == 2 and saved['sr_max'] == 9
    assert saved['countdown_seconds'] == 40


def test_claim_expiry_releases_only_after_confirmed_close_and_restore(tmp_path):
    clock, state, owner, coordinator, lease = setup(tmp_path)
    enable(tmp_path)
    original = copy.deepcopy(state.rooms['room1']['config'])

    coordinator._one(coordinator.sessions.get(lease['id']))
    prepare_id = native.operation_id(lease['id'], 'prepare')
    state.resolve(prepare_id)
    coordinator._one(coordinator.sessions.get(lease['id']))
    open_id = native.operation_id(lease['id'], 'open')
    state.resolve(open_id)
    owner._journal['rooms']['room1'] = {
        'ownership_unknown': False, 'operation': 'owned', 'room_id': 9876,
        'generation': 'native-incarnation',
    }
    worker = FakeWorker(lease['id'], lease['session_generation'], set())
    worker.broker = FakeBroker()
    worker.broker.players = set()
    worker.status['booking']['players'] = []
    worker.peer = type('Peer', (), {'call': lambda self, *args, **kwargs: None})()
    owner.workers['room1'] = worker
    owner.configs = {'room1': {'_authenticated_user_id': 99}}
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert coordinator.sessions.get(lease['id'])['state']['phase'] == 'awaiting_claim'

    # No claimant arrives before the deadline. The slot remains leased while
    # native close is uncertain and while original config is being restored.
    clock.value += 601
    coordinator._one(coordinator.sessions.get(lease['id']))
    row = coordinator.sessions.get(lease['id'])
    assert row['state']['phase'] == 'closing'
    assert row['cleanup_complete'] is False
    close_entry = next(x for x in state.enqueued if x['action'] == 'close')
    state.resolve(close_entry['id'])
    owner._journal['rooms']['room1'] = {
        'ownership_unknown': False, 'operation': 'closed', 'room_id': None,
        'generation': 'native-incarnation',
    }
    coordinator._one(coordinator.sessions.get(lease['id']))
    row = coordinator.sessions.get(lease['id'])
    assert row['state']['phase'] == 'ended'
    assert row['cleanup_complete'] is False
    restore_id = native.operation_id(lease['id'], 'restore')
    assert state.operations[restore_id]['action'] == 'configure'
    assert state.operations[restore_id]['patch'] == original

    state.resolve(restore_id)
    coordinator._one(coordinator.sessions.get(lease['id']))
    row = coordinator.sessions.get(lease['id'])
    assert row['cleanup_complete'] is True
    assert row['settings'] == original or row['settings'] == {'sr_min': 0, 'sr_max': 10}
    assert state.rooms['room1']['config'] == original


def test_worker_action_is_not_replayed_after_generation_is_lost(tmp_path):
    clock, state, owner, coordinator, lease = setup(tmp_path)
    enable(tmp_path)
    coordinator._one(coordinator.sessions.get(lease['id']))
    # A command envelope is only used after a verified worker binding.
    command = {'operation_id': str(uuid.UUID(int=50)), 'action': 'start',
               'user_id': lease['user_id']}
    worker = FakeWorker(lease['id'], lease['session_generation'], {9876})
    worker.broker = FakeBroker()
    calls = []
    worker.peer = type('Peer', (), {'call': lambda self, *args, **kwargs: calls.append(args)})()
    owner.workers['room1'] = worker
    owner.configs = {'room1': {'_authenticated_user_id': 99}}
    coordinator.sessions.acknowledge = lambda *args, **kwargs: {'phase': 'failed'}
    row = coordinator.sessions.get(lease['id'])
    # Hold the action until the coordinator's normal native-open stages are
    # present, then record an unknown worker outcome and lose that generation.
    state.resolve(native.operation_id(lease['id'], 'prepare'))
    coordinator._one(row)
    state.resolve(native.operation_id(lease['id'], 'open'))
    coordinator._dispatch(row, command)
    assert calls == [('booking-control', command['operation_id'])]
    worker.broker.generation = 'replacement-worker-generation'
    coordinator._dispatch(row, command)
    assert calls == [('booking-control', command['operation_id'])]
