"""Focused isolated tests for the private booking ledger."""
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from poolrotater_host.bookings.repository import Repository
from poolrotater_host.bookings.contracts import observation as validate_booking_observation
from poolrotater_host.network.contracts import NetworkError,canonical

HOST = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
INSTALL = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
GENERATION = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'


class Clock:
    value = 1000.0
    def __call__(self):
        return self.value


def make_repo(tmp_path, *, capacity=2, counted=()):
    clock = Clock()
    status = {'capacity': capacity, 'counted': list(counted), 'fresh': True}
    def eligibility(host_id, alias):
        return {'supported': True, 'fresh': status['fresh'], 'host_id': HOST,
                'installation_id': INSTALL, 'generation': GENERATION,
                'effective_limit': status['capacity'], 'capacity_used': len(status['counted']),
                'observed_booking_ids': list(status['counted']), 'supported_presets': ['standard-auto']}
    repo = Repository(tmp_path / 'private', clock=clock, eligibility=eligibility)
    repo.configure_slot('slot-a', HOST, 'room1', 'Pool A', ['standard-auto'],
                        {'sr_min': 1, 'sr_max': 5}, enabled=True)
    return repo, clock, status


def reserve(repo, *, uid=10, rid=None, settings=None):
    return repo.reserve(uid, rid or str(uuid.uuid4()), 'slot-a', 'standard-auto', settings or {'sr_min': 2})


def observation(booking, clock, **updates):
    value = {
        'sequence': 1, 'phase': 'active', 'room_id': 987, 'incarnation': 'inc-1',
        'roster': [], 'roster_known': True, 'updated_at': clock.value, 'ready': True,
        'closed': False, 'effective_settings': booking['settings'],
        'cleanup_complete': False, 'ownership_known': True, 'operation_receipts': [],
        'installation_id': INSTALL, 'generation': GENERATION,
        'session_generation': booking['session_generation'], 'alias': 'room1',
    }
    value.update(updates)
    return value


def test_disabled_default_and_fail_closed_eligibility(tmp_path):
    clock = Clock()
    repo = Repository(tmp_path / 'private', clock=clock, eligibility=None)
    repo.configure_slot('slot-a', HOST, 'room1', 'Pool A', ['standard-auto'],
                        {'sr_min': 1, 'sr_max': 5}, enabled=False)
    assert repo.public_slots()[0]['state'] == 'unavailable'
    with pytest.raises(NetworkError):
        repo.reserve(1, str(uuid.uuid4()), 'slot-a', 'standard-auto', {})


def test_reservation_idempotency_and_native_plus_unobserved_capacity(tmp_path):
    repo, _, _ = make_repo(tmp_path, capacity=2, counted=['native-booking'])
    repo.configure_slot('slot-b', HOST, 'room2', 'Pool B', ['standard-auto'],
                        {'sr_min': 1, 'sr_max': 5}, enabled=True)
    request_id = str(uuid.uuid4())
    first = repo.reserve(10, request_id, 'slot-a', 'standard-auto', {'sr_min': 2})
    assert first == repo.reserve(10, request_id, 'slot-a', 'standard-auto', {'sr_min': 2})
    assert first['host_id'] == HOST and first['installation_id'] == INSTALL
    assert len(first['session_generation']) == 32
    with pytest.raises(NetworkError) as error:
        repo.reserve(11, str(uuid.uuid4()), 'slot-b', 'standard-auto', {'sr_min': 2})
    assert error.value.code == 'host_capacity'


def test_user_request_reuse_with_changed_payload_conflicts(tmp_path):
    repo, _, _ = make_repo(tmp_path)
    rid = str(uuid.uuid4())
    reserve(repo, uid=10, rid=rid)
    with pytest.raises(NetworkError) as error:
        reserve(repo, uid=10, rid=rid, settings={'sr_min': 3})
    assert error.value.code == 'idempotency_conflict'


def test_leases_bind_full_session_and_active_user_view(tmp_path):
    repo, _, _ = make_repo(tmp_path)
    booking = reserve(repo)
    lease, = repo.leases(HOST, INSTALL, GENERATION)
    assert lease['booking_id'] == booking['id']
    assert lease['password'] == booking['password']
    assert repo.active_for_user(10)['role'] == 'owner'
    assert repo.leases(HOST, INSTALL, 'dddddddd-dddd-4ddd-8ddd-dddddddddddd') == []


