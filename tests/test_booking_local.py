"""Focused tests for the host-local booking lease and durable outboxes."""
import copy
import uuid

import pytest

from poolrotater_host.bookings.local import LocalSessions
from poolrotater_host.network.contracts import NetworkError


HOST = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
INSTALLATION = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
GENERATION = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
OWNER = 101
FRIEND = 202
BASE = {'sr_min': 3.5, 'sr_max': 4.5, 'countdown_seconds': 30}


class Clock:
    def __init__(self, value=1000):
        self.value = value

    def __call__(self):
        return self.value


def lease(*, booking_id=None, alias='room1', designated_friend_id=None):
    return {
        'id': booking_id or str(uuid.uuid4()),
        'user_id': OWNER,
        'designated_friend_id': designated_friend_id,
        'host_id': HOST,
        'installation_id': INSTALLATION,
        'generation': GENERATION,
        'alias': alias,
        'session_generation': uuid.uuid4().hex,
        'preset': 'standard-fixed',
        'settings': copy.deepcopy(BASE),
        'password': 'prb_local_test_password',
    }


def make_local(tmp_path, clock=None):
    clock = clock or Clock()
    local = LocalSessions(tmp_path, {'host_id': HOST, 'installation_id': INSTALLATION,
                                     'generation': GENERATION}, clock=clock)
    row = local.install(lease(), BASE)
    return local, clock, row


def command(row, action, *, sequence=0, user_id=OWNER, payload=None, operation_id=None,
            **binding_overrides):
    saved = row['lease']
    result = {
        'protocol_version': 1,
        'operation_id': operation_id or str(uuid.uuid4()),
        'booking_id': saved['id'],
        'host_id': saved['host_id'],
        'installation_id': saved['installation_id'],
        'generation': saved['generation'],
        'alias': saved['alias'],
        'session_generation': saved['session_generation'],
        'sequence': sequence,
        'user_id': user_id,
        'action': action,
        'payload': {} if payload is None else payload,
    }
    result.update(binding_overrides)
    return result


def observation(now, *, players=(), ready=True, room_id=987, incarnation='inc-1',
                roster_known=True, closed=False, ownership_known=True):
    return {
        'ready': ready, 'room_id': room_id, 'incarnation': incarnation,
        'roster_known': roster_known,
        'players': [{'user_id': uid, 'username': f'user-{uid}'} for uid in players],
        'match': None, 'closed': closed, 'ownership_known': ownership_known,
        'updated_at': now,
    }


def test_install_is_idempotent_persistent_and_alias_waits_for_cleanup(tmp_path):
    clock = Clock()
    binding = {'host_id': HOST, 'installation_id': INSTALLATION, 'generation': GENERATION}
    local = LocalSessions(tmp_path, binding, clock=clock)
    first_lease = lease()
    first = local.install(first_lease, BASE)
    assert first['lease']['password'] == first_lease['password']
    assert (tmp_path / 'state' / 'bookings.sqlite3').is_file()

    restarted = LocalSessions(tmp_path, binding, clock=clock)
    assert restarted.install(first_lease, BASE) == first
    with pytest.raises(NetworkError) as occupied:
        restarted.install(lease(), BASE)
    assert occupied.value.code == 'booking_alias_in_use'

    closed = observation(clock.value, ready=False, room_id=None, incarnation=None,
                         roster_known=False, closed=True)
    state, effects = restarted.advance(first_lease['id'], closed)
    assert state['phase'] == 'ended' and effects == []
    cleaned = restarted.complete_cleanup(first_lease['id'])
    assert cleaned['cleanup_complete'] is True
    assert 'password' not in cleaned['lease']
    assert cleaned['roles'] == {}

    replacement = lease()
    restarted.install(replacement, BASE)
    assert restarted.get(replacement['id'])['lease']['password'] == replacement['password']


