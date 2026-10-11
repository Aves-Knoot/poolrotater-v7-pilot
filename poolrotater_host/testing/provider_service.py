"""Long-running installed invented provider; never enrolls or authorizes on start."""
import os
from pathlib import Path

from ..config import ConfigError
from ..layout import atomic_json
from ..provider_rooms import ProviderRoomApi, ProviderRoomHub
from ..provider_service import run_provider_service
from .installed_operator_workflow import authority
from .room_service import InventedRoomService


def run(root, release):
    root = Path(root)
    path = root / 'state/provider-service-fixture.json'
    with authority(root, release, initialize=False) as (auth, provider):
        backend = InventedRoomService(provider)
        api, hub = ProviderRoomApi(auth, backend), ProviderRoomHub(auth, backend)
        def observe(phase, supervisor, workers):
            if phase == 'running':
                atomic_json(path, {'format': 1, 'execution': 'provider-fixture',
                    'phase': 'running', 'pid': os.getpid(), 'uid': os.geteuid(),
                    'version': supervisor.manifest['version'],
                    'worker_pids': [w.process.pid for w in workers],
                    'room_count': len(backend.rooms), 'verified_account': 99,
                    'referee_tls_websocket_connections': backend.wire.connections,
                    'referee_child_pid': hub.client._session.process.pid,
                    'actual_provider_dispatch': False, 'actual_room_testing': False,
                    'live_ready': False})
            elif phase == 'stopped':
                if backend.rooms: raise ConfigError('invented provider cleanup incomplete')
                atomic_json(path, {'format': 1, 'execution': 'provider-fixture',
                    'phase': 'stopped', 'pid': os.getpid(), 'workers_reaped': True,
                    'closed_rooms': len(workers), 'make_calls': backend.authenticated_calls.count('MakeRoom'),
                    'close_calls': backend.authenticated_calls.count('CloseRoom'),
                    'referee_child_reaped': hub.client._session is None,
                    'actual_provider_dispatch': False, 'actual_room_testing': False, 'live_ready': False})
            else:
                atomic_json(path, {'format': 1, 'execution': 'provider-fixture',
                    'phase': 'recovery_required', 'pid': os.getpid(),
                    'actual_provider_dispatch': False, 'actual_room_testing': False, 'live_ready': False})
        run_provider_service(root, release, authentication=auth, api=api, hub=hub, observer=observe)