def test_activity_deadline_is_private_and_requires_fresh_known_roster(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    activity = {'condition': 'empty', 'closes_at': clock.value + 180}
    repo.observe(HOST, booking['id'], observation(booking, clock, activity=activity))
    private = repo.view(booking['id'], 10)
    assert private['activity'] == activity
    assert 'activity' not in repo.public_slots()[0]
    with pytest.raises(NetworkError) as forbidden:
        repo.view(booking['id'], 999)
    assert forbidden.value.code == 'booking_forbidden'

    clock.value += 31
    stale_view = repo.view(booking['id'], 10)
    assert stale_view['activity'] == {'condition': 'unknown', 'closes_at': None}
    assert clock.value - stale_view['activity_updated_at'] > 30

    fresh_unknown = observation(booking, clock, sequence=2, roster_known=False, activity=activity)
    repo.observe(HOST, booking['id'], fresh_unknown)
    unknown_view = repo.view(booking['id'], 10)
    assert unknown_view['activity'] == {'condition': 'unknown', 'closes_at': None}
    assert unknown_view['activity_updated_at'] == clock.value


def test_invalid_activity_snapshot_is_refused(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    invalid = {'condition': 'unknown', 'closes_at': clock.value + 30}
    with pytest.raises(NetworkError) as error:
        repo.observe(HOST, booking['id'], observation(booking, clock, activity=invalid))
    assert error.value.code == 'booking_invalid_activity'


def test_creation_lease_is_immutable_after_observation_role_and_settings_changes(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    initial, = repo.leases(HOST, INSTALL, GENERATION)
    repo.observe(HOST, booking['id'], observation(booking, clock,
                 roster=[{'user_id': 11, 'username': 'friend'}]))
    grant = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'grant', {'user_id': 11}, 1)
    repo.observe(HOST, booking['id'], observation(booking, clock, sequence=2,
                 roster=[{'user_id': 11, 'username': 'friend'}],
                 operation_receipts=[{'operation_id': grant['id'], 'phase': 'applied'}]))
    current = repo.view(booking['id'], 10)
    configure = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'configure', {'sr_min': 3},
                            current['revision'])
    assert repo.view(booking['id'], 10)['settings']['sr_min'] == 2
    repo.observe(HOST, booking['id'], observation(booking, clock, sequence=3,
                 roster=[{'user_id': 11, 'username': 'friend'}],
                 effective_settings=json.loads(configure['payload']),
                 operation_receipts=[{'operation_id': configure['id'], 'phase': 'applied',
                                      'application': 'live'}]))

    assert repo.leases(HOST, INSTALL, GENERATION) == [initial]
    assert repo.view(booking['id'], 10)['applied_settings']['sr_min'] == 3
    assert repo.view(booking['id'], 11)['role'] == 'controller'


def test_claim_deadline_is_taken_from_first_ready_host_observation_and_never_replaced(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    first = observation(booking, clock)
    first['claim_deadline'] = clock.value + 600
    repo.observe(HOST, booking['id'], first)
    pinned = repo.view(booking['id'], 10)['claim_deadline']
    changed = observation(booking, clock, sequence=2)
    changed['claim_deadline'] = clock.value + 900
    repo.observe(HOST, booking['id'], changed)
    assert repo.view(booking['id'], 10)['claim_deadline'] == pinned


def test_owner_controller_permissions_and_live_roster_grant_ack(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    with pytest.raises(NetworkError) as denied:
        repo.view(booking['id'], 99)
    assert denied.value.status == 403
    first = observation(booking, clock, roster=[{'user_id': 11, 'username': 'friend'}])
    repo.observe(HOST, booking['id'], first)
    operation = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'grant', {'user_id': 11}, 1)
    with pytest.raises(NetworkError):
        repo.view(booking['id'], 11)
    delivered, = repo.pending(HOST)
    assert delivered['operation_id'] == operation['id']
    assert repo.pending(HOST) == [delivered]
    applied = observation(booking, clock, sequence=2, roster=[{'user_id': 11, 'username': 'friend'}],
                          operation_receipts=[{'operation_id': operation['id'], 'phase': 'applied', 'application': 'live'}])
    repo.observe(HOST, booking['id'], applied)
    assert repo.view(booking['id'], 11)['role'] == 'controller'
    with pytest.raises(NetworkError) as denied:
        repo.submit(booking['id'], 11, str(uuid.uuid4()), 'end', {}, repo.view(booking['id'], 11)['revision'])
    assert denied.value.status == 403


def test_pending_grant_can_be_revoked_and_late_ack_stays_cancelled(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock,
                 roster=[{'user_id': 11, 'username': 'friend'}]))
    grant = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'grant',
                        {'user_id': 11}, 1)
    revoke = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'revoke',
                         {'user_id': 11}, 2)

    with pytest.raises(NetworkError) as denied:
        repo.view(booking['id'], 11)
    assert denied.value.status == 403
    assert [item['operation_id'] for item in repo.pending(HOST)] == [revoke['id']]

    late = {'operation_id': grant['id'], 'booking_id': booking['id'],
            'session_generation': booking['session_generation'], 'alias': 'room1',
            'phase': 'applied'}
    assert repo.observe_receipts(HOST, [late]) == {'accepted': 0}
    with pytest.raises(NetworkError) as conflict:
        repo.observe_receipts(HOST, [{**late, 'phase': 'failed'}])
    assert conflict.value.code == 'operation_receipt_conflict'
    with pytest.raises(NetworkError):
        repo.view(booking['id'], 11)


