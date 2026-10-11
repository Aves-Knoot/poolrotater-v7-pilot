"""Short, owner-identity SQLite reads for single-threaded operational jobs.

Even mode=ro may create WAL/SHM files. Root initially creates those as root
before SQLite fchowns them, which races a service-user writer on 0600 databases.
Keep the database owner's effective identity for the entire connection lifetime,
including error cleanup. Never use immutable=1 for a changing live database.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import sqlite3
import threading


@contextmanager
def _database_owner(path):
    owner = path.stat()
    uid, gid = os.geteuid(), os.getegid()
    if owner.st_uid == uid:
        yield
        return
    # Effective credentials are process-wide. These operational publishers are
    # synchronous; refuse credential switching in a threaded caller. Normal
    # service-user connections never need to switch credentials.
    if uid != 0:
        raise PermissionError(errno.EACCES, "SQLite reader must run as database owner", str(path))
    if threading.active_count() != 1 or len(list(Path('/proc/self/task').iterdir())) != 1:
        raise OSError(errno.EBUSY, "Owner-identity SQLite reads require a single-threaded process")
    try:
        os.setegid(owner.st_gid)
        os.seteuid(owner.st_uid)
        yield
    finally:
        os.seteuid(uid)
        os.setegid(gid)


@contextmanager
def readonly_database(path, *, timeout=5):
    """Yield a read-only snapshot, close it and restore identity on all exits.

    The caller must do only database reads inside this scope, with no thread
    creation or privileged filesystem work. Materialize results before exiting.
    """
    path = Path(path).resolve()
    with _database_owner(path):
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=timeout)
        try:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            yield conn
        finally:
            conn.close()
