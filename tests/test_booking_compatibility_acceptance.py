"""Bounded startup, private-statistics, and mixed-version booking proofs."""
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import uuid
import zipfile
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from poolrotater_host.bookings.agent import Agent
from poolrotater_host.bookings.local import LocalSessions
from poolrotater_host.bookings.policy import NAME as BOOKING_POLICY
from poolrotater_host.layout import atomic_json, read_json
from poolrotater_host.managed_host.state import State
from poolrotater_host.network.contracts import NetworkError, canonical, secret
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.server import HubServer, tls
from poolrotater_host.network.store import Store
from poolrotater_host.network.transport import Transport
from poolrotater_host.provider_transport import TrustSnapshot
from poolrotater_host.testing.provider_https import certificates
from tests import test_activity_collector as collector_fixture


INSTALLATION = str(uuid.UUID(int=711))
GENERATION = str(uuid.UUID(int=712))
USER_ID = 713


def _managed_state(root, aliases=('room1', 'room2', 'room3', 'room4'), binding=None):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    for name in ('config', 'state', 'state/rolling', 'prefix', 'prefix/managed'):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / name).chmod(0o700)
    binding = binding or {'host_id': str(uuid.uuid4()), 'installation_id': INSTALLATION,
                          'generation': GENERATION}
    config = {'preset': 'standard-fixed', 'visibility': 'private', 'password': 'original-pass',
              'sr_min': 3.5, 'sr_max': 4.5, 'auto_difficulty': False,
              'countdown_seconds': 20, 'loved_mode': False, 'stream_enabled': False}
    state = State(root, **binding)
    state.seed([{'alias': alias, 'desired': 'stopped', 'settings': config}
                for alias in aliases])
    state.policy_applied(1)
    return state, binding, config


def _booking_lease(binding, alias='room1'):
    return {'id': str(uuid.uuid4()), 'user_id': USER_ID, 'designated_friend_id': None,
            **binding, 'alias': alias, 'session_generation': uuid.uuid4().hex,
            'preset': 'standard-fixed', 'settings': {'sr_min': 3.5, 'sr_max': 4.5},
            'password': 'invented-booking-password'}


def _tick_with_startup_mocks(monkeypatch, root, state, booking_leases, *, attempts):
    """Call the real stopped-controller decision with only service IO mocked."""
    from poolrotater_host import pilot_installation, provisioning
    from poolrotater_host.managed_host import operations
    from poolrotater_host.managed_host import catalog_rollout
    from poolrotater_host.managed_host import topology
    from poolrotater_host.managed_host import policy as managed_policy
    from poolrotater_host import pilot_operations
    from poolrotater_host.network import operations as network_operations
    from poolrotater_host.ratings import operations as ratings_operations

    prefix = root / 'prefix'
    target = {'root': root, 'prefix': prefix, 'unit': 'invented.service'}
    marker = {'installation_id': state.binding['installation_id'],
              'generation': state.binding['generation']}
    record = {'service_uid': os.geteuid(), 'bundle_sha256': 'a' * 64}
    monkeypatch.setattr(pilot_installation, 'administrative_binding',
                        lambda instance: (target, marker, record))
    monkeypatch.setattr(pilot_installation, '_stopped', lambda *a, **k: nullcontext())
    monkeypatch.setattr(provisioning, 'protected', lambda path: Path(path))

    def control(path, *args, **kwargs):
        path = Path(path)
        if path == root / managed_policy.NAME:
            return {'policy_version': 1}
        if path.name == 'start-attempt.json' and path.exists():
            return read_json(path)
        return None

    monkeypatch.setattr(provisioning, '_control', control)
    monkeypatch.setattr(provisioning, '_write',
                        lambda path, value, **kwargs: atomic_json(Path(path), value))
    monkeypatch.setattr(operations, '_desired', lambda *a: (state.snapshot(), [], 0))
    monkeypatch.setattr(managed_policy, 'state', lambda root: state)
    monkeypatch.setattr(operations, 'settle_closed', lambda *a: None)
    monkeypatch.setattr(topology, 'pending', lambda *a: False)
    monkeypatch.setattr(catalog_rollout, 'prepare_pending', lambda *a: None)
    monkeypatch.setattr(pilot_operations, '_unit', lambda *a, **k: object())
    monkeypatch.setattr(pilot_operations, '_service_state',
                        lambda *a: {'MainPID': 0, 'ActiveState': 'inactive'})
    monkeypatch.setattr(network_operations, '_checked_unit', lambda *a: None)
    monkeypatch.setattr(network_operations, '_state',
                        lambda *a: {'MainPID': 1, 'ActiveState': 'active'})
    monkeypatch.setattr(ratings_operations, '_identity', lambda *a: nullcontext())
    from poolrotater_host.layout import file_lease
    monkeypatch.setattr(operations, 'file_lease', file_lease)
    monkeypatch.setattr(pilot_operations, 'automatic_start',
                        lambda *a, **k: attempts.append('controller-start') or {'ok': True})
    return operations