def test_duplicate_pending_grant_is_rejected(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock,
                 roster=[{'user_id': 11, 'username': 'friend'}]))
    repo.submit(booking['id'], 10, str(uuid.uuid4()), 'grant', {'user_id': 11}, 1)
    with pytest.raises(NetworkError) as duplicate:
        repo.submit(booking['id'], 10, str(uuid.uuid4()), 'grant', {'user_id': 11}, 2)
    assert duplicate.value.code == 'invalid_controller'


def test_owner_can_retry_end_after_lost_reply_and_terminal_role_cleanup(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock))
    request_id = str(uuid.uuid4())
    ending = repo.submit(booking['id'], 10, request_id, 'end', {}, 1)
    repo.observe(HOST, booking['id'], observation(
        booking, clock, sequence=2, phase='ended', room_id=None, incarnation=None,
        ready=False, closed=True, cleanup_complete=True,
        operation_receipts=[{'operation_id': ending['id'], 'phase': 'applied'}]))
    with repo._transaction() as db:
        db.execute("UPDATE bookings SET role_data='{}' WHERE id=?", (booking['id'],))

    retry = repo.submit(booking['id'], 10, request_id, 'end', {}, 1)
    assert retry['id'] == ending['id'] and retry['phase'] == 'applied'


def test_concurrent_identical_reservations_return_one_durable_operation(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    second_handle = Repository(repo.root, clock=clock, eligibility=repo.eligibility)
    request_id = str(uuid.uuid4())
    args = (10, request_id, 'slot-a', 'standard-auto', {'sr_min': 2})
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: (repo if index % 2 else second_handle).reserve(*args),
                                range(8)))
    assert all(result == results[0] for result in results)


def test_competing_reservations_atomically_allocate_a_slot_once(tmp_path):
    repo, clock, _ = make_repo(tmp_path, capacity=4)
    repo.configure_slot('slot-b', HOST, 'room2', 'Pool B', ['standard-auto'],
                        {'sr_min': 1, 'sr_max': 5}, enabled=True)
    second_handle = Repository(repo.root, clock=clock, eligibility=repo.eligibility)

    def attempt(index):
        handle = repo if index % 2 else second_handle
        try:
            return handle.reserve(index + 100, str(uuid.uuid4()), 'slot-a',
                                  'standard-auto', {'sr_min': 2})['id']
        except NetworkError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))
    assert sum(item not in ('slot_unavailable',) for item in outcomes) == 1
    assert outcomes.count('slot_unavailable') == 7


