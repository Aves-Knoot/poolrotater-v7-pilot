"""Real isolated TLS tests for the authenticated booking host exchange."""
import copy
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from poolrotater_host.bookings.agent import Agent
from poolrotater_host.bookings.local import LocalSessions
from poolrotater_host.diagnostic_logs.common import transaction
from poolrotater_host.layout import atomic_json, read_json
from poolrotater_host.network.contracts import NetworkError, canonical, secret
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.server import HubServer, tls
from poolrotater_host.network.store import Store
from poolrotater_host.network.transport import Transport
from poolrotater_host.provider_transport import TrustSnapshot
from poolrotater_host.testing.provider_https import certificates
from tests.test_network_store import identity


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class ManagedState:
    def __init__(self, binding):
        self.binding = copy.deepcopy(binding)
        self.current = {'phase': 'stopped', 'desired': 'stopped', 'room_id': None,
                        'pending': None, 'failure': None,
                        'config': {'preset': 'standard-fixed', 'visibility': 'private',
                                   'sr_min': 0, 'sr_max': 10}}

    def room(self, alias, *, private=False):
        assert alias == 'room1' and private is True
        return copy.deepcopy(self.current)


class HostClient:
    def __init__(self, root, binding, host_id, token, transport, managed):
        self.root = Path(root)
        self.binding = copy.deepcopy(binding)
        self.host_id = host_id
        self.token = token
        self._transport = transport
        self.managed_state = managed
        self.supported_presets = ['standard-fixed']

    def status(self):
        return {'configured': True, 'enrolled': True,
                'account_binding_matches': True, 'pending_operation': None}

    def profile(self):
        return {'origin': 'https://127.0.0.1'}

    def credential(self):
        return {**self.binding, 'host_id': self.host_id, 'credential': self.token,
                'credential_expires_at': 10**9}

    def pending(self, profile):
        return None

    def transport(self, profile):
        return self._transport


@pytest.fixture(scope='module')
def tls_material():
    temporary = tempfile.TemporaryDirectory()
    folder = Path(temporary.name)
    certificates(folder)
    yield folder, TrustSnapshot((folder / 'ca.crt').read_bytes())
    temporary.cleanup()


@pytest.fixture
def hub(tmp_path, tls_material):
    folder, trust = tls_material
    clock = Clock()
    root = tmp_path / 'hub'
    initialize(root)
    store = Store(root, clock=clock)
    binding = identity()
    token = secret()
    invite = store.invite(binding, label='Invented test host')['invitation']
    enrolled = store.enroll({'protocol_version': 1, 'request_id': str(uuid.uuid4()),
                             'invitation': invite, 'credential': token, **binding})
    server = HubServer(('127.0.0.1', 0), store,
                       tls_context=tls(folder / 'server.crt', folder / 'server.key'))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    transport = Transport(f'https://127.0.0.1:{server.server_port}', trust, timeout=4)
    try:
        yield {'root': root, 'store': store, 'binding': {
            'host_id': enrolled['host_id'],
            **{key: binding[key] for key in ('installation_id', 'generation')}},
            'host_id': enrolled['host_id'], 'token': token, 'clock': clock,
            'transport': transport, 'server': server}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def setup_agent_root(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / 'state').mkdir(mode=0o700, exist_ok=True)
    (root / 'config').mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    (root / 'state').chmod(0o700)
    (root / 'config').chmod(0o700)
    atomic_json(root / 'config/private-bookings.json',
                {'format': 1, 'enabled': True, 'aliases': ['room1']})


def exchange_body(binding, sequence, sent_at):
    return {'protocol_version': 1, **binding, 'sequence': sequence, 'sent_at': sent_at,
            'capabilities': {'version': 1, 'enabled_aliases': ['room1'],
                             'supported_presets': ['standard-fixed'], 'active_booking_ids': []},
            'observations': [], 'receipts': []}


def publish_stopped_private_managed_report(hub):
    """Seed the authenticated-report projection for the isolated native fake."""
    from poolrotater_host.managed_host.hub import Repository as ManagedRepository
    capabilities = {
        'protocol_version': 1, 'policy_version': 1,
        'installation_id': hub['binding']['installation_id'],
        'generation': hub['binding']['generation'], 'host_id': hub['host_id'],
        'max_concurrent_rooms': 4, 'provider_room_limit': 4, 'capacity_used': 0,
        'rooms': [{'alias': 'room1', 'desired': 'stopped', 'revision': 0,
                   'applied_revision': 0, 'phase': 'stopped', 'room_id': None,
                   'room_generation': None, 'preset': 'standard-fixed',
                   'visibility': 'private', 'pending_operation': None,
                   'failure_code': None}],
        'controller_phase': 'running',
    }
    payload = {
        'protocol_version': 1,
        'installation_id': hub['binding']['installation_id'],
        'generation': hub['binding']['generation'],
        'sequence': 1, 'sent_at': hub['clock'](),
        'capabilities': capabilities, 'receipts': [],
    }
    repository = ManagedRepository(hub['store'])
    from poolrotater_host.managed_host.contracts import digest
    response = {'ok': True, 'sequence': 1, 'requests': []}
    with transaction(repository.path) as db:
        db.execute('INSERT OR REPLACE INTO reports VALUES(?,?,?,?,?,?)', (
            hub['host_id'], 1, digest(payload), canonical(capabilities).decode(),
            hub['clock'](), canonical(response).decode()))
    return response