def test_receive_is_bound_idempotent_and_sequence_checked_after_replay(tmp_path):
    local, clock, row = make_local(tmp_path)
    operation_id = str(uuid.uuid4())
    first_command = command(row, 'start', operation_id=operation_id,
                            request_id=str(uuid.uuid4()), expected_revision=0,
                            role_revision=0, role_snapshot={str(OWNER): 'owner'})
    first = local.receive(first_command)
    assert first['phase'] == 'pending'
    assert local.receive(first_command) == first

    changed = {**first_command, 'payload': {'changed': True}}
    conflict = local.receive(changed)
    assert (conflict['phase'], conflict['error']) == ('failed', 'idempotency_conflict')

    bad_binding = command(row, 'abort', sequence=1, alias='room2')
    mismatch = local.receive(bad_binding)
    assert (mismatch['phase'], mismatch['error']) == ('failed', 'host_generation_mismatch')

    same_sequence = command(row, 'abort', sequence=0)
    stale = local.receive(same_sequence)
    assert (stale['phase'], stale['error']) == ('failed', 'stale_booking_command')
    pending = local.pending(row['lease']['id'])
    assert [item['operation_id'] for item in pending] == [operation_id]
    assert pending[0] == {**first_command, 'origin': 'hub', 'expected_settings_revision': 0}
    assert local.pending(row['lease']['id'])[0] == pending[0]
    ack = local.acknowledge(operation_id, 'applied', application={'phase': 'started'})
    assert ack['phase'] == 'applied'
    receipt = local.get(row['lease']['id'])['operation_receipts'][0]
    assert receipt['application'] == {'phase': 'started'}
    assert receipt['effective_settings'] is None
    assert local.receipts()[0]['origin'] == 'hub'


def test_roster_player_grant_applies_after_ack_and_revoke_is_immediate(tmp_path):
    local, clock, row = make_local(tmp_path)
    local.advance(row['lease']['id'], observation(clock.value, players=[OWNER, FRIEND]))
    assert local.authorize(row['lease']['id'], FRIEND, 'vote') is True
    assert local.authorize(row['lease']['id'], FRIEND, 'start') is False

    grant = command(row, 'grant', sequence=0, payload={'user_id': FRIEND})
    accepted = local.receive(grant)
    assert accepted['phase'] == 'pending'
    assert local.authorize(row['lease']['id'], FRIEND, 'configure') is False
    assert local.pending(row['lease']['id'])[0]['action'] == 'grant'
    local.acknowledge(grant['operation_id'], 'applied', application={'role': 'controller'})
    assert local.authorize(row['lease']['id'], FRIEND, 'configure') is True

    local_request_id = str(uuid.uuid4())
    controller_action = local.request_local(row['lease']['id'], FRIEND, local_request_id,
                                            'skip', {})
    revoke = command(row, 'revoke', sequence=2, payload={'user_id': FRIEND})
    local.receive(revoke)
    assert local.authorize(row['lease']['id'], FRIEND, 'skip') is False
    receipts = {item['operation_id']: item for item in local.get(row['lease']['id'])['operation_receipts']}
    assert receipts[controller_action['operation_id']]['phase'] == 'cancelled'
    assert [item['action'] for item in local.pending(row['lease']['id'])] == ['revoke']
    assert receipts[controller_action['operation_id']]['origin'] == 'local'


def test_configure_commits_only_after_applied_ack_and_enforces_cooldown(tmp_path):
    local, clock, row = make_local(tmp_path)
    first = command(row, 'configure', sequence=0, payload={'sr_min': 4.0})
    local.receive(first)
    assert local.get(row['lease']['id'])['settings'] == BASE
    local.pending(row['lease']['id'])
    blocked = command(row, 'configure', sequence=1, payload={'sr_min': 4.0})
    cooldown = local.receive(blocked)
    assert (cooldown['phase'], cooldown['error']) == ('failed', 'configure_cooldown')
    clock.value += 10
    pending = local.receive(command(row, 'configure', sequence=1,
                                    payload={'sr_min': 4.0}))
    assert (pending['phase'], pending['error']) == ('failed', 'configure_pending')
    local.acknowledge(first['operation_id'], 'failed', error='native_rejected')
    assert local.get(row['lease']['id'])['settings'] == BASE
    second = command(row, 'configure', sequence=1, payload={'sr_min': 4.0})
    local.receive(second)
    local.pending(row['lease']['id'])
    local.acknowledge(second['operation_id'], 'applied',
                      effective_settings={'sr_min': 4.0})
    assert local.get(row['lease']['id'])['settings']['sr_min'] == 4.0
    assert local.get(row['lease']['id'])['settings']['sr_max'] == 4.5


