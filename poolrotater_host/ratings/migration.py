"""Private consistent backup primitive for explicit stopped ledger migrations.

The installed administrator must establish quiescence before migrating. This
module neither stops services nor assumes a SQLite lock proves workers stopped.
"""
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
from urllib.parse import quote


def _private(path, *, directory=False):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink(): raise ValueError('rating_backup_symlink_refused')
    info = path.lstat()
    regular = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not regular or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077 or (not directory and info.st_nlink != 1):
        raise ValueError('rating_backup_private_path_required')
    return path


def snapshot_ledger(source, destination, *, deadline_seconds=10, before_publish=None):
    """Publish a complete SQLite backup exclusively; WAL pages are included.

    Existing recovery inputs are never replaced. Failed/unfinished temporary
    files are owned and removed. No source checkpoint or DDL is performed.
    """
    source = _private(source)
    destination = Path(destination).absolute()
    _private(destination.parent, directory=True)
    if os.path.lexists(destination): raise ValueError('rating_backup_already_exists')
    if type(deadline_seconds) not in (int, float) or not 0 < deadline_seconds <= 30:
        raise ValueError('rating_backup_deadline_invalid')
    deadline = time.monotonic()+deadline_seconds
    def progress(*_):
        if time.monotonic() > deadline: raise TimeoutError('rating_backup_timeout')
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix='.rating-backup-', dir=destination.parent)
        temporary = Path(name); os.fchmod(fd, 0o600); os.close(fd)
        origin = sqlite3.connect('file:'+quote(str(source))+'?mode=ro', uri=True, timeout=5)
        try:
            copied = sqlite3.connect(temporary, timeout=5)
            try:
                origin.backup(copied, pages=128, progress=progress, sleep=.02)
                if copied.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise ValueError('rating_backup_integrity_failed')
            finally: copied.close()
        finally: origin.close()
        fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)
        with temporary.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        receipt = {'sha256': digest, 'bytes': temporary.stat().st_size}
        if before_publish is not None:
            before_publish(receipt)
        # link publishes without replacing a previously committed recovery input.
        os.link(temporary, destination)
        fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return receipt
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


RECORD = 'state/rating-migration.json'
LEDGER = 'state/controller-ratings.db'
BACKUP = 'backups/ratings-before-m4.sqlite'


def migration_record(root):
    """Validate private receipt and binding without reading player data."""
    from ..layout import load, read_json, contained
    marker, _ = load(root)
    path = contained(root, RECORD)
    if not os.path.lexists(path): return None
    value = read_json(path)
    if (type(value) is not dict or set(value) != {'format', 'installation_id', 'generation',
            'phase', 'capture_started', 'backup'} or type(value['format']) is not int or value['format'] != 1
            or any(value[k] != marker[k] for k in ('installation_id', 'generation'))
            or value['phase'] not in ('prepared', 'complete')):
        raise ValueError('rating_migration_receipt_invalid')
    import math
    when = value['capture_started']
    if type(when) not in (int, float) or not math.isfinite(when) or when < 0:
        raise ValueError('rating_migration_receipt_invalid')
    backup = value['backup']
    if backup is not None:
        import re
        if (type(backup) is not dict or set(backup) != {'sha256', 'bytes'}
                or type(backup['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', backup['sha256'])
                or type(backup['bytes']) is not int or not 0 < backup['bytes'] <= 256*1024**2):
            raise ValueError('rating_migration_receipt_invalid')
    if value['phase'] == 'complete' and backup is None:
        raise ValueError('rating_migration_receipt_invalid')
    return value


def capture_enabled(root):
    """Startup fence; an unfinished migration cannot open workers."""
    value = migration_record(root)
    from .epoch_review import record as review_record
    from ..layout import load
    review=review_record(root,load(root)[0])
    if review is not None and review['phase']!='complete':
        raise ValueError('rating_epoch_review_recovery_required')
    if value is not None and value['phase'] != 'complete':
        raise ValueError('rating_migration_recovery_required')
    if value is not None:
        local_status(root)  # Restored/incompatible ledgers fail before room creation.
    return value is not None


def _ledger_connection(path):
    _private(path)
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(path)+suffix)
        if os.path.lexists(sidecar):
            # The last reader/writer can remove a WAL sidecar between existence
            # and lstat. Disappearance is normal; an existing unsafe file still
            # fails closed. The main database never gets this exception.
            try:_private(sidecar)
            except FileNotFoundError:pass
    conn = sqlite3.connect('file:'+quote(str(path))+'?mode=rw', uri=True, timeout=5)
    try:
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA synchronous=FULL')
    except BaseException:
        conn.close(); raise
    return conn


