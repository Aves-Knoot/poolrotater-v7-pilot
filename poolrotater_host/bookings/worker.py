"""Main-loop booking controls for one pinned native room worker.

The rolling worker owns the lifecycle queue and calls these helpers only from
its main thread. The selection adapter performs no file, network, or provider
I/O; callers supply the already-qualified pool materializer for settings that
need a fresh candidate pool.
"""

from .selection import (
    SelectionError,
    apply_booking_settings,
    clear_selection,
    install_selection,
)

__all__ = [
    'SelectionError',
    'apply_booking_settings',
    'clear_selection',
    'install_selection',
]