def test_effect_outbox_is_atomic_stable_across_restart_and_acknowledged(tmp_path):
    clock = Clock(10000)
    binding = {'host_id': HOST, 'installation_id': INSTALLATION, 'generation': GENERATION}
    local = LocalSessions(tmp_path, binding, clock=clock)
    saved = lease()
    local.install(saved, BASE)
    waiting = observation(clock.value, players=[999])
    state, effects = local.advance(saved['id'], waiting)
    assert state['phase'] == 'awaiting_claim'
    assert effects == []

    clock.value += 540
    state, effects = local.advance(saved['id'], observation(clock.value, players=[999]))
    assert effects == [{'effect_id': effects[0]['effect_id'],
                        'type': 'warning', 'code': 'claim_required'}]
    effect_id = effects[0]['effect_id']
    restarted = LocalSessions(tmp_path, binding, clock=clock)
    clock.value += 1
    _, replayed = restarted.advance(saved['id'], observation(clock.value, players=[999]))
    assert replayed == effects
    restarted.acknowledge_effect(saved['id'], effect_id)
    clock.value += 1
    _, pending = restarted.advance(saved['id'], observation(clock.value, players=[999]))
    assert pending == []


def test_cleanup_requires_confirmed_close_and_blocks_authorization_after_end(tmp_path):
    local, clock, row = make_local(tmp_path)
    with pytest.raises(NetworkError) as not_closed:
        local.complete_cleanup(row['lease']['id'])
    assert not_closed.value.code == 'booking_not_closed'

    unknown = observation(clock.value, ready=False, room_id=None, incarnation=None,
                          roster_known=False, closed=True, ownership_known=False)
    state, _ = local.advance(row['lease']['id'], unknown)
    assert state['phase'] == 'preparing'
    assert local.authorize(row['lease']['id'], OWNER, 'configure') is True

    confirmed = {**unknown, 'ownership_known': True}
    state, _ = local.advance(row['lease']['id'], confirmed)
    assert state['phase'] == 'ended'
    assert local.authorize(row['lease']['id'], OWNER, 'configure') is False
    assert local.complete_cleanup(row['lease']['id'])['cleanup_complete'] is True


def test_grant_requires_fresh_verified_roster_and_end_blocks_new_actions(tmp_path):
    local, clock, row = make_local(tmp_path)
    stale_roster = observation(clock.value - 31, players=[OWNER, FRIEND])
    local.advance(row['lease']['id'], stale_roster)
    stale = local.receive(command(row, 'grant', payload={'user_id': FRIEND}))
    assert (stale['phase'], stale['error']) == ('failed', 'player_not_in_roster')

    local.advance(row['lease']['id'], observation(clock.value, players=[OWNER, FRIEND]))
    local.receive(command(row, 'end', sequence=0))
    assert local.authorize(row['lease']['id'], OWNER, 'configure') is False
    closing = local.receive(command(row, 'skip', sequence=1))
    assert (closing['phase'], closing['error']) == ('failed', 'booking_closing')
    assert [item['action'] for item in local.pending(row['lease']['id'])] == ['end']


def test_local_commands_have_separate_sequence_and_idempotency_namespace(tmp_path):
    local, _, row = make_local(tmp_path)
    local_request = str(uuid.uuid4())
    command = local.request_local(row['lease']['id'], OWNER, local_request, 'start', {})
    assert command['origin'] == 'local' and command['sequence'] == 0
    assert local.request_local(row['lease']['id'], OWNER, local_request, 'start', {}) == command
    with pytest.raises(NetworkError) as conflict:
        local.request_local(row['lease']['id'], OWNER, local_request, 'abort', {})
    assert conflict.value.code == 'idempotency_conflict'

    hub_command = command_envelope = {
        'protocol_version': 1, 'operation_id': str(uuid.uuid4()),
        'booking_id': row['lease']['id'], 'host_id': HOST,
        'installation_id': INSTALLATION, 'generation': GENERATION,
        'alias': 'room1', 'session_generation': row['lease']['session_generation'],
        'sequence': 0, 'user_id': OWNER, 'action': 'skip', 'payload': {},
    }
    received = local.receive(hub_command)
    assert received['origin'] == 'hub' and received['sequence'] == 0
    pending = local.pending(row['lease']['id'])
    assert [(item['origin'], item['sequence']) for item in pending] == [('local', 0), ('hub', 0)]
    assert pending[0]['operation_id'] == command['operation_id']
    assert pending[1] == {**command_envelope, 'origin': 'hub',
                          'expected_settings_revision': 0}


