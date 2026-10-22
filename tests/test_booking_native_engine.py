"""Booking/native integration through the real durable State and Engine.

All upstream room behavior is a local fake adapter; these tests never construct
an SDK client, open a room, or contact a provider.
"""
import copy
import uuid
from pathlib import Path

import pytest

from poolrotater_host.bookings import native
from poolrotater_host.bookings.agent import Agent
from poolrotater_host.bookings.local import LocalSessions
from poolrotater_host.bookings.contracts import observation as validate_wire_observation
from poolrotater_host.layout import atomic_json, read_json
from poolrotater_host.managed_host.engine import BeforeMutationError, Engine
from poolrotater_host.managed_host.state import State
from poolrotater_host.network.contracts import NetworkError

HOST = str(uuid.UUID(int=101))
INSTALLATION = str(uuid.UUID(int=102))
GENERATION = str(uuid.UUID(int=103))
BOOKING = str(uuid.UUID(int=104))


class Clock:
    def __init__(self, value=1000): self.value = value
    def __call__(self): return self.value


class Process:
    def poll(self): return None


class Hub:
    def __init__(self): self.owned = {9876}
    def roster_progress(self): return (1, 1, False)


class Broker:
    def __init__(self):
        self.generation = 'worker-generation-1'
        self.hub = Hub()
        self.players = {777}
        self.known = True
        self.uncertain = False


class Peer:
    def __init__(self): self.calls = []; self.fail = False
    def call(self, *args, **kwargs):
        self.calls.append(args)
        if self.fail: raise TimeoutError('delivery uncertain')


class Worker:
    def __init__(self, booking_id, session_generation, *, players=(777,)):
        self.process = Process()
        self._fresh = True
        self.status = {
            'ready': True, 'room_id': 9876,
            'booking': {'id': booking_id, 'session_generation': session_generation,
                        'match': None, 'roster_known': True,
                        'players': [{'user_id': p, 'username': 'player'} for p in players]},
        }
        self.broker = Broker()
        self.broker.players = set(players)
        self.peer = Peer()
    def fresh(self): return self._fresh


class Registry:
    def __init__(self): self.rooms = {}
    def snapshot(self): return {'rooms': copy.deepcopy(self.rooms)}


class Owner:
    def __init__(self, root, state):
        self.runtime = str(root)
        self.managed = type('Managed', (), {'state': state})()
        self.registry = Registry()
        self.workers = {}
        self.configs = {'room1': {'_authenticated_user_id': 999}}


class LocalAdapter:
    """Executes engine calls against State without any network/provider code."""
    def __init__(self, state, owner):
        self.state = state; self.owner = owner
        self.calls = []; self.fail_open = False; self.fail_close = False
        self.deny_prepare = False; self.worker_generation = 'a' * 32
    def preflight(self, operation):
        self.calls.append(('preflight', operation['action']))
        if (self.deny_prepare and operation['action'] == 'configure' and
                operation['id'] == native.operation_id(BOOKING, 'prepare')):
            raise BeforeMutationError('catalog_unavailable')
    def ready(self, operation): return True
    def apply(self, operation):
        action = operation['action']; alias = operation['alias']
        self.calls.append(('apply', action))
        if action == 'configure':
            return {'application': 'recreate'}
        if action == 'open':
            self.state.reserve(alias, self.worker_generation, operation=operation['id'])
            self.state.mark_dispatched(alias, self.worker_generation)
            if self.fail_open:
                self.state.uncertain_creation(alias, self.worker_generation)
                self.owner.registry.rooms[alias] = {
                    'ownership_unknown': True, 'operation': 'uncertain',
                    'room_id': None, 'generation': self.worker_generation,
                }
                raise RuntimeError('simulated uncertain creation')
            self.state.observe_owned(alias, self.worker_generation, 9876)
            self.owner.registry.rooms[alias] = {
                'ownership_unknown': False, 'operation': 'owned',
                'room_id': 9876, 'generation': self.worker_generation,
            }
            return {'room_id': 9876, 'application': 'recreate'}
        if action == 'close':
            room = self.state.room(alias, private=True)
            if room['room_id'] is None:
                return {'application': 'lifecycle'}
            if self.fail_close:
                raise RuntimeError('simulated uncertain close')
            self.state.confirmed_closed(alias, self.worker_generation, room['room_id'])
            self.owner.registry.rooms[alias] = {
                'ownership_unknown': False, 'operation': 'closed',
                'room_id': None, 'generation': self.worker_generation,
            }
            self.owner.workers.pop(alias, None)
            return {'room_id': 9876, 'application': 'lifecycle'}
        raise AssertionError(f'unexpected native action: {action}')