def test_tls_route_authenticates_host_binding_and_rejects_wrong_generation(hub):
    body = exchange_body(hub['binding'], 1, hub['clock']())
    result = hub['transport'].request('POST', '/v1/hosts/self/bookings',
                                      body=body, token=hub['token'])
    assert result == {'ok': True, 'sequence': 1, 'leases': [], 'commands': []}

    with pytest.raises(NetworkError) as unauthenticated:
        hub['transport'].request('POST', '/v1/hosts/self/bookings', body=body,
                                 token=secret())
    assert unauthenticated.value.status in (401, 403)

    wrong = {**body, 'sequence': 2, 'generation': str(uuid.uuid4())}
    with pytest.raises(NetworkError) as mismatch:
        hub['transport'].request('POST', '/v1/hosts/self/bookings',
                                 body=wrong, token=hub['token'])
    assert mismatch.value.code == 'host_identity_mismatch'


def test_lost_reply_replays_durable_response_after_expiry_then_stale_body_resequences(hub, tmp_path):
    host_root = tmp_path / 'host'
    setup_agent_root(host_root)
    managed = ManagedState({'host_id': hub['host_id'], **hub['binding']})
    sent = []
    replies = []

    class LoseFirstResponse:
        def request(self, method, path, *, body, token):
            sent.append(copy.deepcopy(body))
            response = hub['transport'].request(method, path, body=body, token=token)
            replies.append(copy.deepcopy(response))
            if len(sent) == 1:
                raise NetworkError('hub_transport_failed', 503)
            return response

    client = HostClient(host_root, {'host_id': hub['host_id'], **hub['binding']},
                        hub['host_id'], hub['token'], LoseFirstResponse(), managed)
    local = LocalSessions(host_root, managed.binding, clock=hub['clock'])
    agent = Agent(client, store=local, clock=hub['clock'])

    assert agent.step()['phase'] == 'retrying'
    persisted = read_json(agent.path)
    assert persisted['pending'] == sent[0]

    # Change hub booking/operation state after the accepted request lost its
    # response. Replaying that exact exchange must return the original empty
    # response, not a newly computed lease or command list.
    from poolrotater_host.bookings.hub import Repository as HubBookingRepository
    ledger = HubBookingRepository(hub['store']).ledger
    ledger.eligibility = lambda host_id, alias: {
        'host_id': host_id, 'installation_id': hub['binding']['installation_id'],
        'generation': hub['binding']['generation'], 'fresh': True, 'supported': True,
        'effective_limit': 4, 'capacity_used': 0, 'observed_booking_ids': [],
        'supported_presets': ['standard-fixed']}
    ledger.configure_slot('cached-slot', hub['host_id'], 'room1', 'Cached slot',
                          ['standard-fixed'], {'sr_min': 1, 'sr_max': 10}, enabled=True)
    changed = ledger.reserve(1001, str(uuid.uuid4()), 'cached-slot', 'standard-fixed',
                             {'sr_min': 2, 'sr_max': 8})
    ledger.submit(changed['id'], 1001, str(uuid.uuid4()), 'start', {}, 0)

    hub['clock'].value += 301
    assert agent.step()['phase'] == 'connected'
    assert sent[1] == sent[0]
    assert replies[1] == replies[0]
    assert replies[1]['leases'] == [] and replies[1]['commands'] == []
    assert local.leases() == []

    # A different, stale sequence is rejected explicitly; the agent clears
    # only that pending exchange and sends a fresh sequence on the next step.
    stale = exchange_body(client.binding, 1, 1000)
    atomic_json(agent.path, {'sequence': 2, 'pending': stale})
    assert agent.step()['phase'] == 'retrying'
    assert read_json(agent.path) == {'sequence': 2, 'pending': None}
    assert agent.step()['phase'] == 'connected'
    assert sent[-2]['sequence'] == 1 and sent[-1]['sequence'] == 2
    assert sent[-1]['sent_at'] == hub['clock']()
    assert [row['lease']['id'] for row in local.leases()] == [changed['id']]