def test_stopped_controller_starts_once_for_durable_booking_and_default_off_stays_stopped(
        tmp_path, monkeypatch):
    root = tmp_path / 'with-booking'
    state, binding, config = _managed_state(root)
    atomic_json(root / BOOKING_POLICY,
                {'format': 1, 'enabled': True, 'aliases': ['room1']})
    ledger = LocalSessions(root, binding)
    lease = _booking_lease(binding)
    ledger.install(lease, {'sr_min': 3.5, 'sr_max': 4.5})
    assert all(state.room(f'room{i}')['desired'] == 'stopped' for i in range(1, 5))
    attempts = []
    operations = _tick_with_startup_mocks(monkeypatch, root, state,
                                          [lease['id'] + ':' + lease['session_generation']],
                                          attempts=attempts)
    first = operations.tick('invented')
    second = operations.tick('invented')
    assert first['phase'] == 'controller-started'
    assert first['rooms_started'] is False
    assert first['room_creation_managed_by_controller'] is True
    assert second['phase'] == 'startup-requires-review'
    assert attempts == ['controller-start']
    assert all(state.room(f'room{i}')['desired'] == 'stopped' for i in range(1, 5))
    assert not state.pending()

    off_root = tmp_path / 'default-off'
    off_state, _, _ = _managed_state(off_root)
    attempts_off = []
    off_operations = _tick_with_startup_mocks(monkeypatch, off_root, off_state, [],
                                              attempts=attempts_off)
    result = off_operations.tick('invented')
    assert result == {'ok': True, 'phase': 'intentionally-stopped', 'rooms_started': False}
    assert attempts_off == []


class PrivateBookingWorkerStatisticsAcceptance(unittest.TestCase):
    setUp = collector_fixture.ActivityCollectorTests.setUp
    consent = collector_fixture.ActivityCollectorTests.consent
    presence = collector_fixture.ActivityCollectorTests.presence
    join = collector_fixture.ActivityCollectorTests.join
    events = collector_fixture.ActivityCollectorTests.events

    def test_real_broker_private_visibility_hook_feeds_private_stats_collector(self):
        from rolling.broker import Broker

        broker = Broker.__new__(Broker)
        broker.owner = SimpleNamespace(runtime=str(self.root))
        broker.alias = 'room1'
        broker.generation = uuid.uuid4().hex
        (self.root / 'state').chmod(0o700)
        self.ledger.chmod(0o600)
        broker.cfg = {'_host_root': str(self.root), 'ratings_db': str(self.ledger),
                      'ruleset_id': 0, 'mania_keys': 4}
        broker.reporting_visibility(public=False, room_id=1)
        with sqlite3.connect(self.ledger) as db:
            row = db.execute('''SELECT public,mode,room_alias FROM activity_visibility
                                WHERE room_id=1 ORDER BY observed_at DESC,id DESC LIMIT 1''').fetchone()
        assert row == (0, 'standard', 'room1')

        start = time.time() + 1
        self.now = start
        self.consent()
        self.presence(start=start, end=start + 9)
        self.join(at=start + 2)
        self.now = start + 10
        self.collector.collect()
        events = self.events()
        assert {item['kind'] for item in events} == {'presence', 'join'}
        assert all(item['payload']['room_id'] == 1 for item in events)
        assert 'secret room map' not in json.dumps(events).lower()


class HostClient:
    def __init__(self, root, binding, token, transport, managed_state):
        self.root = root
        self.binding = binding
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
        return {**self.binding, 'credential': self.token, 'credential_expires_at': 10**10}

    def pending(self, profile):
        return None

    def transport(self, profile):
        return self._transport


