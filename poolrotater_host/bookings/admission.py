"""One local admission lease shared by booking installs and controller updates."""
from contextlib import contextmanager
import os
from pathlib import Path
from ..layout import file_lease


def declared(root):
    root=Path(root)
    return any(os.path.lexists(root/path)for path in
               ('config/private-bookings.json','state/bookings.sqlite3'))


@contextmanager
def update_admission(root):
    """Call as the installed service UID; legacy roots stay entirely inert."""
    root=Path(root)
    if not declared(root):
        yield
        return
    with file_lease(root/'state/booking-admission.lock',timeout=1):
        yield