def test_tls_hub_lease_installs_into_private_host_ledger_without_native_calls(hub, tmp_path):
    host_root = tmp_path / 'host'
    setup_agent_root(host_root)
    managed = ManagedState({'host_id': hub['host_id'], **hub['binding']})
    client = HostClient(host_root, managed.binding, hub['host_id'], hub['token'],
                        hub['transport'], managed)
    local = LocalSessions(host_root, managed.binding, clock=hub['clock'])
    agent = Agent(client, store=local, clock=hub['clock'])

    # First exchange records authenticated host capabilities. A fake native
    # eligibility seam then creates a reservation without contacting a provider.
    assert agent.step()['phase'] == 'connected'
    from poolrotater_host.bookings.hub import Repository as HubBookingRepository
    ledger = HubBookingRepository(hub['store']).ledger
    ledger.eligibility = lambda host_id, alias: {
        'host_id': host_id, 'installation_id': hub['binding']['installation_id'],
        'generation': hub['binding']['generation'], 'fresh': True, 'supported': True,
        'effective_limit': 4, 'capacity_used': 0, 'observed_booking_ids': [],
        'supported_presets': ['standard-fixed']}
    ledger.configure_slot('integration-slot', hub['host_id'], 'room1', 'Integration room',
                          ['standard-fixed'], {'sr_min': 1, 'sr_max': 10}, enabled=True)
    request_id = str(uuid.uuid4())
    booking = ledger.reserve(1001, request_id, 'integration-slot', 'standard-fixed',
                             {'sr_min': 2, 'sr_max': 8})

    assert agent.step()['phase'] == 'connected'
    installed = local.get(booking['id'])
    assert installed['lease']['password'] == booking['password']
    assert installed['lease']['session_generation'] == booking['session_generation']
    assert installed['settings']['sr_min'] == 2
    assert managed.current['desired'] == 'stopped' and managed.current['room_id'] is None


def test_tls_exchange_repeats_old_cleanup_after_alias_reuse(hub, tmp_path):
    """Historical ended reports remain idempotent after a slot gets a new lease."""
    host_root = tmp_path / 'host'
    setup_agent_root(host_root)
    managed = ManagedState({'host_id': hub['host_id'], **hub['binding']})
    client = HostClient(host_root, managed.binding, hub['host_id'], hub['token'],
                        hub['transport'], managed)
    local = LocalSessions(host_root, managed.binding, clock=hub['clock'])
    agent = Agent(client, store=local, clock=hub['clock'])

    assert agent.step()['phase'] == 'connected'
    publish_stopped_private_managed_report(hub)
    from poolrotater_host.bookings.hub import Repository as HubBookingRepository
    ledger = HubBookingRepository(hub['store']).ledger
    ledger.eligibility = lambda host_id, alias: {
        'host_id': host_id, 'installation_id': hub['binding']['installation_id'],
        'generation': hub['binding']['generation'], 'fresh': True, 'supported': True,
        'effective_limit': 4, 'capacity_used': 0, 'observed_booking_ids': [],
        'supported_presets': ['standard-fixed']}
    ledger.configure_slot('reuse-slot', hub['host_id'], 'room1', 'Reuse slot',
                          ['standard-fixed'], {'sr_min': 1, 'sr_max': 10}, enabled=True)
    first = ledger.reserve(1001, str(uuid.uuid4()), 'reuse-slot', 'standard-fixed',
                           {'sr_min': 2, 'sr_max': 8})
    assert agent.step()['phase'] == 'connected'  # host installs first lease

    view = ledger.view(first['id'], 1001)
    end_operation = ledger.submit(first['id'], 1001, str(uuid.uuid4()), 'end', {},
                                  view['revision'])
    assert agent.step()['phase'] == 'connected'  # authenticated hub sends End Session
    command, = local.pending(first['id'])
    assert command['operation_id'] == end_operation['id']
    assert command['action'] == 'end'
    local.acknowledge(command['operation_id'], 'applied',
                      application={'application': 'live'})
    local.advance(first['id'], {
        'ready': False, 'room_id': None, 'incarnation': None,
        'roster_known': True, 'players': [], 'match': None,
        'closed': True, 'ownership_known': True, 'updated_at': hub['clock'](),
    })
    local.complete_cleanup(first['id'])

    # Hub observes confirmed cleanup and releases the alias.
    assert agent.step()['phase'] == 'connected'
    assert ledger.public_slots()[0]['state'] == 'available'
    second = ledger.reserve(1002, str(uuid.uuid4()), 'reuse-slot', 'standard-fixed',
                            {'sr_min': 3, 'sr_max': 7})

    # The Agent's next exchange repeats first's terminal history while it
    # installs the new lease. A later sequence repeats that same old report;
    # neither report may close or poison the second reservation.
    assert agent.step()['phase'] == 'connected'
    assert local.get(second['id'])['lease']['id'] == second['id']
    assert agent.step()['phase'] == 'connected'
    assert ledger.view(second['id'], 1002)['phase'] == 'preparing'
    assert ledger.public_slots()[0]['state'] in ('preparing', 'reserved')


