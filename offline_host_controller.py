"""Development-only native host integration with real supervisor/worker processes.

There is no live CLI or OAuth path here. Callers supply synthetic transports,
inside scripts/offline. Immutable release checking remains the real default;
tests explicitly scope their writable-fixture mock in the test harness.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import queue

from poolrotater_host.config import ConfigError, contained
from poolrotater_host.layout import checked_root, owner_lease
from poolrotater_host.oauth import OfflineOAuth
from poolrotater_host.runtime_config import RuntimeConfigBridge


@contextmanager
def host_controller(root, release, *, api, hub, account_id=None, authentication=None):
    if authentication is None and (type(account_id) is not int or account_id <= 0):
        raise ConfigError('synthetic controller requires a positive account identity')
    if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1':
        raise ConfigError('controller integration requires scripts/offline isolation')
    if getattr(api, 'offline_fixture', False) is not True or getattr(hub, 'offline_fixture', False) is not True:
        raise ConfigError('controller integration accepts synthetic transports only')
    root = checked_root(Path(root))
    if authentication is not None:
        if not isinstance(authentication, OfflineOAuth) or authentication.root != root or account_id is not None:
            raise ConfigError('controller requires its own offline OAuth authority')
    from poolrotater_host.provider_room_state import BINDING_PATH
    if os.path.lexists(contained(root, BINDING_PATH)):
        raise ConfigError('provider room authority cannot be adopted by the offline controller')
    with _host_controller(root, release, api=api, hub=hub, account_id=account_id,
                          authentication=authentication) as supervisor:
        yield supervisor


@contextmanager
def _host_controller(root, release, *, api, hub, account_id=None, authentication=None,
                     before_start=None, prepared=False):
    """Shared supervisor lifecycle after the caller's exact authority checks."""
    release = Path(release).resolve()
    runtime_validator = None
    if not release.is_relative_to(contained(root, 'releases')):
        from poolrotater_host.provisioning import load_provisioned
        if prepared:
            load_provisioned(root, release=release, prepared=True)
            runtime_validator = lambda candidate: load_provisioned(root, release=candidate, prepared=True)
        else:
            load_provisioned(root,release=release)
            runtime_validator = lambda candidate: load_provisioned(root, release=candidate)
    lease = authentication.verified_owner() if authentication is not None else owner_lease(root, account_id=account_id)
    with lease as account:
        if authentication is not None: account_id = account.user_id
        from poolrotater_host.ownership_recovery import require_settled
        require_settled(root)
        if before_start is not None: before_start(account)
        if runtime_validator is not None:
            # The administrator uses this same owner lease for a stopped switch.
            # Recheck after acquisition to close a preflight/activation race.
            runtime_validator(release)
        # Identity verification and ownership precede configuration/store/room creation.
        bridge = RuntimeConfigBridge(root, account_id=account_id)
        directory = contained(root, 'state/rolling')
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.stat().st_uid != os.geteuid() or directory.stat().st_mode & 0o077:
            raise ConfigError('runtime ownership directory must be private')
        from rolling.supervisor import RollingSupervisor
        supervisor = RollingSupervisor(bridge.load_rooms(), api, hub, queue.Queue(), None,
                                       release, root, health_seconds=.15, startup_timeout=35,
                                       config_loader=bridge.load_rooms,
                                       settings_path=contained(root, 'config/supervisor.json'),
                                       runtime_validator=runtime_validator)
        from poolrotater_host.runtime_registry import PrivateRegistry
        supervisor.registry = PrivateRegistry(supervisor.registry)
        hub.shared = supervisor.shared
        try:
            hub.connect()
            supervisor.shared.start_dispatch()
            yield supervisor
        finally:
            supervisor.stopping.set()
            if supervisor.rollout_thread:
                supervisor.rollout_thread.join(timeout=35)
            supervisor.shared.begin_shutdown(45)
            api.cancel_pending.set()
            errors = []
            try:
                for worker in list(supervisor.handles.values()):
                    try:
                        supervisor.dispose(worker)
                        if worker.broker.hub.owned and not worker.broker.uncertain:
                            rid, = worker.broker.hub.owned
                            worker.broker.close_room(rid)
                        if worker.broker.hub.owned or worker.broker.uncertain:
                            raise RuntimeError('synthetic room ownership remains unresolved')
                        supervisor.record(worker.alias, phase='stopped', pid=None)
                    except Exception as exc:
                        supervisor.record(worker.alias, phase='recovery_required', error=type(exc).__name__)
                        errors.append(exc)
                supervisor.write_status(force=True)
            finally:
                supervisor.discord.stop()
                for modlog in supervisor.modlogs.values():
                    modlog.stop(timeout=1)
                supervisor.shared.stop()
            if errors:
                raise ConfigError('offline controller cleanup requires ownership reconciliation') from errors[0]