def test_revoked_controller_cannot_replay_private_operation_response(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock, roster=[{'user_id': 11, 'username': 'friend'}]))
    grant = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'grant', {'user_id': 11}, 1)
    repo.observe(HOST, booking['id'], observation(booking, clock, sequence=2,
                 roster=[{'user_id': 11, 'username': 'friend'}],
                 operation_receipts=[{'operation_id': grant['id'], 'phase': 'applied'}]))
    rid = str(uuid.uuid4())
    prior = repo.submit(booking['id'], 11, rid, 'skip', {}, repo.view(booking['id'], 11)['revision'])
    repo.submit(booking['id'], 10, str(uuid.uuid4()), 'revoke', {'user_id': 11}, repo.view(booking['id'], 10)['revision'])
    with pytest.raises(NetworkError) as denied:
        repo.submit(booking['id'], 11, rid, 'skip', {}, prior['expected_revision'])
    assert denied.value.status == 403


def test_failed_or_incomplete_closure_keeps_slot_and_terminal_hides_credentials(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    failed = observation(booking, clock, phase='failed', room_id=None, closed=True,
                         cleanup_complete=False)
    repo.observe(HOST, booking['id'], failed)
    assert repo.view(booking['id'], 10)['phase'] == 'unavailable'
    with repo._transaction() as db:
        row = db.execute('SELECT password,lease_envelope FROM bookings WHERE id=?',
                         (booking['id'],)).fetchone()
        cached, = db.execute('SELECT response FROM requests WHERE booking_id=?',
                             (booking['id'],)).fetchone()
    assert row['password'] == booking['password']
    assert json.loads(row['lease_envelope'])['password'] == booking['password']
    assert json.loads(cached)['password'] == booking['password']
    with pytest.raises(NetworkError):
        repo.reserve(11, str(uuid.uuid4()), 'slot-a', 'standard-auto', {})
    closed = observation(booking, clock, sequence=2, phase='ended', room_id=None,
                         closed=True, cleanup_complete=True)
    repo.observe(HOST, booking['id'], closed)
    terminal = repo.view(booking['id'], 10)
    assert terminal['phase'] == 'ended'
    assert terminal['password'] is None and terminal['join_url'] is None
    with repo._transaction() as db:
        row = db.execute('SELECT password,lease_envelope,room_id,incarnation,role_data,observation '
                         'FROM bookings WHERE id=?', (booking['id'],)).fetchone()
        cached, = db.execute('SELECT response FROM requests WHERE booking_id=?',
                             (booking['id'],)).fetchone()
    assert row['password'] == '' and row['lease_envelope'] is None
    assert row['room_id'] is None and row['incarnation'] is None and row['role_data'] == '{}'
    assert json.loads(row['observation'])['roster'] == []
    assert json.loads(cached)['password'] is None


def test_end_replay_is_terminal_and_scrubs_session_material_before_alias_reuse(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    request_id = str(uuid.uuid4())
    original = repo.reserve(10, request_id, 'slot-a', 'standard-auto', {'sr_min': 2})
    private_map = {'beatmap_id': 123, 'label': 'Private map', 'stars': 4.5,
                   'hit_length': 120}
    repo.observe(HOST, original['id'], observation(
        original, clock, roster=[{'user_id': 10, 'username': 'Owner'},
                                 {'user_id': 11, 'username': 'Friend'}],
        current_map=private_map, queue=[private_map], queue_known=True))
    grant = repo.submit(original['id'], 10, str(uuid.uuid4()), 'grant',
                        {'user_id': 11}, repo.view(original['id'], 10)['revision'])
    repo.pending(HOST)
    repo.observe(HOST, original['id'], observation(
        original, clock, sequence=2,
        roster=[{'user_id': 10, 'username': 'Owner'},
                {'user_id': 11, 'username': 'Friend'}],
        current_map=private_map, queue=[private_map], queue_known=True,
        operation_receipts=[{'operation_id': grant['id'], 'phase': 'applied'}]))
    assert repo.view(original['id'], 11)['role'] == 'controller'

    # Invites live in the portal's separate private browser ledger.
    browser_path = repo.root / 'booking-browser.sqlite3'
    with sqlite3.connect(browser_path) as db:
        db.execute('''CREATE TABLE join_invites(
            token TEXT PRIMARY KEY,booking TEXT NOT NULL,owner INTEGER NOT NULL,
            expires REAL NOT NULL,revoked INTEGER NOT NULL)''')
        db.execute('INSERT INTO join_invites VALUES(?,?,?,?,0)',
                   ('invite-digest', original['id'], 10, clock.value + 1800))
    browser_path.chmod(0o600)

    ended = observation(original, clock, sequence=3, phase='ended', room_id=None,
                        incarnation=None, roster=[], roster_known=True, ready=False,
                        closed=True, cleanup_complete=True, ownership_known=True)
    repo.observe(HOST, original['id'], ended)
    with repo._transaction() as db:
        row = db.execute('SELECT password,lease_envelope,room_id,incarnation,role_data,observation '
                         'FROM bookings WHERE id=?', (original['id'],)).fetchone()
        operations = db.execute('SELECT action,payload,envelope FROM operations '
                                'WHERE booking_id=?', (original['id'],)).fetchall()
        cached, = db.execute('SELECT response FROM requests WHERE booking_id=?',
                             (original['id'],)).fetchone()
    assert row['password'] == '' and row['lease_envelope'] is None
    assert row['room_id'] is None and row['incarnation'] is None and row['role_data'] == '{}'
    observation_data = json.loads(row['observation'])
    assert observation_data['roster'] == []
    assert observation_data['current_map'] is None and observation_data['queue'] == []
    assert all(json.loads(item['payload']) == {} and json.loads(item['envelope']) == {}
               for item in operations)
    assert not any('Private map' in item or 'Friend' in item for item in
                   (row['observation'], row['role_data'], row['lease_envelope'] or ''))
    with sqlite3.connect(browser_path) as db:
        assert db.execute('SELECT COUNT(*) FROM join_invites WHERE booking=?',
                          (original['id'],)).fetchone()[0] == 0
    cached_response = json.loads(cached)
    assert cached_response['id'] == original['id']
    assert cached_response['phase'] == 'ended'
    assert cached_response['password'] is None and cached_response['join_url'] is None
    assert cached_response['players'] == [] and cached_response['current_map'] is None
    assert cached_response['queue'] == []

    retry = repo.reserve(10, request_id, 'slot-a', 'standard-auto', {'sr_min': 2})
    assert retry['id'] == original['id'] and retry['phase'] == 'ended'
    assert retry['password'] is None and retry['join_url'] is None
    assert retry['players'] == [] and retry['current_map'] is None and retry['queue'] == []
    with pytest.raises(NetworkError) as conflict:
        repo.reserve(10, request_id, 'slot-a', 'standard-auto', {'sr_min': 3})
    assert conflict.value.code == 'idempotency_conflict'

    reused = repo.reserve(20, str(uuid.uuid4()), 'slot-a', 'standard-auto', {})
    assert reused['id'] != original['id']
    retry_after_reuse = repo.reserve(10, request_id, 'slot-a', 'standard-auto', {'sr_min': 2})
    assert retry_after_reuse['id'] == original['id']
    assert retry_after_reuse['phase'] == 'ended' and retry_after_reuse['password'] is None


def test_ended_owner_can_read_only_terminal_receipt_after_role_cleanup(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock,
                 phase='ended', room_id=None, incarnation=None, ready=False,
                 closed=True, cleanup_complete=True,
                 roster=[{'user_id': 11, 'username': 'Former guest'}]))
    with repo._transaction() as db:
        db.execute("UPDATE bookings SET role_data='{}' WHERE id=?", (booking['id'],))

    receipt = repo.view(booking['id'], 10)
    assert receipt['phase'] == 'ended' and receipt['role'] == 'owner'
    assert receipt['password'] is None and receipt['join_url'] is None
    assert receipt['room_id'] is None and receipt['players'] == []
    assert receipt['roster_known'] is False
    with pytest.raises(NetworkError) as closed:
        repo.submit(booking['id'], 10, str(uuid.uuid4()), 'start', {}, receipt['revision'])
    assert closed.value.code == 'booking_closed'
    with pytest.raises(NetworkError) as denied:
        repo.view(booking['id'], 11)
    assert denied.value.status == 403


def test_no_show_cooldown_is_requester_scoped_and_reservation_rate_limited(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    repo.configure_slot('slot-b', HOST, 'room2', 'Pool B', ['standard-auto'],
                        {'sr_min': 1, 'sr_max': 5}, enabled=True)
    booking = reserve(repo)
    no_show = observation(booking, clock, phase='ended', room_id=None, closed=True,
                          cleanup_complete=True, terminal_reason='claim_timeout')
    repo.observe(HOST, booking['id'], no_show)

    with pytest.raises(NetworkError) as cooldown:
        repo.reserve(10, str(uuid.uuid4()), 'slot-b', 'standard-auto', {})
    assert cooldown.value.code == 'no_show_cooldown'
    # Cleanup frees room1 for another requester immediately.
    other = repo.reserve(20, str(uuid.uuid4()), 'slot-a', 'standard-auto', {})
    assert other['user_id'] == 20
    with pytest.raises(NetworkError) as still_limited:
        repo.reserve(10, str(uuid.uuid4()), 'slot-a', 'standard-auto', {})
    assert still_limited.value.code == 'no_show_cooldown'


def test_reservation_rate_limited_after_ten_successes_per_minute(tmp_path):
    repo, clock, _ = make_repo(tmp_path, capacity=20)
    for index in range(10):
        booking = repo.reserve(10, str(uuid.uuid4()), 'slot-a', 'standard-auto', {})
        ended = observation(booking, clock, sequence=1, phase='ended', room_id=None,
                            incarnation=None, ready=False, closed=True, cleanup_complete=True)
        repo.observe(HOST, booking['id'], ended)
    with pytest.raises(NetworkError) as limited:
        repo.reserve(10, str(uuid.uuid4()), 'slot-a', 'standard-auto', {})
    assert limited.value.code == 'reservation_rate_limited'



def test_local_command_sequences_are_independent_of_revision_observations(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock))
    first = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'skip', {}, 1)
    # A roster-visible host observation advances the browser revision independently.
    repo.observe(HOST, booking['id'], observation(booking, clock, sequence=2,
                 roster=[{'user_id': 11, 'username': 'friend'}]))
    second = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'start', {}, 3)
    commands = repo.pending(HOST)
    assert [c['sequence'] for c in commands] == [1, 2]
    assert [c['operation_id'] for c in commands] == [first['id'], second['id']]


