"""Read-only eligibility tests using isolated temporary SQLite reports."""
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from poolrotater_host.bookings.eligibility import Eligibility
from poolrotater_host.network.contracts import NetworkError, canonical

HOST = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
INSTALL = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
GENERATION = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'


class Clock:
    value = 10_000.0
    def __call__(self):
        return self.value


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'network.sqlite3'
        self.clock = Clock()


def _db(path, script, rows):
    db = sqlite3.connect(path)
    db.executescript(script)
    for sql, args in rows:
        db.execute(sql, args)
    db.commit()
    db.close()
    path.chmod(0o600)


def setup_reports(tmp_path, *, native_age=0, booking_age=0, sent_delta=0,
                  revoked=0, host_generation=GENERATION, native_phase='stopped',
                  desired='stopped', visibility='private', pending=None, failure=None,
                  controller_phase='running', capacity_used=1, sibling_rooms=(),
                  active_ids=(), observations=(), enabled_aliases=('room1',)):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    store = Store(root)
    now = store.clock.value
    _db(store.path, 'CREATE TABLE hosts(id TEXT PRIMARY KEY,installation_id TEXT,generation TEXT,revoked INTEGER)', [
        ('INSERT INTO hosts VALUES(?,?,?,?)', (HOST, INSTALL, host_generation, revoked))])

    rooms = [{'alias': 'room1', 'desired': desired, 'revision': 1, 'applied_revision': 1,
              'phase': native_phase, 'room_id': 321 if native_phase == 'running' else None,
              'room_generation': 'dddddddddddddddddddddddddddddddd' if native_phase == 'running' else None,
              'preset': 'standard-auto', 'visibility': visibility,
              'pending_operation': pending, 'failure_code': failure}, *sibling_rooms]
    native_caps = {
        'protocol_version': 1, 'policy_version': 1, 'installation_id': INSTALL,
        'generation': GENERATION, 'host_id': HOST, 'max_concurrent_rooms': 4,
        'provider_room_limit': 3, 'capacity_used': capacity_used,
        'rooms': rooms, 'controller_phase': controller_phase,
    }
    _db(root / 'managed-rooms.sqlite3',
        'CREATE TABLE reports(host TEXT PRIMARY KEY,sequence INTEGER,pin TEXT,capabilities TEXT,received REAL,response TEXT)', [
            ('INSERT INTO reports VALUES(?,?,?,?,?,?)', (HOST, 5, 'native-pin', json.dumps(native_caps), now - native_age, '{}'))])

    booking_payload = {
        'protocol_version': 1, 'host_id': HOST, 'installation_id': INSTALL,
        'generation': GENERATION, 'sequence': 7, 'sent_at': now - sent_delta,
        'capabilities': {'version': 1, 'enabled_aliases': list(enabled_aliases),
                         'supported_presets': ['standard-auto', 'standard-loved'],
                         'active_booking_ids': list(active_ids)},
        'observations': list(observations), 'receipts': [],
    }
    pin = hashlib.sha256(canonical(booking_payload)).hexdigest()
    _db(root / 'bookings-hosts.sqlite3',
        'CREATE TABLE reports(host TEXT PRIMARY KEY,sequence INTEGER,pin TEXT,payload TEXT,received REAL,response TEXT)', [
            ('INSERT INTO reports VALUES(?,?,?,?,?,?)', (HOST, 7, pin, canonical(booking_payload).decode(), now - booking_age, '{}'))])
    return store, Eligibility(store, clock=store.clock), root


def test_stopped_explicit_alias_returns_native_quota_and_preset_allowlist(tmp_path):
    _, reader, _ = setup_reports(tmp_path, controller_phase='stopped', capacity_used=0)
    result = reader(HOST, 'room1')
    assert result['supported'] and result['fresh']
    assert result['effective_limit'] == 3
    assert result['capacity_used'] == 0
    assert result['observed_booking_ids'] == []
    assert result['supported_presets'] == ['standard-auto', 'standard-loved']