def make_settings():
    return {'preset': 'standard-fixed', 'visibility': 'private', 'password': 'original-pass',
            'sr_min': 3.5, 'sr_max': 4.5, 'auto_difficulty': False,
            'countdown_seconds': 20, 'loved_mode': False, 'stream_enabled': False}


def setup(root, *, clock=None, settings=None, booking_preset='standard-fixed'):
    clock = clock or Clock()
    root.mkdir(exist_ok=True)
    root.chmod(0o700)
    (root / 'state').mkdir(exist_ok=True)
    (root / 'state').chmod(0o700)
    (root / 'config').mkdir(exist_ok=True)
    (root / 'config').chmod(0o700)
    binding = {'host_id': HOST, 'installation_id': INSTALLATION, 'generation': GENERATION}
    state = State(root, **binding, clock=clock)
    original = copy.deepcopy(settings or make_settings())
    state.seed([{'alias': 'room1', 'desired': 'stopped', 'settings': original}])
    owner = Owner(root, state)
    owner.registry.rooms['room1'] = {
        'ownership_unknown': False, 'operation': 'closed',
        'room_id': None, 'generation': None,
    }
    adapter = LocalAdapter(state, owner)
    engine = Engine(state, adapter)
    atomic_json(root / 'config/private-bookings.json',
                {'format': 1, 'enabled': True, 'aliases': ['room1']})
    coordinator = native.Coordinator(owner, clock=clock)
    lease = {'id': BOOKING, 'user_id': 777, 'designated_friend_id': None,
             **binding, 'alias': 'room1', 'session_generation': 'b' * 32,
             'preset': booking_preset, 'settings': {'sr_min': 3.5, 'sr_max': 4.5},
             'password': 'temporary-private'}
    coordinator.sessions.install(lease, {'sr_min': 3.5, 'sr_max': 4.5, 'countdown_seconds': 20})
    return clock, state, owner, adapter, engine, coordinator, lease, original


def poll(coordinator, engine, booking_id=BOOKING):
    result = coordinator._one(coordinator.sessions.get(booking_id))
    return result, engine.step()


def open_ready(root, *, clock=None, players=(777,), booking_preset='standard-fixed'):
    clock, state, owner, adapter, engine, coordinator, lease, original = setup(
        root, clock=clock, booking_preset=booking_preset)
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert engine.step()['phase'] == 'applied'  # private configure
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert engine.step()['phase'] == 'applied'  # one durable open
    worker = Worker(lease['id'], lease['session_generation'], players=players)
    owner.workers['room1'] = worker
    coordinator._one(coordinator.sessions.get(lease['id']))
    return clock, state, owner, adapter, engine, coordinator, lease, original, worker