def _configured_scopes(path, host):
    # Explicit stopped preparation also runs before the first shared Catch
    # worker. Capture readiness is not proof that optional Catch tables exist.
    # Existing modes gain no schema/topology change from a runtime upgrade.
    if any(room['preset'].startswith('taiko-') for room in host['rooms']):
        from bot.scoped_ratings import ScopedRatingStore
        from bot.taiko_ppstats import TaikoPPStatsStore
        ScopedRatingStore(str(path),ruleset_id=1)
        TaikoPPStatsStore(str(path),ruleset_id=1)
    if not any(room['preset']=='catch-fixed' for room in host['rooms']):return
    from bot.scoped_ratings import ScopedRatingStore
    from bot.catch_ppstats import CatchPPStatsStore
    ScopedRatingStore(str(path),ruleset_id=2)
    CatchPPStatsStore(str(path),ruleset_id=2)


def migrate_ledger(root):
    """Called only inside the administrator's verified stopped/owner lease.

    Runs as the dedicated UID. No provider credentials, network calls or service
    mutations. A prepared receipt can safely resume after any interrupted step.
    """
    from ..layout import load, contained, atomic_json
    from bot.ratings import RatingStore
    from bot.rating_outbox import prepare, validate
    marker, host = load(root)
    path, backup = contained(root, LEDGER), contained(root, BACKUP)
    record = migration_record(root)
    if record is None:
        if os.path.lexists(backup): raise ValueError('rating_untracked_backup_refused')
        if not os.path.lexists(path):
            # Fresh install initialization is explicit and occurs while stopped.
            RatingStore(str(path))
        _private(path)
        record = {'format': 1, 'installation_id': marker['installation_id'],
                  'generation': marker['generation'], 'phase': 'prepared',
                  'capture_started': time.time(), 'backup': None}
        atomic_json(contained(root, RECORD), record)
    if record['phase'] == 'complete':
        conn = _ledger_connection(path)
        try:
            meta = validate(conn)
            if tuple(meta[1:3]) != (marker['installation_id'], marker['generation']):
                raise ValueError('rating_restored_identity_requires_review')
        finally: conn.close()
        _configured_scopes(path,host)
        return local_status(root)
    if not os.path.lexists(backup):
        def journal(receipt):
            # Publish expected identity BEFORE backup publication, so a restart
            # never adopts an arbitrary pre-existing recovery file.
            record['backup'] = receipt
            atomic_json(contained(root, RECORD), record)
        snapshot_ledger(path, backup, before_publish=journal)
    _private(backup)
    with backup.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    if record['backup'] is None or record['backup'] != {'sha256': digest, 'bytes': backup.stat().st_size}:
        raise ValueError('rating_backup_identity_mismatch')
    # Explicit stopped initialization/legacy upgrades, after preserving the
    # original ledger. A crash during legacy DDL resumes here idempotently.
    RatingStore(str(path))
    conn = _ledger_connection(path)
    try:
        conn.execute('BEGIN EXCLUSIVE')
        prepare(conn, installation_id=marker['installation_id'], generation=marker['generation'],
                capture_started=record['capture_started'])
        conn.commit()
    except BaseException:
        conn.rollback(); raise
    finally: conn.close()
    record['phase'] = 'complete'
    atomic_json(contained(root, RECORD), record)
    _configured_scopes(path,host)
    return local_status(root)


def local_status(root):
    """Counts only; never includes voter IDs, payloads, paths or credentials."""
    from ..layout import contained
    from bot.rating_outbox import validate
    record = migration_record(root)
    if record is None: return {'ok': True, 'capture_enabled': False, 'migration_phase': 'absent'}
    if record['phase'] != 'complete':
        return {'ok': True, 'capture_enabled': False, 'migration_phase': 'prepared', 'recovery_required': True}
    conn = _ledger_connection(contained(root, LEDGER))
    try:
        meta = validate(conn)
        if tuple(meta[1:3]) != (record['installation_id'], record['generation']):
            raise ValueError('rating_restored_identity_requires_review')
        counts = dict(conn.execute('SELECT phase,COUNT(*) FROM rating_outbox GROUP BY phase'))
        if not set(counts) <= {'pending','accepted','already_accepted','superseded','conflict',
                               'invalid','quarantined','permanently_rejected','retryable'}:
            raise ValueError('rating_outbox_phase_invalid')
        result={'ok': True, 'capture_enabled': True, 'migration_phase': 'complete',
                'sequence': meta[3], 'events': counts, 'recovery_required': False}
        from .epoch_review import record as review_record
        review=review_record(root,record)
        if review is not None:
            result.update(epoch_review_phase=review['phase'],capture_enabled=review['phase']=='complete',
                          recovery_required=review['phase']!='complete')
        return result
    finally: conn.close()