def test_lost_reply_retry_keeps_immutable_envelope_after_role_change(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock,
                 roster=[{'user_id': 11, 'username': 'friend'}]))
    request_id = str(uuid.uuid4())
    original = repo.submit(booking['id'], 10, request_id, 'skip', {}, 1)
    delivered, = repo.pending(HOST)
    repo.submit(booking['id'], 10, str(uuid.uuid4()), 'grant', {'user_id': 11}, 2)
    replay = repo.submit(booking['id'], 10, request_id, 'skip', {}, 1)
    current = next(c for c in repo.pending(HOST) if c['operation_id'] == original['id'])
    assert replay['id'] == original['id']
    assert replay['command_sequence'] == original['command_sequence'] == 1
    assert current == delivered


def test_cancelled_dispatched_action_late_receipt_is_recorded_without_restoring_role(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock,
                 roster=[{'user_id': 11, 'username': 'friend'}]))
    grant = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'grant', {'user_id': 11}, 1)
    repo.observe(HOST, booking['id'], observation(booking, clock, sequence=2,
                 roster=[{'user_id': 11, 'username': 'friend'}],
                 operation_receipts=[{'operation_id': grant['id'], 'phase': 'applied'}]))
    skip = repo.submit(booking['id'], 11, str(uuid.uuid4()), 'skip', {}, 3)
    repo.pending(HOST)
    repo.submit(booking['id'], 10, str(uuid.uuid4()), 'revoke', {'user_id': 11}, 4)
    late = observation(booking, clock, sequence=3,
                       roster=[{'user_id': 11, 'username': 'friend'}],
                       operation_receipts=[{'operation_id': skip['id'], 'phase': 'applied',
                                            'application': 'live'}])
    repo.observe(HOST, booking['id'], late)
    with pytest.raises(NetworkError) as denied:
        repo.view(booking['id'], 11)
    assert denied.value.status == 403
    op = next(item for item in repo.view(booking['id'], 10)['operations'] if item['id'] == skip['id'])
    assert op['phase'] == 'cancelled'
    conflicting_late = observation(booking, clock, sequence=4,
                                   roster=[{'user_id': 11, 'username': 'friend'}],
                                   operation_receipts=[{'operation_id': skip['id'],
                                                        'phase': 'failed',
                                                        'error': 'provider_failure'}])
    with pytest.raises(NetworkError) as conflict:
        repo.observe(HOST, booking['id'], conflicting_late)
    assert conflict.value.code == 'operation_receipt_conflict'