def test_real_engine_reservation_claim_serial_commands_and_exact_cleanup(tmp_path):
    (clock, state, owner, adapter, engine, coordinator, lease, original,
     worker) = open_ready(tmp_path)
    assert state.room('room1')['phase'] == 'running'
    assert state.limits()['capacity_used'] == 1
    assert coordinator.sessions.get(lease['id'])['state']['phase'] == 'active'
    assert coordinator.sessions.get(lease['id'])['roles'][777] == 'owner'
    assert state.room('room1', private=True)['config']['password'] == 'temporary-private'

    first = coordinator.sessions.request_local(lease['id'], 777, str(uuid.UUID(int=201)), 'skip', {})
    second = coordinator.sessions.request_local(lease['id'], 777, str(uuid.UUID(int=202)), 'abort', {})
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert worker.peer.calls == [('booking-control', first['operation_id'])]
    # The second command waits for the first command's authoritative receipt.
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert worker.peer.calls == [('booking-control', first['operation_id'])]
    coordinator.sessions.acknowledge(first['operation_id'], 'applied',
                                     application={'application': 'live'})
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert worker.peer.calls[-1] == ('booking-control', second['operation_id'])

    ending = coordinator.sessions.request_local(lease['id'], 777, str(uuid.UUID(int=203)), 'end', {})
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert state.pending()[0]['action'] == 'close'
    assert engine.step()['phase'] == 'applied'
    coordinator._one(coordinator.sessions.get(lease['id']))
    restore_id = native.operation_id(lease['id'], 'restore')
    assert state.operation(restore_id)['phase'] == 'received'
    assert state.room('room1')['desired'] == 'stopped'
    assert engine.step()['phase'] == 'applied'
    coordinator._one(coordinator.sessions.get(lease['id']))
    row = coordinator.sessions.get(lease['id'])
    assert row['cleanup_complete'] is True
    assert state.limits()['capacity_used'] == 0
    assert state.room('room1', private=True)['config'] == original
    assert coordinator.sessions.get(lease['id'])['state']['phase'] == 'ended'

    # Ended host summaries retain lifecycle receipts but clear private live
    # room details, and the native observation file drops the cleaned lease.
    ended_view = Agent.__new__(Agent)._reported_observation(
        coordinator.sessions.get(lease['id']),
        {'current_map': {'beatmap_id': 7, 'label': 'private old map',
                         'stars': 4.2, 'hit_length': 120},
         'queue': [{'beatmap_id': 8, 'label': 'private next map',
                    'stars': 3.1, 'hit_length': 100}], 'queue_known': True},
        sequence=17, now=clock())
    assert ended_view['current_map'] is None
    assert ended_view['queue'] == [] and ended_view['queue_known'] is False
    validate_wire_observation(ended_view)
    coordinator.tick()
    assert read_json(tmp_path / 'state/booking-observations.json')['observations'] == []

    reused = {**lease, 'id': str(uuid.UUID(int=105)),
              'session_generation': 'c' * 32}
    coordinator.sessions.install(reused, {'sr_min': 3.5, 'sr_max': 4.5,
                                          'countdown_seconds': 20})
    coordinator._one(coordinator.sessions.get(reused['id']))
    assert engine.step()['phase'] == 'applied'
    coordinator._one(coordinator.sessions.get(reused['id']))
    assert engine.step()['phase'] == 'applied'
    assert state.limits()['capacity_used'] == 1


def test_uncertain_open_and_close_keep_native_capacity_held(tmp_path):
    clock, state, owner, adapter, engine, coordinator, lease, original = setup(tmp_path)
    adapter.fail_open = True
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert engine.step()['phase'] == 'applied'
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert engine.step()['phase'] == 'uncertain'
    assert state.limits()['capacity_used'] == 1
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert sum(item['action'] == 'open' for item in state.pending()) == 0
    assert sum(call == ('apply', 'open') for call in adapter.calls) == 1

    # A real owned room whose close becomes uncertain also remains a reservation.
    clock2, state2, owner2, adapter2, engine2, coordinator2, lease2, _, worker = open_ready(tmp_path / 'other')
    adapter2.fail_close = True
    coordinator2.sessions.request_local(lease2['id'], 777, str(uuid.UUID(int=204)), 'end', {})
    coordinator2._one(coordinator2.sessions.get(lease2['id']))
    assert engine2.step()['phase'] == 'uncertain'
    assert state2.limits()['capacity_used'] == 1


def test_cross_preset_lease_restores_exact_operator_configuration(tmp_path):
    clock, state, owner, adapter, engine, coordinator, lease, original, worker = open_ready(
        tmp_path, booking_preset='standard-loved')
    leased = state.room('room1', private=True)['config']
    assert leased['preset'] == 'standard-loved'
    assert leased['loved_mode'] is True
    coordinator.sessions.request_local(lease['id'], 777, str(uuid.UUID(int=206)), 'end', {})
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert engine.step()['phase'] == 'applied'
    coordinator._one(coordinator.sessions.get(lease['id']))
    restore_id = native.operation_id(lease['id'], 'restore')
    assert state.operation(restore_id)['phase'] == 'received'
    assert state.operation(restore_id)['result'] is None
    assert engine.step()['phase'] == 'applied'
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert coordinator.sessions.get(lease['id'])['cleanup_complete'] is True
    assert state.room('room1', private=True)['config'] == original


