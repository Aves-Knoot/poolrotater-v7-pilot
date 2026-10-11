"""One installed provider lifecycle; the fixed upstream entry is release-disabled."""
import os
from pathlib import Path
import signal
import threading

from .config import ConfigError
from .layout import atomic_json
from .provider_contract import require_live_provider
from .provider_rooms import ProviderRoomApi, ProviderRoomHub, _authority, provider_host_controller


def run_provider_service(root, release, *, authentication, api, hub, observer=None):
    """Run existing native workers and teardown under their provider owner lease.

    The invented installed service uses this same loop. An optional fixture
    observer supplies fixture-only counters; it never handles room lifecycle.
    Startup does not enroll, authorize, reset pacing or resolve old ownership.
    """
    _authority(authentication)
    root = Path(root)
    stop = threading.Event()
    if observer is not None and not callable(observer):
        raise ConfigError('invalid provider service observer')
    if observer is None:
        def observer(phase, supervisor, workers):
            value = {'format': 1, 'execution': authentication.execution,
                     'phase': phase, 'pid': os.getpid(), 'uid': os.geteuid(),
                     'installation_id': authentication.binding['installation_id'],
                     'generation': authentication.binding['generation'],
                     'live_ready': False}
            if phase == 'running':
                value.update(version=supervisor.manifest['version'],
                             room_count=len(supervisor.configs),
                             worker_pids=[worker.process.pid for worker in workers])
            elif phase == 'stopped':
                value.update(workers_reaped=True, closed_rooms=len(workers),
                             referee_child_reaped=hub.client._session is None)
            atomic_json(root / 'state/provider-service.json', value)
    previous = {number: signal.signal(number, lambda *_: stop.set())
                for number in (signal.SIGTERM, signal.SIGINT)}
    workers = []
    acquired = False
    supervisor = None
    try:
        with provider_host_controller(root, release, authentication=authentication,
                                      api=api, hub=hub) as supervisor:
            acquired = True
            for alias in supervisor.configs:
                if stop.is_set(): break
                worker = supervisor.prepare(alias, release)
                supervisor.activate(worker)
                workers.append(worker)
            if not stop.is_set(): observer('running', supervisor, workers)
            while not stop.wait(.5) and not supervisor.stopping.is_set():
                # Use the native maintenance cadence: room recreation needs
                # publication, configuration refresh and status processing.
                supervisor.tick()
                for worker in list(supervisor.handles.values()):
                    if not any(seen is worker for seen in workers): workers.append(worker)
                if supervisor.stopping.is_set(): break
                phases = supervisor.registry.snapshot()['rooms']
                for alias, worker in list(supervisor.workers.items()):
                    phase = phases.get(alias, {}).get('phase')
                    if phase == 'recovery_required':
                        raise ConfigError('provider worker health failed')
                    # Tick can intentionally park a worker, or a rollout can
                    # dispose the old generation while activating its successor.
                    if phase == 'parked' or (alias == supervisor.rollout_alias
                            and worker.broker.generation not in supervisor.handles):
                        continue
                    if not worker.fresh():
                        raise ConfigError('provider worker health failed')
        if any(worker.process.poll() is None for worker in workers):
            raise ConfigError('provider cleanup incomplete')
        observer('stopped', supervisor, workers)
    except Exception:
        if acquired: observer('recovery_required', supervisor, workers)
        raise
    finally:
        for number, handler in previous.items(): signal.signal(number, handler)


def run_fixed_provider_service(root, release):
    """Dormant fixed service; no secret/trust/transport/worker before permission."""
    from .installed_authorization import _runtime_guard
    from .pilot_identity import provider_execution
    runtime = _runtime_guard(root, release, prepared=True)()
    require_live_provider(provider_execution(runtime))
    from .installed_authorization import installed_provider_authentication
    from .provider_transport import TrustSnapshot
    auth = installed_provider_authentication(root, release, TrustSnapshot.system(prepared=True))
    api, hub = ProviderRoomApi(auth), ProviderRoomHub(auth)
    return run_provider_service(root, release, authentication=auth, api=api, hub=hub)
