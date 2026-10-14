"""One installed provider lifecycle; the fixed upstream entry is release-disabled."""
import os
import logging
from pathlib import Path
import signal
import threading
import time

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
    failure = {}
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
                             room_count=len(workers),
                             worker_pids=[worker.process.pid for worker in workers])
            elif phase == 'stopped':
                value.update(workers_reaped=True, closed_rooms=len(workers),
                             referee_child_reaped=hub.client._session is None)
            elif phase == 'failed':
                value.update(failure or {'failure_code': 'service_failed'})
                value['workers_reaped'] = all(worker.process.poll() is not None for worker in workers)
                value['referee_child_reaped'] = hub.client._session is None
            atomic_json(root / 'state/provider-service.json', value)
    workers = []
    acquired = False
    supervisor = None
    def requested_stop(*_):
        stop.set()
        if supervisor is not None:
            supervisor.request_shutdown()
    previous = {number: signal.signal(number, requested_stop)
                for number in (signal.SIGTERM, signal.SIGINT)}
    last_usage_log = 0.
    try:
        with provider_host_controller(root, release, authentication=authentication,
                                      api=api, hub=hub) as supervisor:
            acquired = True
            private = getattr(supervisor, 'private_pilot', None)
            selected = (private.alias,) if private is not None else tuple(supervisor.configs)
            for alias in selected:
                if stop.is_set(): break
                worker = supervisor.prepare(alias, release)
                workers.append(worker)
                supervisor.activate(worker)
            if not stop.is_set(): observer('running', supervisor, workers)
            while not stop.wait(.5) and not supervisor.stopping.is_set():
                if time.monotonic() - last_usage_log >= 60:
                    last_usage_log = time.monotonic()
                    try:
                        from .provider_usage import summary
                        logging.getLogger(__name__).info('%s', summary(root))
                    except Exception:
                        logging.getLogger(__name__).warning('provider usage summary unavailable; pacing remains authoritative')
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
                        raise ConfigError('provider_worker_ownership_recovery_required')
                    # Tick can intentionally park a worker, or a rollout can
                    # dispose the old generation while activating its successor.
                    if phase == 'parked' or (alias == supervisor.rollout_alias
                            and worker.broker.generation not in supervisor.handles):
                        continue
                    # Native status comes from the gameplay loop. That loop
                    # can wait behind another room's authenticated API/referee
                    # call, so an old observation is not evidence of exit or
                    # uncertain ownership. Keep Worker.fresh() strict for
                    # publication, public observations and empty-room actions;
                    # use the native process/connection signals for teardown.
                    if worker.failure:
                        raise ConfigError('provider_worker_failed')
                    if worker.process.poll() is not None:
                        raise ConfigError('provider_worker_exited')
                    if worker.peer.closed.is_set():
                        raise ConfigError('provider_worker_connection_closed')
            if stop.is_set() or supervisor.stopping.is_set():
                supervisor.request_shutdown()
        if any(worker.process.poll() is None for worker in workers):
            raise ConfigError('provider_cleanup_incomplete')
        observer('stopped', supervisor, workers)
    except Exception as error:
        from .service_diagnostics import failure_details
        failure = failure_details(error)
        if acquired: observer('failed', supervisor, workers)
        raise
    finally:
        for number, handler in previous.items(): signal.signal(number, handler)


def run_fixed_provider_service(root, release):
    """Dormant fixed service; no secret/trust/transport/worker before permission."""
    from .installed_authorization import _runtime_guard
    from .pilot_identity import provider_execution
    runtime = _runtime_guard(root, release, prepared=True)()
    require_live_provider(provider_execution(runtime))
    # Restore the existing native supervisor audit sink before integrations or
    # provider adapters are constructed. Worker processes keep their own logs.
    from bot.logsetup import configure
    configure(log_file=str(Path(root) / 'logs/multiroom.log'))
    formatter = logging.Formatter(
        "%(asctime)s [%(threadName)s] %(name)s %(levelname)s %(message)s",
        "%Y-%m-%d %H:%M:%S")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    # Third-party Discord DEBUG logs contain raw payloads/private URLs. Native
    # discord_control events use a separate logger and remain observable.
    logging.getLogger('discord').setLevel(logging.WARNING)
    from .installed_authorization import installed_provider_authentication
    from .provider_transport import TrustSnapshot
    auth = installed_provider_authentication(root, release, TrustSnapshot.system(prepared=True))
    api, hub = ProviderRoomApi(auth), ProviderRoomHub(auth)
    return run_provider_service(root, release, authentication=auth, api=api, hub=hub)