def test_denied_prepare_never_creates_a_room_and_cleanup_is_durable(tmp_path):
    clock, state, owner, adapter, engine, coordinator, lease, original = setup(tmp_path)
    adapter.deny_prepare = True
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert engine.step()['phase'] == 'refused'
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert state.room('room1')['room_id'] is None
    assert state.limits()['capacity_used'] == 0
    assert not any(call == ('apply', 'open') for call in adapter.calls)
    # The first poll records the refused-preparation closure. The next poll
    # queues restoration, which must itself be applied before cleanup releases.
    coordinator._one(coordinator.sessions.get(lease['id']))
    coordinator._one(coordinator.sessions.get(lease['id']))
    restore_id = native.operation_id(lease['id'], 'restore')
    assert state.operation(restore_id)['phase'] == 'received'
    assert engine.step()['phase'] == 'applied'
    coordinator._one(coordinator.sessions.get(lease['id']))
    row = coordinator.sessions.get(lease['id'])
    assert row['cleanup_complete'] is True
    assert state.room('room1', private=True)['config'] == original


def test_unexpected_close_stops_native_intent_before_restore(tmp_path):
    clock, state, owner, adapter, engine, coordinator, lease, original, worker = open_ready(tmp_path)
    state.confirmed_closed('room1', adapter.worker_generation, 9876)
    owner.registry.rooms['room1'] = {
        'ownership_unknown': False, 'operation': 'closed', 'room_id': None,
        'generation': adapter.worker_generation,
    }
    owner.workers.pop('room1')
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert coordinator.sessions.get(lease['id'])['state']['phase'] == 'ended'
    close_id = native.operation_id(lease['id'], 'confirmed-room-closed')
    assert state.operation(close_id)['phase'] == 'received'
    assert state.room('room1')['desired'] == 'running'
    assert engine.step()['phase'] == 'applied'
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert state.room('room1')['desired'] == 'stopped'
    restore_id = native.operation_id(lease['id'], 'restore')
    assert state.operation(restore_id)['phase'] == 'received'
    assert engine.step()['phase'] == 'applied'
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert coordinator.sessions.get(lease['id'])['cleanup_complete'] is True
    assert state.room('room1', private=True)['config'] == original
    assert sum(call == ('apply', 'open') for call in adapter.calls) == 1


def test_lost_worker_rpc_is_terminal_and_never_replayed(tmp_path):
    clock, state, owner, adapter, engine, coordinator, lease, _, worker = open_ready(tmp_path)
    command = coordinator.sessions.request_local(lease['id'], 777, str(uuid.UUID(int=205)), 'skip', {})
    worker.peer.fail = True
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert worker.peer.calls == [('booking-control', command['operation_id'])]
    receipt = next(r for r in coordinator.sessions.get(lease['id'])['operation_receipts']
                   if r['operation_id'] == command['operation_id'])
    assert receipt['phase'] == 'failed'
    assert receipt['error'] == 'worker_action_outcome_unknown'
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert worker.peer.calls == [('booking-control', command['operation_id'])]
    # A replacement worker generation cannot replay the uncertain operation.
    worker.broker.generation = 'worker-generation-2'
    coordinator._dispatch(coordinator.sessions.get(lease['id']),
                          {'operation_id': command['operation_id'], 'action': 'skip', 'user_id': 777})
    assert worker.peer.calls == [('booking-control', command['operation_id'])]