def test_invalid_late_receipt_does_not_advance_observation_and_error_is_redacted(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock))
    operation = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'skip', {}, 1)
    invalid = observation(booking, clock, sequence=2,
                          operation_receipts=[{'operation_id': operation['id'], 'phase': 'failed',
                                               'error': 'provider token raw material'}])
    with pytest.raises(NetworkError):
        repo.observe(HOST, booking['id'], invalid)
    valid = observation(booking, clock, sequence=2,
                        operation_receipts=[{'operation_id': operation['id'], 'phase': 'failed',
                                             'error': 'provider_failure'}])
    repo.observe(HOST, booking['id'], valid)
    view = repo.view(booking['id'], 10)
    op = next(item for item in view['operations'] if item['id'] == operation['id'])
    assert op['error'] == 'provider_failure'
    assert 'provider token raw material' not in str(view)


def test_end_command_holds_slot_until_closed_owned_cleanup_is_verified(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock))
    ending = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'end', {}, 1)
    assert ending['command_sequence'] == 1
    unresolved = observation(booking, clock, sequence=2, phase='ended', room_id=None,
                             closed=True, cleanup_complete=False)
    repo.observe(HOST, booking['id'], unresolved)
    with pytest.raises(NetworkError):
        repo.reserve(11, str(uuid.uuid4()), 'slot-a', 'standard-auto', {})
    closed = observation(booking, clock, sequence=3, phase='ended', room_id=None,
                         closed=True, cleanup_complete=True,
                         operation_receipts=[{'operation_id': ending['id'], 'phase': 'applied'}])
    repo.observe(HOST, booking['id'], closed)
    assert repo.view(booking['id'], 10)['phase'] == 'ended'
    next_booking = repo.reserve(11, str(uuid.uuid4()), 'slot-a', 'standard-auto', {})
    assert next_booking['user_id'] == 11