@pytest.mark.parametrize('kwargs', [
    {'native_age': 121}, {'booking_age': 121}, {'sent_delta': -1},
    {'revoked': 1}, {'host_generation': 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'},
])
def test_stale_future_revoked_or_unsafe_reports_fail_closed(tmp_path, kwargs):
    _, reader, _ = setup_reports(tmp_path, **kwargs)
    with pytest.raises(NetworkError):
        reader(HOST, 'room1')


@pytest.mark.parametrize('kwargs', [
    {'enabled_aliases': ()}, {'pending': 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'},
    {'failure': 'controller_unavailable'},
])
def test_disabled_pending_or_failed_alias_is_unsupported(tmp_path, kwargs):
    _, reader, _ = setup_reports(tmp_path, **kwargs)
    result = reader(HOST, 'room1')
    assert result['fresh'] is True and result['supported'] is False


def test_running_private_room_requires_its_matching_active_booking_lease(tmp_path):
    booking_id = str(uuid.uuid4())
    observation = {'booking_id': booking_id, 'observation': {
        'phase': 'active', 'alias': 'room1', 'room_id': 321,
        'ownership_known': True, 'cleanup_complete': False,
    }}
    _, reader, _ = setup_reports(tmp_path, native_phase='running', desired='running',
                                  visibility='private', active_ids=(booking_id,), observations=(observation,))
    result = reader(HOST, 'room1')
    assert result['supported'] and result['observed_booking_ids'] == [booking_id]


def test_public_running_room_cannot_be_taken_over_and_missing_database_is_read_only(tmp_path):
    _, reader, _ = setup_reports(tmp_path, native_phase='running', desired='running', visibility='public')
    assert reader(HOST, 'room1')['supported'] is False

    missing_root = tmp_path / 'missing'
    missing_root.mkdir(mode=0o700)
    store = Store(missing_root)
    before = set(missing_root.iterdir())
    with pytest.raises(NetworkError):
        Eligibility(store, clock=store.clock)(HOST, 'room1')
    assert set(missing_root.iterdir()) == before


def test_stopped_private_controller_is_eligible_only_when_all_rooms_are_closed(tmp_path):
    closed_sibling = {
        'alias': 'room2', 'desired': 'stopped', 'revision': 0, 'applied_revision': 0,
        'phase': 'unobserved', 'room_id': None, 'room_generation': None,
        'preset': 'standard-auto', 'visibility': 'public',
        'pending_operation': None, 'failure_code': None,
    }
    _, reader, _ = setup_reports(tmp_path, controller_phase='stopped', capacity_used=0,
                                 sibling_rooms=(closed_sibling,))
    assert reader(HOST, 'room1')['supported'] is True


@pytest.mark.parametrize('kwargs', [
    {'controller_phase': 'failed'}, {'controller_phase': 'starting'},
    {'controller_phase': 'unavailable'},
    {'capacity_used': 1},
    {'sibling_rooms': ({'alias': 'room2', 'desired': 'running', 'revision': 1,
                        'applied_revision': 1, 'phase': 'running', 'room_id': 322,
                        'room_generation': 'e' * 32, 'preset': 'standard-auto',
                        'visibility': 'private', 'pending_operation': None,
                        'failure_code': None},)},
    {'sibling_rooms': ({'alias': 'room2', 'desired': 'stopped', 'revision': 1,
                        'applied_revision': 1, 'phase': 'stopped', 'room_id': None,
                        'room_generation': None, 'preset': 'standard-auto',
                        'visibility': 'private',
                        'pending_operation': 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
                        'failure_code': None},)},
    {'sibling_rooms': ({'alias': 'room2', 'desired': 'stopped', 'revision': 1,
                        'applied_revision': 1, 'phase': 'uncertain', 'room_id': None,
                        'room_generation': None, 'preset': 'standard-auto',
                        'visibility': 'private', 'pending_operation': None,
                        'failure_code': 'room_ownership_requires_review'},)},
])
def test_stopped_controller_with_uncertain_global_room_state_is_ineligible(tmp_path, kwargs):
    kwargs.setdefault('controller_phase', 'stopped')
    kwargs.setdefault('capacity_used', 0)
    _, reader, _ = setup_reports(tmp_path, **kwargs)
    assert reader(HOST, 'room1')['supported'] is False


def test_stopped_public_target_alias_is_not_eligible_for_booking(tmp_path):
    _, reader, _ = setup_reports(tmp_path, controller_phase='stopped', capacity_used=0,
                                 visibility='public')
    assert reader(HOST, 'room1')['supported'] is False
