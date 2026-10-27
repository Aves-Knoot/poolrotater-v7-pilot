"""One installed provider lifecycle; the fixed upstream entry is release-disabled."""
import os
import logging
import json
from pathlib import Path
import signal
import threading
import time
from contextlib import contextmanager

from .config import ConfigError
from .layout import atomic_json
from .provider_contract import require_live_provider
from .provider_rooms import ProviderRoomApi, ProviderRoomHub, _authority, provider_host_controller

WORKER_FAILURE_CLEANUP_SECONDS = 30


@contextmanager
def _managed_service_controller(*args, **kwargs):
    """A fatal service exit coordinates closure before account transports stop."""
    on_failure = kwargs.pop('_on_failure', None)
    with provider_host_controller(*args, **kwargs) as supervisor:
        try:
            yield supervisor
        except BaseException as error:
            # Capture the original cause BEFORE cleanup can fail or be killed.
            # This callback has no lifecycle authority and cannot grant retries.
            if on_failure is not None:
                try: on_failure(error, supervisor)
                except Exception:
                    logging.getLogger(__name__).warning('controller failure evidence unavailable')
            # The service is exiting, so retaining occupied siblings would
            # orphan them. This does not bypass uncertainty or roster fences.
            supervisor.request_shutdown()
            raise


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
    failing_workers = {}
    def capture_failure(error, owner):
        from .service_diagnostics import controller_failure
        detail = controller_failure(error, release, getattr(owner, '_tick_component', None))
        failure.update({k: detail[k] for k in ('failure_code', 'failure_http_status') if k in detail})
        # Separate private evidence file avoids changing existing status contracts.
        atomic_json(root / 'state/controller-failure.json', {
            'format': 1, 'at': time.time(), 'pid': os.getpid(), **detail})
        logging.getLogger(__name__).error('controller failure before cleanup: %s',
                                         json.dumps(detail, sort_keys=True))
    try:
        with _managed_service_controller(root, release, authentication=authentication,
                                      api=api, hub=hub, _on_failure=capture_failure) as supervisor:
            acquired = True
            private = getattr(supervisor, 'private_pilot', None)
            selected = (private.alias,) if private is not None else tuple(supervisor.configs)
            for alias in selected:
                if stop.is_set(): break
                managed=getattr(supervisor,'managed',None)
                if managed is not None and not managed.startup_allowed(alias):
                    managed.park(alias)
                    continue
                worker = supervisor.prepare(alias, supervisor.start_release_for(alias))
                workers.append(worker)
                supervisor.activate(worker)
            if not stop.is_set():
                supervisor.prepare_startup_status()
                if not stop.is_set() and not supervisor.stopping.is_set():
                    observer('running', supervisor, workers)
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
                        from .creation_containment import isolate, CONTAINED, DEFERRED
                        outcome = isolate(supervisor, worker, authentication, hub)
                        if outcome == CONTAINED:
                            failing_workers.pop((alias, id(worker)), None)
                            continue
                        if outcome == DEFERRED:
                            key = (alias, id(worker))
                            seen = failing_workers.setdefault(key, time.monotonic())
                            if time.monotonic() - seen < WORKER_FAILURE_CLEANUP_SECONDS:
                                continue
                        raise ConfigError('provider_worker_ownership_recovery_required')
                    # The native rolling engine owns the handoff alias until
                    # activation or safe rollback has settled. Let it handle
                    # candidate failure and planned retirement; siblings and
                    # uncertain ownership still remain terminal.
                    if phase == 'parked' or alias == supervisor.rollout_alias:
                        continue
                    # Native status comes from the gameplay loop. That loop
                    # can wait behind another room's authenticated API/referee
                    # call, so an old observation is not evidence of exit or
                    # uncertain ownership. Keep Worker.fresh() strict for
                    # publication, public observations and empty-room actions;
                    # use the native process/connection signals for teardown.
                    failure_code = None
                    if worker.failure:
                        failure_code = 'provider_worker_failed'
                    elif worker.process.poll() is not None:
                        failure_code = 'provider_worker_exited'
                    elif worker.peer.closed.is_set():
                        failure_code = 'provider_worker_connection_closed'
                    if failure_code:
                        # Native failed-alias cleanup has its own fixed deadline.
                        # It may be waiting for an already-dispatched close or a
                        # verified roster refresh; do not preempt it with an older
                        # RPC-disconnect timestamp. Unknown ownership above still
                        # stops immediately, and this deadline is never extended.
                        cleanup_deadline = getattr(worker, 'failure_cleanup_deadline', None)
                        if (cleanup_deadline is not None and worker.process.poll() is not None
                                and time.monotonic() < cleanup_deadline):
                            continue
                        # Retirement may start AFTER the fast check above.
                        # Recheck delegation after observing the signal; also
                        # ignore a stale reference once the native engine has
                        # replaced it with its healthy successor.
                        if supervisor.workers.get(alias) is not worker or (
                                alias == supervisor.rollout_alias):
                            continue
                        # Both normal shutdown and failure can disconnect RPC
                        # before process exit becomes visible to native tick.
                        # Let that bounded cleanup settle;
                        # the native tick parks a positively closed/reaped alias.
                        # A process/RPC can exit between tick() and this check.
                        # Give the next tick its chance to reconcile that exact
                        # dead alias too. Unknown ownership is escalated by tick;
                        # the grace never claims closure or replays a mutation.
                        # Never use a stale status snapshot to claim closure.
                        key=(alias,id(worker))
                        seen=failing_workers.setdefault(key,time.monotonic())
                        if time.monotonic()-seen < WORKER_FAILURE_CLEANUP_SECONDS:
                            if not getattr(worker,'_failure_containment_logged',False):
                                logging.getLogger(__name__).warning(
                                    'room worker terminating; waiting for alias-scoped closure before escalation: '
                                    '%s exit_code=%s rpc_closed=%s failure_reported=%s',
                                    alias, getattr(worker.process, 'returncode', None),
                                    worker.peer.closed.is_set(), bool(worker.failure))
                                worker._failure_containment_logged=True
                            continue
                        raise ConfigError(failure_code)
            if stop.is_set() or supervisor.stopping.is_set():
                supervisor.request_shutdown()
        if any(worker.process.poll() is None for worker in workers):
            raise ConfigError('provider_cleanup_incomplete')
        observer('stopped', supervisor, workers)
    except Exception as error:
        from .service_diagnostics import failure_details
        if not failure:
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
    from .reporting_logging import install
    from . import __version__
    install(root,version=__version__)
    if __version__=='7.1.0':
        from .activity_migration import capture_enabled
        if not capture_enabled(root):raise ConfigError('v7.1 activity preparation required before startup')
    from .installed_authorization import installed_provider_authentication
    from .provider_transport import TrustSnapshot
    auth = installed_provider_authentication(root, release, TrustSnapshot.system(prepared=True))
    api, hub = ProviderRoomApi(auth), ProviderRoomHub(auth)
    return run_provider_service(root, release, authentication=auth, api=api, hub=hub)
