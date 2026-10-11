"""Protected provider-room driver; only already-authorized invented installations."""
from pathlib import Path
import time

from ..config import ConfigError
from ..layout import read_json
from ..ownership_recovery import digest
from ..provider_room_recovery import ProviderOwnershipRecovery, ProviderOwnershipReview
from ..provider_rooms import ProviderRoomApi, ProviderRoomHub, provider_host_controller
from .installed_operator_workflow import authority
from .installed_authorization import installed_provider_fixture_authentication
from .room_service import InventedRoomService

SCENARIOS = ('run', 'reconnect', 'lost-create', 'lost-close-recover')


def run(root, release, *, scenario):
    if scenario not in SCENARIOS: raise ConfigError('invalid invented room scenario')
    root = Path(root)
    workers = []
    with authority(root, release, initialize=False) as (auth, provider):
        backend = InventedRoomService(provider)
        api, hub = ProviderRoomApi(auth, backend), ProviderRoomHub(auth, backend)
        if scenario == 'lost-create': backend.fault('MakeRoom', 'after')
        failed = False
        try:
            with provider_host_controller(root, release, authentication=auth, api=api, hub=hub) as supervisor:
                aliases = list(supervisor.configs) if scenario == 'run' else [next(iter(supervisor.configs))]
                for alias in aliases:
                    worker = supervisor.prepare(alias, release); workers.append(worker)
                    supervisor.activate(worker)
                if scenario == 'reconnect':
                    from bot.hub import HubDisconnected
                    worker, = workers
                    hub.reconnect()
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        try:
                            worker.broker.handle('hub.invoke', 'JoinRoom', [worker.status['room_id']],
                                                 timeout=min(5, deadline-time.monotonic()))
                        except HubDisconnected:
                            time.sleep(.02)
                            continue
                        with supervisor.shared.lock:
                            progress = worker.broker.hub.roster_progress()
                            if (worker.broker.known and worker.broker.hub.is_connected()
                                    and worker.broker.snapshot_generation == hub._connect_generation
                                    and not progress[2]):
                                break
                        time.sleep(.02)
                    else:
                        raise ConfigError('invented reconnect fresh roster deadline')
                if scenario == 'lost-close-recover': backend.fault('CloseRoom', 'after')
        except ConfigError:
            if scenario not in ('lost-create', 'lost-close-recover'): raise
            failed = True
        if any(worker.process.poll() is None for worker in workers):
            raise ConfigError('invented workers not reaped')
        if scenario in ('lost-create', 'lost-close-recover') and not failed:
            raise ConfigError('invented uncertain outcome was silently accepted')
        recovery_applied = False
        if scenario == 'lost-close-recover':
            # Reconstruct the installed authority, with the same actual TLS
            # service still supplying direct evidence of this invented close.
            rebuilt = installed_provider_fixture_authentication(root, release,
                'https://127.0.0.1:' + str(provider.server.server_port), auth.transport.trust)
            result = ProviderOwnershipRecovery(rebuilt).apply(expected_journal_sha256=digest(
                read_json(root/'state/rolling/ownership.json')))
            recovery_applied = result['recovery_applied']
            second = InventedRoomService(provider)
            with provider_host_controller(root, release, authentication=rebuilt,
                    api=ProviderRoomApi(rebuilt, second), hub=ProviderRoomHub(rebuilt, second)) as supervisor:
                if supervisor.handles: raise ConfigError('recovery started workers')
        review = ProviderOwnershipReview(auth).inspect()
        return {'format': 1, 'execution': 'provider-fixture', 'scenario': scenario,
                'fixture_workers_started': len(workers), 'workers_reaped': True,
                'make_calls': backend.authenticated_calls.count('MakeRoom'),
                'close_calls': backend.authenticated_calls.count('CloseRoom'),
                'reconnects': backend.authenticated_calls.count('reconnect'),
                'referee_tls_websocket_connections': backend.wire.connections,
                'referee_wire_commands': len(backend.wire.commands),
                'referee_child_reaped': hub.client._session is None,
                'unknown_creation': any(row['result'] == 'unknown_creation' for row in review['rooms']),
                'recovery_applied': recovery_applied, 'protected_runtime_checked': True,
                'typed_api_loopback_https_requests': sum(
                    call['path'].startswith(('/api/v2/chat/', '/api/v2/users/',
                                             '/api/v2/beatmaps/', '/api/v2/beatmapsets/'))
                    for call in provider.calls),
                'actual_provider_dispatch': False, 'actual_room_testing': False,
                'live_ready': False, 'start_allowed': False}
