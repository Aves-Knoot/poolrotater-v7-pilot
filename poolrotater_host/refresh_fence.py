"""Serialize scheduled room recreation with update-drain admission.

The update gate remains the authority. The lease only closes the interval
between collecting a worker identity and publishing that gate.
"""
from contextlib import contextmanager
import os
from pathlib import Path

from .layout import file_lease

LOCK = 'state/rolling/automatic-refresh-admission.lock'
GATE = 'state/rolling/supervisor-maintenance.json'


@contextmanager
def update_fence(root, uid):
    """Acquire as the service UID, then run root admission with that lease held."""
    from .ratings.operations import _identity
    lease = file_lease(Path(root) / LOCK)
    if os.geteuid() == uid:
        lease.__enter__()
    else:
        with _identity(uid):
            lease.__enter__()
    try:
        yield
    finally:
        lease.__exit__(None, None, None)


@contextmanager
def refresh_fence(root):
    """Hold through a refresh, excluding a concurrent update gate publication."""
    root = Path(root)
    with file_lease(root / LOCK):
        yield not os.path.lexists(root / GATE)
