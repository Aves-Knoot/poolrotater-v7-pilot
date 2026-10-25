"""Per-request fresh booking evidence for authenticated host exchanges."""
import hashlib
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from poolrotater_host.bookings.eligibility import Eligibility
from poolrotater_host.bookings.hub import Repository as HubBookings
from poolrotater_host.diagnostic_logs.common import transaction
from poolrotater_host.managed_host.hub import Repository as ManagedReports
from poolrotater_host.network.contracts import NetworkError, canonical, secret
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.store import Store
from tests.test_booking_eligibility import setup_reports
from tests.test_booking_exchange import Clock, publish_stopped_private_managed_report
from tests.test_network_store import identity


def _current_report(host, payload, received):
    return {'host': host, 'sequence': payload['sequence'],
            'pin': hashlib.sha256(canonical(payload)).hexdigest(),
            'payload': canonical(payload).decode(), 'received': received}


def _exchange(binding, sequence, sent_at):
    return {'protocol_version': 1, **binding, 'sequence': sequence, 'sent_at': sent_at,
            'capabilities': {'version': 1, 'enabled_aliases': ['room1'],
                             'supported_presets': ['standard-fixed'],
                             'active_booking_ids': []},
            'observations': [], 'receipts': []}


def _hub(tmp_path):
    root = tmp_path / 'hub'
    initialize(root)
    clock = Clock(1000.0)
    store = Store(root, clock=clock)
    host_binding = identity()
    token = secret()
    invite = store.invite(host_binding, label='Invented current-report host')['invitation']
    enrolled = store.enroll({'protocol_version': 1, 'request_id': str(uuid.uuid4()),
                             'invitation': invite, 'credential': token, **host_binding})
    binding = {'host_id': enrolled['host_id'],
               'installation_id': host_binding['installation_id'],
               'generation': host_binding['generation']}
    env = {'root': root, 'store': store, 'clock': clock,
           'host_id': enrolled['host_id'], 'binding': binding, 'token': token}
    publish_stopped_private_managed_report(env)
    return env


def test_current_authenticated_report_delivers_pending_end_when_stored_exchange_is_stale(tmp_path):
    env = _hub(tmp_path)
    repo = HubBookings(env['store'])
    first = _exchange(env['binding'], 1, env['clock']())
    assert repo.accept(env['token'], first)['commands'] == []

    ledger = repo.ledger
    ledger.eligibility = lambda host_id, alias: {
        'host_id': host_id, 'installation_id': env['binding']['installation_id'],
        'generation': env['binding']['generation'], 'fresh': True, 'supported': True,
        'effective_limit': 4, 'capacity_used': 0, 'observed_booking_ids': [],
        'supported_presets': ['standard-fixed']}
    shared_eligibility = ledger.eligibility
    ledger.configure_slot('fresh-report-slot', env['host_id'], 'room1', 'Fresh report',
                          ['standard-fixed'], {'sr_min': 0, 'sr_max': 10}, enabled=True)
    booking = ledger.reserve(707, str(uuid.uuid4()), 'fresh-report-slot', 'standard-fixed', {})
    end = ledger.submit(booking['id'], 707, str(uuid.uuid4()), 'end', {}, booking['revision'])

    # BookAgent's inspection can take longer than the report freshness window.
    # The independent managed-room report is refreshed, while the last booking
    # exchange remains 130 seconds old.
    env['clock'].value += 130
    managed = ManagedReports(env['store'])
    with transaction(managed.path) as db:
        db.execute('UPDATE reports SET received=? WHERE host=?',
                   (env['clock'](), env['host_id']))

    current = _exchange(env['binding'], 2, env['clock']())
    reply = repo.accept(env['token'], current)
    assert [item['operation_id'] for item in reply['commands']] == [end['id']]
    assert reply['commands'][0]['action'] == 'end'

    # Same-sequence replay still returns the durable, byte-equivalent response.
    assert repo.accept(env['token'], current) == reply
    # The shared repository remains DB-backed; current evidence was request-local.
    assert repo.ledger.eligibility is shared_eligibility


@pytest.mark.parametrize('change', ['foreign_binding', 'revoked', 'stale', 'bad_pin', 'malformed'])
def test_current_report_override_cannot_bypass_binding_freshness_or_pin(tmp_path, change):
    store, _, _ = setup_reports(tmp_path, booking_age=121)
    now = store.clock()
    with sqlite3.connect(store.root / 'bookings-hosts.sqlite3') as db:
        payload = json.loads(db.execute('SELECT payload FROM reports WHERE host=?',
                                        ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',)).fetchone()[0])
    payload['sent_at'] = now
    row = _current_report('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', payload, now)
    if change == 'foreign_binding':
        payload['installation_id'] = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
        row = _current_report(row['host'], payload, now)
    elif change == 'stale':
        payload['sent_at'] = now - 121
        row = _current_report(row['host'], payload, now - 121)
    elif change == 'bad_pin':
        row['pin'] = 'f' * 64
    elif change == 'malformed':
        row.pop('payload')
    elif change == 'revoked':
        with sqlite3.connect(store.path) as db:
            db.execute('UPDATE hosts SET revoked=1 WHERE id=?', (row['host'],))

    reader = Eligibility(store, clock=store.clock, current_booking_report=row)
    with pytest.raises(NetworkError):
        reader(row['host'], 'room1')


def test_request_local_fresh_report_does_not_refresh_legacy_database_eligibility_concurrently(tmp_path):
    store, default_reader, _ = setup_reports(tmp_path, booking_age=121)
    now = store.clock()
    with sqlite3.connect(store.root / 'bookings-hosts.sqlite3') as db:
        payload = json.loads(db.execute('SELECT payload FROM reports WHERE host=?',
                                        ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',)).fetchone()[0])
    payload['sent_at'] = now
    current_reader = Eligibility(store, clock=store.clock,
                                 current_booking_report=_current_report(
                                     'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', payload, now))

    def read(reader):
        try:
            return reader('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'room1')['fresh']
        except NetworkError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        current, legacy = list(pool.map(read, (current_reader, default_reader)))
    assert current is True
    assert legacy is False
    # Neither Eligibility object mutates its peer or the stored report.
    assert default_reader.current_booking_report is None
    assert current_reader.current_booking_report['received'] == now
    with sqlite3.connect(store.root / 'bookings-hosts.sqlite3') as db:
        assert db.execute('SELECT received FROM reports').fetchone()[0] == now - 121


def test_default_eligibility_remains_database_backed_and_fails_when_report_is_stale(tmp_path):
    store, reader, _ = setup_reports(tmp_path, booking_age=121)
    with pytest.raises(NetworkError):
        reader('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'room1')
    assert reader.current_booking_report is None