def _old_hub_process(tmp_path, cert_root):
    work = Path(__file__).resolve().parents[4]
    archive = work / 'validation/catalog-rollout/publication/poolrotater-v7.2.14-runtime.zip'
    assert archive.is_file()
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == \
        'a3430a58f554a84fd5ab327ed2a0f1848149166e0658630a73e822e8f9247ec2'
    payload_root = tmp_path / 'old-v7214'
    payload_root.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if not member.filename.startswith('payload/'):
                continue
            target = (payload_root / member.filename).resolve()
            assert target.is_relative_to(payload_root.resolve())
            if member.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(bundle.read(member))

    ready = tmp_path / 'old-hub-ready.json'
    script = tmp_path / 'old_hub_server.py'
    script.write_text('''
import json, sys, threading, time, uuid
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'payload'))
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.store import Store
from poolrotater_host.ratings.repository import prepare
from poolrotater_host.network.contracts import secret
from poolrotater_host.network.server import HubServer, tls
root, certs, ready = Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
initialize(root)
store = Store(root)
prepare(store)
identity = {'operator_id': str(uuid.uuid4()), 'account_id': 723,
            'installation_id': str(uuid.UUID(int=711)),
            'generation': str(uuid.UUID(int=712)), 'directory': True}
token = secret()
invite = store.invite(identity, label='Invented v7.2.14 host')['invitation']
enrolled = store.enroll({'protocol_version': 1, 'request_id': str(uuid.uuid4()),
                         'invitation': invite, 'credential': token, **identity})
server = HubServer(('127.0.0.1', 0), store,
                   tls_context=tls(certs / 'server.crt', certs / 'server.key'))
with ready.open('w') as output:
    json.dump({'port': server.server_port, 'host_id': enrolled['host_id'],
               'installation_id': identity['installation_id'],
               'generation': identity['generation'], 'token': token}, output)
    output.flush()
ready.chmod(0o600)
server.serve_forever(poll_interval=.02)
''')
    process = subprocess.Popen([sys.executable, str(script), str(payload_root),
                                 str(tmp_path / 'old-hub-state'), str(cert_root), str(ready)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, env={**os.environ,
                                    'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not ready.exists() and process.poll() is None:
        time.sleep(.05)
    if not ready.exists():
        stderr = process.communicate(timeout=2)[1].decode(errors='replace')
        raise AssertionError('v7.2.14 HubServer did not start: ' + stderr[-2000:])
    return process, read_json(ready)


def test_v7214_hub_keeps_legacy_routes_and_refuses_booking_exchange(tmp_path):
    cert_root = tmp_path / 'tls'
    cert_root.mkdir(mode=0o700)
    certificates(cert_root)
    process, binding_info = _old_hub_process(tmp_path, cert_root)
    try:
        transport = Transport(f"https://127.0.0.1:{binding_info['port']}",
                              TrustSnapshot((cert_root / 'ca.crt').read_bytes()), timeout=4)
        binding = {key: binding_info[key]
                   for key in ('host_id', 'installation_id', 'generation')}
        heartbeat = {
            'protocol_version': 1, 'installation_id': binding['installation_id'],
            'generation': binding['generation'], 'sequence': 0,
            'code_version': '7.2.14', 'catalog_version': 'a' * 64, 'rooms': [],
        }
        assert transport.request('POST', '/v1/hosts/self/heartbeat',
                                 body=heartbeat, token=binding_info['token'])['ok'] is True

        # This previously published hub handles the installed managed-room
        # report route, then cleanly reports the unrecognized booking route.
        from poolrotater_host.managed_host import contracts as managed_contracts
        capabilities = {
            'protocol_version': 1, 'policy_version': 1,
            'installation_id': binding['installation_id'],
            'generation': binding['generation'], 'host_id': binding['host_id'],
            'max_concurrent_rooms': 1, 'provider_room_limit': 1, 'capacity_used': 0,
            'rooms': [], 'controller_phase': 'stopped',
        }
        managed_contracts.capabilities(capabilities)
        managed = {'protocol_version': 1, 'installation_id': binding['installation_id'],
                   'generation': binding['generation'], 'sequence': 1,
                   'sent_at': time.time(), 'capabilities': capabilities, 'receipts': []}
        managed_reply = transport.request('POST', '/v1/hosts/self/managed-rooms',
                                          body=managed, token=binding_info['token'])
        assert managed_reply['ok'] is True
        booking_exchange = {
            'protocol_version': 1, **binding, 'sequence': 0, 'sent_at': time.time(),
            'capabilities': {'version': 1, 'enabled_aliases': ['room1'],
                             'supported_presets': ['standard-fixed'],
                             'active_booking_ids': []},
            'observations': [], 'receipts': [],
        }
        with pytest.raises(NetworkError) as unsupported:
            transport.request('POST', '/v1/hosts/self/bookings',
                              body=booking_exchange, token=binding_info['token'])
        assert unsupported.value.status == 404
        assert unsupported.value.code == 'route_not_found'

        host_root = tmp_path / 'new-host'
        state, host_binding, _ = _managed_state(host_root, binding=binding)
        atomic_json(host_root / BOOKING_POLICY,
                    {'format': 1, 'enabled': True, 'aliases': ['room1']})
        from poolrotater_host.bookings.local import LocalSessions
        host_ledger = LocalSessions(host_root, host_binding)
        client = HostClient(host_root, host_binding, binding_info['token'], transport, state)
        receipt = Agent(client, store=host_ledger).step()
        assert receipt['phase'] == 'hub-upgrade-required'
        assert receipt['error'] == 'unsupported-capability'
        assert receipt['live_rooms_affected'] is False
        assert host_ledger.leases() == []
        assert state.limits()['capacity_used'] == 0
        assert all(state.room(f'room{i}')['desired'] == 'stopped' for i in range(1, 5))
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_new_hub_accepts_prior_host_heartbeat_and_managed_report_without_booking_capabilities(
        tmp_path):
    cert_root = tmp_path / 'tls'
    cert_root.mkdir(mode=0o700)
    certificates(cert_root)
    hub_root = tmp_path / 'hub'
    hub_root.mkdir(mode=0o700)
    initialize(hub_root)
    store = Store(hub_root)
    from poolrotater_host.ratings.repository import prepare
    prepare(store)
    operator = {'operator_id': str(uuid.uuid4()), 'account_id': 724,
                'installation_id': str(uuid.UUID(int=711)),
                'generation': str(uuid.UUID(int=712)), 'directory': True}
    token = secret()
    invitation = store.invite(operator, label='Invented prior-version host')['invitation']
    enrolled = store.enroll({'protocol_version': 1, 'request_id': str(uuid.uuid4()),
                             'invitation': invitation, 'credential': token, **operator})
    binding = {'host_id': enrolled['host_id'], 'installation_id': operator['installation_id'],
               'generation': operator['generation']}
    server = HubServer(('127.0.0.1', 0), store,
                       tls_context=tls(cert_root / 'server.crt', cert_root / 'server.key'))
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        transport = Transport(f'https://127.0.0.1:{server.server_port}',
                              TrustSnapshot((cert_root / 'ca.crt').read_bytes()), timeout=4)
        heartbeat = {'protocol_version': 1,
                     'installation_id': binding['installation_id'],
                     'generation': binding['generation'], 'sequence': 0,
                     'code_version': '7.2.14', 'catalog_version': 'b' * 64,
                     'rooms': []}
        assert transport.request('POST', '/v1/hosts/self/heartbeat',
                                 body=heartbeat, token=token)['ok'] is True

        capabilities = {
            'protocol_version': 1, 'policy_version': 1,
            **binding, 'max_concurrent_rooms': 1, 'provider_room_limit': 1,
            'capacity_used': 0, 'rooms': [], 'controller_phase': 'stopped',
        }
        managed = {'protocol_version': 1, **{key: binding[key] for key in
                   ('installation_id', 'generation')}, 'sequence': 1,
                   'sent_at': time.time(), 'capabilities': capabilities, 'receipts': []}
        managed_reply = transport.request('POST', '/v1/hosts/self/managed-rooms',
                                          body=managed, token=token)
        assert managed_reply['ok'] is True

        # A prior host has no booking exchange report/capability. The new hub
        # keeps its ordinary operations available but cannot allocate it a lease.
        from poolrotater_host.bookings.hub import Repository as HubBookingRepository
        ledger = HubBookingRepository(store).ledger
        ledger.configure_slot('legacy-slot', binding['host_id'], 'room1', 'Legacy slot',
                              ['standard-fixed'], {'sr_min': 1, 'sr_max': 10}, enabled=True)
        with pytest.raises(NetworkError) as refused:
            ledger.reserve(9001, str(uuid.uuid4()), 'legacy-slot', 'standard-fixed',
                           {'sr_min': 2, 'sr_max': 8})
        assert refused.value.code == 'host_ineligible'
        assert ledger.active_for_user(9001) is None
        assert ledger.leases(binding['host_id'], binding['installation_id'],
                             binding['generation']) == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
