"""Fence a dead, unadopted creation without granting any recovery authority.

Only the affected native worker is retired. Its durable unknown ownership and
candidate evidence remain intact; startup, capacity and update guards still see
the hold. A disconnected or unauthorized shared referee is never contained here.
"""
import logging
from pathlib import Path

log = logging.getLogger(__name__)
CONTAINED = 'contained'
DEFERRED = 'deferred'
REFUSED = 'refused'


def isolate(supervisor, worker, authentication, hub):
    from .provider_authorization import ProviderOAuth
    from .provider_referee import ProviderRefereeClient
    from .provider_rooms import _authority
    # These are parent-owned identities, not fields accepted from worker RPC.
    if (type(authentication) is not ProviderOAuth
            or type(getattr(hub, 'client', None)) is not ProviderRefereeClient
            or hub.client.auth is not authentication
            or supervisor.shared.hub is not hub
            or Path(authentication.root) != Path(supervisor.runtime)
            or supervisor.workers.get(worker.alias) is not worker
            or supervisor.rollout_alias == worker.alias
            or supervisor.stopping.is_set() or supervisor.shared.stopping.is_set()):
        return REFUSED
    try:
        _authority(authentication)
        hub.client._boundary()
    except Exception:
        return REFUSED
    # Never wait for a sibling's routing lock in the controller loop. The caller
    # applies a fixed bounded grace; no mutation/retry happens on this path.
    if not supervisor.shared.lock.acquire(blocking=False):
        return DEFERRED
    try:
        row = supervisor.registry.snapshot()['rooms'].get(worker.alias, {})
        if (row.get('phase') != 'recovery_required'
                or row.get('operation') != 'make_pending'
                or row.get('ownership_unknown') is not True
                or row.get('room_id') is not None
                or row.get('channel_id') is not None
                or row.get('generation') != worker.broker.generation
                or worker.process.poll() is None
                or worker.broker.hub.owned
                or not worker.broker.uncertain
                or worker.broker.hub.creating):
            return REFUSED
        client = hub.client
        with client._lock:
            if (client._manager is None or not client._manager.is_alive()
                    or client._cleanup_failed or client._stop.is_set()
                    or type(client._connect_generation) is not int
                    or client._connect_generation <= 0):
                return REFUSED
            if not hub.is_connected() or client._session is None:
                # Preserve the ordinary bounded reconnect opportunity. The
                # service owns the deadline and resets it only on containment.
                return DEFERRED
        receipt = getattr(worker, '_creation_containment', None)
        identity = (worker.broker.generation, worker.process.pid)
        if receipt is not None:
            return CONTAINED if (receipt == identity and worker.broker.fenced
                                 and not worker.broker.active and row.get('pid') is None
                                 and row.get('failure_contained') is True) else REFUSED
        if row.get('pid') != worker.process.pid:
            return REFUSED
        # dispose only reaps this already-dead child and retires its empty view;
        # it neither invokes CloseRoom nor claims the unknown creation absent.
        supervisor.dispose(worker)
        supervisor.record(worker.alias, pid=None, failure_contained=True)
        worker._creation_containment = identity
        log.error('failed room creation isolated: alias=%s; ownership remains unknown; '
                  'no room retry or recovery applied', worker.alias)
        return CONTAINED
    finally:
        supervisor.shared.lock.release()