def test_unauthorized_remote_command_is_a_durable_refusal_never_dispatched(tmp_path):
    local, clock, row = make_local(tmp_path)
    local.advance(row['lease']['id'], observation(clock.value, players=[OWNER, FRIEND]))
    refused = command(row, 'start', sequence=0, user_id=FRIEND)
    first = local.receive(refused)
    second = local.receive(refused)
    assert (first['phase'], first['error']) == ('failed', 'booking_forbidden')
    assert second == first
    assert local.pending(row['lease']['id']) == []
    receipts = local.get(row['lease']['id'])['operation_receipts']
    assert len(receipts) == 1 and receipts[0]['error'] == 'booking_forbidden'


def test_get_decodes_operation_json_fields_and_agent_receipts_exclude_local(tmp_path):
    local, _, row = make_local(tmp_path)
    local_operation = local.request_local(row['lease']['id'], OWNER, str(uuid.uuid4()), 'start', {})
    local.acknowledge(local_operation['operation_id'], 'applied', application={'phase': 'started'})
    saved = local.get(row['lease']['id'])['operation_receipts'][0]
    assert saved['application'] == {'phase': 'started'}
    assert local.receipts() == []


def test_settings_revision_prevents_stale_hub_commands_after_local_commit(tmp_path):
    local, clock, row = make_local(tmp_path)
    booking_id = row['lease']['id']
    configure = command(row, 'configure', sequence=0, payload={'sr_min': 4.0},
                        expected_settings_revision=0)
    local.receive(configure)
    stale_start = command(row, 'start', sequence=1, expected_settings_revision=0)
    local.receive(stale_start)
    dispatched = local.pending(booking_id)
    assert [item['action'] for item in dispatched] == ['configure', 'start']

    local.acknowledge(configure['operation_id'], 'applied',
                      effective_settings={'sr_min': 4.0})
    assert local.get(booking_id)['settings_revision'] == 1
    assert local.pending(booking_id) == []
    stale_receipt = next(item for item in local.get(booking_id)['operation_receipts']
                         if item['operation_id'] == stale_start['operation_id'])
    assert (stale_receipt['phase'], stale_receipt['error']) == (
        'failed', 'settings_revision_conflict')

    remote_configure = command(row, 'configure', sequence=2, payload={'sr_min': 4.1},
                               expected_settings_revision=0)
    refused = local.receive(remote_configure)
    assert (refused['phase'], refused['error']) == ('failed', 'settings_revision_conflict')

    clock.value += 10
    local_configure = local.request_local(booking_id, OWNER, str(uuid.uuid4()),
                                          'configure', {'sr_min': 4.2})
    assert local_configure['phase'] == 'pending'
    assert local.pending(booking_id)[0]['expected_settings_revision'] == 1


def test_command_queue_is_bounded_but_owner_end_is_priority(tmp_path):
    local, _, row = make_local(tmp_path)
    booking_id = row['lease']['id']
    for sequence in range(32):
        accepted = local.receive(command(row, 'start', sequence=sequence))
        assert accepted['phase'] == 'pending'

    overflow = command(row, 'skip', sequence=32)
    refused = local.receive(overflow)
    assert (refused['phase'], refused['error']) == ('failed', 'booking_command_queue_full')
    assert local.receive(overflow) == refused
    with pytest.raises(NetworkError) as local_overflow:
        local.request_local(booking_id, OWNER, str(uuid.uuid4()), 'start', {})
    assert local_overflow.value.code == 'booking_command_queue_full'

    end = local.receive(command(row, 'end', sequence=32))
    assert end['phase'] == 'pending'
    assert [item['action'] for item in local.pending(booking_id)] == ['end']


def test_owner_revoke_is_priority_when_command_queue_is_full(tmp_path):
    local, clock, row = make_local(tmp_path)
    booking_id = row['lease']['id']
    local.advance(booking_id, observation(clock.value, players=[OWNER, FRIEND]))
    grant = local.receive(command(row, 'grant', sequence=0, payload={'user_id': FRIEND}))
    local.pending(booking_id)
    local.acknowledge(grant['operation_id'], 'applied', application={'role': 'controller'})
    for sequence in range(1, 33):
        local.receive(command(row, 'start', sequence=sequence))

    revoke = local.receive(command(row, 'revoke', sequence=33, payload={'user_id': FRIEND}))
    assert revoke['phase'] == 'pending'
    assert not local.authorize(booking_id, FRIEND, 'start')
    pending = local.pending(booking_id)
    assert len(pending) == 33 and pending[-1]['action'] == 'revoke'