def test_top_level_agent_receipts_bind_session_and_apply_grant_without_reviving_revoked_role(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock,
                 roster=[{'user_id': 11, 'username': 'friend'}]))
    grant = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'grant', {'user_id': 11}, 1)
    receipt = {'operation_id': grant['id'], 'booking_id': booking['id'],
               'session_generation': booking['session_generation'], 'alias': 'room1',
               'phase': 'applied'}
    assert repo.observe_receipts(HOST, [receipt]) == {'accepted': 1}
    assert repo.view(booking['id'], 11)['role'] == 'controller'
    assert repo.observe_receipts(HOST, [receipt]) == {'accepted': 0}

    # Revoke is immediately authoritative at the hub. A later host receipt
    # cannot recreate the controller role.
    repo.submit(booking['id'], 10, str(uuid.uuid4()), 'revoke', {'user_id': 11}, 3)
    with pytest.raises(NetworkError) as denied:
        repo.view(booking['id'], 11)
    assert denied.value.status == 403


def test_top_level_receipt_rejects_binding_mismatch_and_ignores_unknown_stale_id(tmp_path):
    repo, clock, _ = make_repo(tmp_path)
    booking = reserve(repo)
    repo.observe(HOST, booking['id'], observation(booking, clock))
    operation = repo.submit(booking['id'], 10, str(uuid.uuid4()), 'skip', {}, 1)
    invalid = {'operation_id': operation['id'], 'booking_id': booking['id'],
               'session_generation': 'b' * 32, 'alias': 'room1', 'phase': 'applied'}
    with pytest.raises(NetworkError) as mismatch:
        repo.observe_receipts(HOST, [invalid])
    assert mismatch.value.code == 'operation_receipt_binding_mismatch'

    stale = {'operation_id': str(uuid.uuid4()), 'booking_id': str(uuid.uuid4()),
             'session_generation': 'c' * 32, 'alias': 'room4', 'phase': 'cancelled'}
    assert repo.observe_receipts(HOST, [stale]) == {'accepted': 0}
    assert next(x for x in repo.view(booking['id'], 10)['operations']
                if x['id'] == operation['id'])['phase'] == 'pending'


def test_public_assignments_include_only_safe_slot_projection(tmp_path):
    repo, _, _ = make_repo(tmp_path)
    assignments = repo.public_assignments()
    assert len(assignments) == 1
    assert set(assignments[0]) == {'host_id', 'alias', 'slot'}
    assert assignments[0]['host_id'] == HOST and assignments[0]['alias'] == 'room1'
    assert set(assignments[0]['slot']) <= {
        'slot_id', 'label', 'state', 'fresh', 'supported_presets', 'reason'}
    assert 'password' not in str(assignments)