def test_no_policy_is_inert_and_legacy_unknown_route_maps_to_upgrade_required(tmp_path):
    root = tmp_path / 'host'
    (root / 'state').mkdir(parents=True, mode=0o700)
    (root / 'config').mkdir(mode=0o700)
    root.chmod(0o700)
    (root / 'state').chmod(0o700)
    (root / 'config').chmod(0o700)
    binding = {'host_id': str(uuid.uuid4()), 'installation_id': str(uuid.uuid4()),
               'generation': str(uuid.uuid4())}

    class NoNetwork(HostClient):
        def transport(self, profile):
            raise AssertionError('disabled agent attempted network exchange')

    client = NoNetwork(root, binding, binding['host_id'], secret(), None, ManagedState(binding))
    assert Agent(client, clock=Clock()).step()['phase'] == 'disabled'

    setup_agent_root(root)

    class OldHub:
        def request(self, *args, **kwargs):
            raise NetworkError('route_not_found', 404)

    legacy = HostClient(root, binding, binding['host_id'], secret(), OldHub(), ManagedState(binding))
    result = Agent(legacy, clock=Clock()).step()
    assert result['phase'] == 'hub-upgrade-required'
    assert result['live_rooms_affected'] is False


def test_tls_cached_creation_lease_is_retired_only_after_confirmed_cleanup(hub, tmp_path):
    import json
    from poolrotater_host.bookings.hub import Repository as HubBookingRepository
    host_root = tmp_path / 'host'
    setup_agent_root(host_root)
    managed = ManagedState({'host_id': hub['host_id'], **hub['binding']})
    client = HostClient(host_root, managed.binding, hub['host_id'], hub['token'],
                        hub['transport'], managed)
    local = LocalSessions(host_root, managed.binding, clock=hub['clock'])
    agent = Agent(client, store=local, clock=hub['clock'])
    assert agent.step()['phase'] == 'connected'
    publish_stopped_private_managed_report(hub)
    repository = HubBookingRepository(hub['store'])
    ledger = repository.ledger
    ledger.eligibility = lambda host_id, alias: {
        **managed.binding, 'fresh': True, 'supported': True,
        'effective_limit': 4, 'capacity_used': 0, 'observed_booking_ids': [],
        'supported_presets': ['standard-fixed']}
    ledger.configure_slot('retired-slot', hub['host_id'], 'room1', 'Private slot',
                          ['standard-fixed'], {'sr_min': 1, 'sr_max': 10}, enabled=True)
    booking = ledger.reserve(1001, str(uuid.uuid4()), 'retired-slot', 'standard-fixed', {})
    assert agent.step()['phase'] == 'connected'
    with transaction(repository.path) as db:
        report = dict(db.execute('SELECT * FROM reports WHERE host=?', (hub['host_id'],)).fetchone())
    assert booking['password'] in report['response']
    accepted = json.loads(report['payload'])
    replay = hub['transport'].request('POST', '/v1/hosts/self/bookings',
                                       body=accepted, token=hub['token'])
    assert replay['leases'][0]['password'] == booking['password']

    terminal = {
        'sequence': 1, 'phase': 'ended', 'room_id': None, 'incarnation': None,
        'roster': [], 'roster_known': True, 'updated_at': hub['clock'](),
        'ready': False, 'closed': True, 'effective_settings': {},
        'cleanup_complete': False, 'ownership_known': True, 'operation_receipts': [],
        'installation_id':hub['binding']['installation_id'], 'generation':hub['binding']['generation'],
        'session_generation': booking['session_generation'], 'alias': 'room1'}
    # An unconfirmed cleanup must retain both capacity and exact replay material.
    ledger.observe(hub['host_id'], booking['id'], terminal)
    assert hub['transport'].request('POST', '/v1/hosts/self/bookings',
                                    body=accepted, token=hub['token']) == replay
    terminal.update(sequence=2, cleanup_complete=True)
    ledger.observe(hub['host_id'], booking['id'], terminal)
    retired = hub['transport'].request('POST', '/v1/hosts/self/bookings',
                                       body=accepted, token=hub['token'])
    assert retired == {**replay, 'leases': [], 'commands': []}
    with transaction(repository.path) as db:
        stored = db.execute('SELECT response FROM reports WHERE host=?', (hub['host_id'],)).fetchone()['response']
    assert booking['password'] not in stored
