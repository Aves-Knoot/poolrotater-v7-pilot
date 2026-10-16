"""Private bounded SQLite state; these databases never rewrite the native ledger."""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat
from ..network.contracts import require


@contextmanager
def connection(path):
    path=Path(path)
    for parent in (path.parent,*path.parent.parents):
        require(not parent.is_symlink(),'unsafe_activity_storage')
    require(path.parent.is_dir(),'activity_storage_parent_required')
    parent_info=path.parent.stat()
    require(parent_info.st_uid==os.geteuid() and not stat.S_IMODE(parent_info.st_mode)&0o022,'unsafe_activity_storage_parent')
    if not os.path.lexists(path):
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and info.st_nlink==1
            and not stat.S_IMODE(info.st_mode)&0o077,'unsafe_activity_storage')
    require(info.st_size<512*1024**2,'activity_capacity_required',503)
    for suffix in ('-wal','-shm','-journal'):
        side=Path(str(path)+suffix)
        if os.path.lexists(side):
            side_info=side.lstat()
            require(not side.is_symlink() and stat.S_ISREG(side_info.st_mode)
                    and side_info.st_uid==os.geteuid() and not stat.S_IMODE(side_info.st_mode)&0o077,'unsafe_activity_storage')
    db=sqlite3.connect(path,timeout=1,isolation_level=None);db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA busy_timeout=1000');db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA max_page_count=131072');db.execute('BEGIN IMMEDIATE')
        yield db;db.commit()
    except BaseException:
        db.rollback();raise
    finally: db.close()