def test_contract_observation_matches_repository_envelope_and_rejects_missing_binding():
    clock = Clock()
    booking = {'session_generation': 'a' * 32, 'settings': {'sr_min': 1, 'sr_max': 5}}
    value = observation(booking, clock)
    assert validate_booking_observation(value) is value
    with pytest.raises(NetworkError):
        validate_booking_observation({k: v for k, v in value.items() if k != 'ready'})
    value['claim_deadline'] = clock.value + 600
    assert validate_booking_observation(value) is value
    value['activity'] = {'condition': 'no_gameplay', 'closes_at': clock.value + 1800}
    assert validate_booking_observation(value) is value
    value['activity'] = {'condition': 'unknown', 'closes_at': clock.value + 1800}
    with pytest.raises(NetworkError):
        validate_booking_observation(value)
    del value['activity']
    value['claim_deadline'] = True
    with pytest.raises(NetworkError):
        validate_booking_observation(value)


def test_terminal_reports_can_repeat_with_new_sequences_without_blocking_reuse(tmp_path):
    repo,clock,_=make_repo(tmp_path);booking=reserve(repo)
    ended=observation(booking,clock,phase='ended',room_id=None,incarnation=None,
                      roster_known=False,ready=False,closed=True,cleanup_complete=True,
                      effective_settings={},terminal_reason='host_closed')
    repo.observe(HOST,booking['id'],ended)
    for sequence in range(2,5):
        result=repo.observe(HOST,booking['id'],{**ended,'sequence':sequence})
        assert result['accepted']is True
    assert repo.public_slots()[0]['state']=='available'
    next_booking=repo.reserve(11,str(uuid.uuid4()),'slot-a','standard-auto',{})
    repo.observe(HOST,booking['id'],{**ended,'sequence':5})
    assert repo.view(next_booking['id'],11)['phase']=='preparing'
    with pytest.raises(NetworkError):
        repo.observe(HOST,booking['id'],observation(booking,clock,sequence=6))


def test_late_first_ready_report_keeps_host_claim_deadline_and_settings_version(tmp_path):
    repo,clock,_=make_repo(tmp_path);booking=reserve(repo)
    original_deadline=clock.value+600
    clock.value+=300
    settings={**booking['settings'],'sr_min':3}
    repo.observe(HOST,booking['id'],observation(booking,clock,claim_deadline=original_deadline,
                 settings_revision=1,effective_settings=settings))
    view=repo.view(booking['id'],10)
    assert view['claim_deadline']==original_deadline
    assert view['settings']==settings
    op=repo.submit(booking['id'],10,str(uuid.uuid4()),'configure',{'sr_min':4},view['revision'])
    command=next(c for c in repo.pending(HOST)if c['operation_id']==op['id'])
    assert command['expected_settings_revision']==1
    with pytest.raises(NetworkError):
        repo.observe(HOST,booking['id'],observation(booking,clock,sequence=2,settings_revision=0))


def test_bounded_controls_preserve_priority_end_and_private_queue(tmp_path):
    repo,clock,_=make_repo(tmp_path);booking=reserve(repo)
    private_map={'beatmap_id':123,'label':'Private selection','stars':4.5,'hit_length':120}
    repo.observe(HOST,booking['id'],observation(booking,clock,current_map=private_map,
                 queue=[private_map],queue_known=True))
    view=repo.view(booking['id'],10)
    assert view['current_map']==private_map and view['queue']==[private_map]
    assert b'Private selection'not in canonical(repo.public_slots())
    for _ in range(32):
        view=repo.view(booking['id'],10)
        repo.submit(booking['id'],10,str(uuid.uuid4()),'skip',{},view['revision'])
    view=repo.view(booking['id'],10)
    with pytest.raises(NetworkError)as full:
        repo.submit(booking['id'],10,str(uuid.uuid4()),'skip',{},view['revision'])
    assert full.value.code=='booking_command_queue_full'
    ended=repo.submit(booking['id'],10,str(uuid.uuid4()),'end',{},view['revision'])
    assert ended['phase']=='pending'
