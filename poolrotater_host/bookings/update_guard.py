"""Fail-closed, side-effect-free booking gate for ordinary update admission."""
from . import policy
from ..network.contracts import NetworkError, require
from pathlib import Path

ALIASES = {'room1', 'room2', 'room3', 'room4'}


def holds_alias(root, alias):
    """Return whether this alias has an unclean lease or uncertain local state."""
    if type(alias) is not str or alias not in ALIASES:
        return True
    try:
        path = Path(root) / 'state/bookings.sqlite3'
        if not path.exists():
            return False
        # Consult the root-wide admission check, then narrow its result by alias.
        # It also fails closed when the existing private database cannot be read.
        policy.holds_updates(root)
        from .local import LocalSessions
        from ..managed_host.policy import state as managed_state
        store = LocalSessions(root, managed_state(root).binding)
        rows = store.leases(include_ended=True)
        for row in rows:
            lease = row.get('lease')
            lifecycle = row.get('state')
            if (type(lease) is not dict or lease.get('alias') not in ALIASES or
                    type(lease.get('id')) is not str or
                    type(row.get('cleanup_complete')) is not bool or
                    type(lifecycle) is not dict or lifecycle.get('phase') not in
                    ('preparing', 'awaiting_claim', 'active', 'closing', 'ended')):
                return True
            if lease['alias'] == alias and not row.get('cleanup_complete'):
                return True
        return False
    except Exception:
        return True


def holds_updates(root, alias=None):
    """Compatibility wrapper: root-wide when omitted, alias-specific otherwise."""
    if alias is not None:
        return holds_alias(root, alias)
    try:
        return bool(policy.holds_updates(root))
    except Exception:
        return True


def require_update_allowed(root, alias):
    require(not holds_alias(root, alias), 'booking_update_blocked', 409)
    return True
