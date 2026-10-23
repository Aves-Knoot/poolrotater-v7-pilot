"""Focused closeout invariants for requester allocation and clock safety."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from poolrotater_host.bookings.local import LocalSessions
from poolrotater_host.bookings.repository import Repository
from poolrotater_host.network.contracts import NetworkError


HOST_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
HOST_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
INSTALL_A = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
INSTALL_B = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
GENERATION_A = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
GENERATION_B = 'ffffffff-ffff-4fff-8fff-ffffffffffff'
OWNER = 451


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


def make_multi_host_repo(root, clock):
    bindings = {
        HOST_A: (INSTALL_A, GENERATION_A),
        HOST_B: (INSTALL_B, GENERATION_B),
    }

    def eligibility(host_id, alias):
        installation_id, generation = bindings[host_id]
        return {
            'supported': True,
            'fresh': True,
            'host_id': host_id,
            'installation_id': installation_id,
            'generation': generation,
            'effective_limit': 1,
            'capacity_used': 0,
            'observed_booking_ids': [],
            'supported_presets': ['standard-auto'],
        }

    repo = Repository(root, clock=clock, eligibility=eligibility)
    repo.configure_slot('slot-a', HOST_A, 'room1', 'Pool A', ['standard-auto'],
                        {'sr_min': 1, 'sr_max': 5}, enabled=True)
    repo.configure_slot('slot-b', HOST_B, 'room1', 'Pool B', ['standard-auto'],
                        {'sr_min': 1, 'sr_max': 5}, enabled=True)
    return repo, eligibility


def reserve(repo, slot_id, *, user_id=OWNER, request_id=None):
    return repo.reserve(user_id, request_id or str(uuid.uuid4()), slot_id,
                        'standard-auto', {'sr_min': 2})


def host_observation(booking, now, *, phase='active', sequence=1, **changes):
    value = {
        'sequence': sequence,
        'phase': phase,
        'room_id': 1901,
        'incarnation': 'booking-incarnation',
        'roster': [],
        'roster_known': True,
        'updated_at': now,
        'ready': True,
        'closed': False,
        'effective_settings': booking['settings'],
        'cleanup_complete': False,
        'ownership_known': True,
        'operation_receipts': [],
        'installation_id': booking['installation_id'],
        'generation': booking['generation'],
        'session_generation': booking['session_generation'],
        'alias': booking['alias'],
    }
    value.update(changes)
    return value


@pytest.mark.parametrize('uncertainty', ['preparing', 'closing'])
def test_uncertain_booking_never_fails_over_to_an_alternate_host(tmp_path, uncertainty):
    clock = Clock()
    repo, _ = make_multi_host_repo(tmp_path / 'hub', clock)
    first = reserve(repo, 'slot-a')

    if uncertainty == 'closing':
        repo.observe(HOST_A, first['id'], host_observation(first, clock.value))
        revision = repo.view(first['id'], OWNER)['revision']
        repo.submit(first['id'], OWNER, str(uuid.uuid4()), 'end', {}, revision)
        assert repo.view(first['id'], OWNER)['phase'] == 'closing'
        # No host closure or cleanup receipt is delivered: this remains uncertain.
        assert repo.view(first['id'], OWNER)['password'] == first['password']
    else:
        assert repo.view(first['id'], OWNER)['phase'] == 'preparing'

    with pytest.raises(NetworkError) as refused:
        reserve(repo, 'slot-b')
    assert refused.value.code == 'booking_already_active'
    assert repo.active_for_user(OWNER)['id'] == first['id']
    assert repo.leases(HOST_B, INSTALL_B, GENERATION_B) == []
    assert repo.view(first['id'], OWNER)['phase'] == uncertainty


def test_two_slots_on_two_hosts_racing_for_one_requester_create_one_booking(tmp_path):
    clock = Clock()
    repo, eligibility = make_multi_host_repo(tmp_path / 'hub', clock)
    second_handle = Repository(repo.root, clock=clock, eligibility=eligibility)
    barrier_slots = ('slot-a', 'slot-b')
    barrier = Barrier(2)

    def attempt(slot_id):
        handle = repo if slot_id == 'slot-a' else second_handle
        barrier.wait(timeout=5)
        try:
            return ('created', reserve(handle, slot_id)['id'])
        except NetworkError as error:
            return ('refused', error.code)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, barrier_slots))

    created = [value for status, value in outcomes if status == 'created']
    refused = [value for status, value in outcomes if status == 'refused']
    assert len(created) == 1
    assert refused == ['booking_already_active']
    active = repo.active_for_user(OWNER)
    assert active['id'] == created[0]
    bindings = {
        'slot-a': (HOST_A, INSTALL_A, GENERATION_A),
        'slot-b': (HOST_B, INSTALL_B, GENERATION_B),
    }
    winning_host = bindings[active['slot_id']]
    losing_host = bindings['slot-b' if active['slot_id'] == 'slot-a' else 'slot-a']
    winner_leases = repo.leases(*winning_host)
    assert [lease['booking_id'] for lease in winner_leases] == [created[0]]
    assert repo.leases(*losing_host) == []


def local_observation(now, *, players=(), updated_at=None):
    return {
        'ready': True,
        'room_id': 1901,
        'incarnation': 'booking-incarnation',
        'roster_known': True,
        'players': [{'user_id': user_id, 'username': f'user-{user_id}'}
                    for user_id in players],
        'match': None,
        'closed': False,
        'ownership_known': True,
        'updated_at': now if updated_at is None else updated_at,
    }


def test_forward_clock_jump_requires_fresh_native_observation(tmp_path):
    clock = Clock()
    binding = {'host_id': HOST_A, 'installation_id': INSTALL_A,
               'generation': GENERATION_A}
    sessions = LocalSessions(tmp_path / 'host', binding, clock=clock)
    booking_id = str(uuid.uuid4())
    lease = {
        'id': booking_id,
        'user_id': OWNER,
        'designated_friend_id': None,
        **binding,
        'alias': 'room1',
        'session_generation': 'a' * 32,
        'preset': 'standard-auto',
        'settings': {'sr_min': 2},
        'password': 'private-test-secret',
    }
    sessions.install(lease, {'sr_min': 1, 'sr_max': 5})

    # First fresh, owned observation starts and claims the session.
    sessions.advance(booking_id, local_observation(1000, players=[OWNER]))
    assert sessions.get(booking_id)['state']['phase'] == 'active'
    assert sessions.get(booking_id)['state']['clock_high_water'] == 1000

    # The wall clock leaps past the inactivity deadline. Old native state is
    # stale, so it cannot prove the room is empty or authorize closure.
    clock.value = 5000
    _, effects = sessions.advance(
        booking_id, local_observation(5000, players=[], updated_at=1000))
    state = sessions.get(booking_id)['state']
    assert effects == []
    assert state['phase'] == 'active'
    assert state['clock_high_water'] == 1000
    assert not state['close_requested']

    # A fresh authoritative observation at the jumped time can now establish
    # the inactivity condition and request closure.
    _, effects = sessions.advance(booking_id, local_observation(5000, players=[]))
    state = sessions.get(booking_id)['state']
    assert state['phase'] == 'closing'
    assert state['clock_high_water'] == 5000
    assert state['close_requested']
    assert any(effect['type'] == 'close' and
               effect['reason'] == 'inactive_timeout' for effect in effects)