def test_accepted_but_unprocessed_command_times_out_without_replay_and_unblocks_queue(tmp_path):
    clock, state, owner, adapter, engine, coordinator, lease, _, worker = open_ready(tmp_path)
    first = coordinator.sessions.request_local(lease['id'], 777,
                                               str(uuid.UUID(int=207)), 'skip', {})
    second = coordinator.sessions.request_local(lease['id'], 777,
                                                str(uuid.UUID(int=208)), 'abort', {})

    # A successful RPC return proves delivery was accepted by the worker
    # channel, but no gameplay receipt arrives to prove command completion.
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert worker.peer.calls == [('booking-control', first['operation_id'])]
    clock.value += native.COMMAND_OUTCOME_SECONDS - 1
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert worker.peer.calls == [('booking-control', first['operation_id'])]
    clock.value += 1
    coordinator._one(coordinator.sessions.get(lease['id']))
    first_receipt = next(r for r in coordinator.sessions.get(lease['id'])['operation_receipts']
                         if r['operation_id'] == first['operation_id'])
    assert first_receipt['phase'] == 'failed'
    assert first_receipt['error'] == 'worker_action_outcome_unknown'
    # The expired command is terminal and is never retried; the next tick can
    # send the later queued command once the alias gate has cleared.
    assert worker.peer.calls == [('booking-control', first['operation_id'])]
    coordinator._one(coordinator.sessions.get(lease['id']))
    assert worker.peer.calls[-1] == ('booking-control', second['operation_id'])
    assert worker.peer.calls.count(('booking-control', first['operation_id'])) == 1


def test_existing_public_alias_is_not_taken_over_and_default_off_is_inert(tmp_path):
    root = tmp_path / 'disabled'
    root.mkdir(); root.chmod(0o700); (root / 'state').mkdir(mode=0o700)
    cfg = [{'_instance_name': 'room1', 'visibility': 'public'}]
    assert native.overlay(root, cfg) is cfg
    assert not (root / 'state/bookings.sqlite3').exists()
    public = {'preset': 'standard-fixed', 'visibility': 'public', 'sr_min': 3.5,
              'sr_max': 4.5, 'auto_difficulty': False, 'countdown_seconds': 20,
              'loved_mode': False, 'stream_enabled': False}
    _, state, _, _, _, coordinator, lease, _ = setup(tmp_path / 'enabled', settings=public)
    # The existing public slot is never converted into a private reservation.
    with pytest.raises(NetworkError) as error:
        coordinator._original(lease)
    assert error.value.code == 'booking_slot_not_closed'
    assert state.room('room1')['desired'] == 'stopped'
    assert state.limits()['capacity_used'] == 0


def test_tick_map_queue_metadata_is_bounded_and_lifecycle_ignores_it(tmp_path):
    clock, state, owner, adapter, engine, coordinator, lease, _, worker = open_ready(tmp_path)
    maps = [{'beatmap_id': i, 'label': f'map {i}', 'stars': 2.5 + i / 10,
             'hit_length': 90 + i} for i in range(1, 8)]
    worker.status['public'] = {'current_map': maps[0], 'queue': maps,
                               'queue_known': True}
    coordinator.tick()
    saved = read_json(tmp_path / 'state/booking-observations.json')
    assert saved['format'] == 1
    supplied = next(item['observation'] for item in saved['observations']
                    if item['booking_id'] == lease['id'])
    assert supplied['current_map'] == maps[0]
    assert supplied['queue'] == maps[:5]
    assert supplied['queue_known'] is True

    local_row = coordinator.sessions.get(lease['id'])
    reported = Agent.__new__(Agent)._reported_observation(
        local_row, supplied, sequence=4, now=clock())
    validate_wire_observation(reported)
    lifecycle_input = Agent._lifecycle_observation(reported)
    assert not {'current_map', 'queue', 'queue_known'} & set(lifecycle_input)
    state_after, _ = coordinator.sessions.advance(lease['id'], lifecycle_input)
    assert state_after['phase'] == 'active'

    # An unavailable host report exposes only a fixed failure code; user
    # supplied strings and map values never become failure telemetry.
    coordinator._fail(lease['id'], 'native_operation_requires_review')
    clock.value += 1
    coordinator.tick()
    unavailable = next(item['observation'] for item in
                       read_json(tmp_path / 'state/booking-observations.json')['observations']
                       if item['booking_id'] == lease['id'])
    assert unavailable['phase'] == 'unavailable'
    assert unavailable['failure_code'] == 'native_operation_requires_review'
    wire = Agent.__new__(Agent)._reported_observation(
        coordinator.sessions.get(lease['id']), unavailable, sequence=5, now=clock())
    assert wire['phase'] == 'unavailable'
    assert wire['failure_code'] == 'native_operation_requires_review'
    validate_wire_observation(wire)
