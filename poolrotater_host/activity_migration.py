"""Stopped, additive native activity-ledger migration with durable recovery.

The root controller-upgrade caller first holds its existing quiescent owner
lease, checks unit/process absence, then drops to the actual service UID here.
No provider, credential, service operation or consent change occurs.
"""
from __future__ import annotations
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote

from .activity import ledger
from .config import ConfigError
from .layout import (account_lease, atomic_json, contained, load, owner_lease,
                     read_json, _namespace_lease)
from .ratings.migration import _private, _ledger_connection, snapshot_ledger

RECORD = 'state/activity-migration.json'
INITIALIZATION = 'state/activity-initialization.json'
LEDGER = 'state/controller-ratings.db'
BACKUP = 'backups/ratings-before-v71-activity.sqlite'
SCHEMA_SHA256 = hashlib.sha256(ledger.SCHEMA.encode()).hexdigest()
MAX_BYTES = 2 * 1024**3
MAX_ROWS = 2_000_000
MAX_SECONDS = 30
META = 'activity_schema_meta'
META_SQL = '''CREATE TABLE activity_schema_meta (
 id INTEGER PRIMARY KEY CHECK(id=1), format INTEGER NOT NULL,
 installation_id TEXT NOT NULL,generation TEXT NOT NULL,
 schema_sha256 TEXT NOT NULL,legacy_sha256 TEXT NOT NULL,migrated_at REAL NOT NULL)'''


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _schema(conn):
    return [(row[0], row[1], row[2]) for row in conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'activity_%' ORDER BY name")]


def _expected_schema():
    with sqlite3.connect(':memory:') as conn:
        ledger.prepare(conn)
        conn.execute(META_SQL)
        return _schema(conn)


def _validate_schema(conn, *, allow_empty_preexisting=False):
    actual = _schema(conn)
    expected = _expected_schema()
    if allow_empty_preexisting:
        expected_without_meta = [row for row in expected if row[1] != META]
        if not actual:
            return
        if actual != expected_without_meta:
            raise ValueError('activity_unmanaged_schema_requires_review')
        for kind, name, _ in actual:
            if kind == 'table' and conn.execute('SELECT 1 FROM ' + _quote(name) + ' LIMIT 1').fetchone():
                raise ValueError('activity_unmanaged_history_requires_review')
        return
    if actual != expected:
        raise ValueError('activity_schema_identity_requires_review')


def _fingerprint(conn):
    """Stream a bounded deterministic legacy-data proof; no rows leave memory.

    SQLite backup retains row IDs. WITHOUT ROWID tables use primary-key order.
    Only additive activity objects are excluded; all other schema and data are
    included, preserving ratings, outboxes, PP and operating history.
    """
    deadline = time.monotonic() + MAX_SECONDS
    digest = hashlib.sha256()
    count = 0
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        schema = list(conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name NOT LIKE 'activity_%' ORDER BY type,name"))
        for row in schema:
            digest.update(repr(tuple(row)).encode('utf-8') + b'\n')
            if row[0] != 'table':
                continue
            name = row[1]
            info = list(conn.execute('PRAGMA table_info(' + _quote(name) + ')'))
            pk = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
            order = ','.join(_quote(key) for key in pk) if 'WITHOUT ROWID' in (row[3] or '').upper() else 'rowid'
            for data in conn.execute('SELECT * FROM ' + _quote(name) + ' ORDER BY ' + order):
                count += 1
                if count > MAX_ROWS or time.monotonic() > deadline:
                    raise ValueError('activity_legacy_verification_bound_exceeded')
                digest.update(repr(tuple(data)).encode('utf-8') + b'\n')
        return digest.hexdigest()
    finally:
        conn.set_progress_handler(None, 0)


def _backup_identity(path):
    _private(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('activity_ledger_size_bound_exceeded')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'sha256': digest, 'bytes': path.stat().st_size}


def migration_record(root):
    marker, _ = load(root)
    path = contained(root, RECORD)
    if not os.path.lexists(path):
        return None
    value = read_json(path)
    required = {'format', 'installation_id', 'generation', 'phase', 'capture_started',
                'schema_sha256', 'legacy_sha256', 'backup'}
    if (type(value) is not dict or set(value) != required or type(value['format']) is not int or value['format'] != 1
            or any(value[k] != marker[k] for k in ('installation_id', 'generation'))
            or value['phase'] not in ('prepared', 'complete') or value['schema_sha256'] != SCHEMA_SHA256
            or type(value['legacy_sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', value['legacy_sha256'])
            or type(value['capture_started']) not in (int, float) or not math.isfinite(value['capture_started']) or value['capture_started'] < 0):
        raise ValueError('activity_migration_receipt_requires_review')
    backup = value['backup']
    if backup is not None and (type(backup) is not dict or set(backup) != {'sha256', 'bytes'}
            or type(backup['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', backup['sha256'])
            or type(backup['bytes']) is not int or not 0 < backup['bytes'] <= MAX_BYTES):
        raise ValueError('activity_migration_backup_receipt_requires_review')
    if value['phase'] == 'complete' and backup is None:
        raise ValueError('activity_migration_backup_missing')
    return value


def _witness(conn, marker, record):
    _validate_schema(conn)
    rows = conn.execute('SELECT * FROM activity_schema_meta').fetchall()
    expected = (1, 1, marker['installation_id'], marker['generation'],
                SCHEMA_SHA256, record['legacy_sha256'], record['capture_started'])
    if len(rows) != 1 or tuple(rows[0]) != expected:
        raise ValueError('activity_restored_or_foreign_ledger_requires_review')


def _stopped_journal(root, config):
    path = contained(root, 'state/rolling/ownership.json')
    if os.path.lexists(path):
        from .reconciliation import _journal
        value = _journal(read_json(path), {room['alias'] for room in config['rooms']})
        for state in value['rooms'].values():
            if (state.get('phase') not in ('stopped', 'parked') or state.get('room_id')
                    or state.get('ownership_unknown') or state.get('operation') != 'closed'):
                raise ConfigError('activity_migration_requires_stopped_clean_ownership')
    recovery = contained(root, 'state/ownership-recovery.json')
    if os.path.lexists(recovery):
        from .ownership_recovery import validate_record
        value = read_json(recovery)
        validate_record(value, root=root, marker=load(root)[0], uid=os.geteuid())
        if value['phase'] == 'prepared':
            raise ConfigError('activity_migration_ownership_recovery_pending')


def _migrate(root, marker):
    path, backup = contained(root, LEDGER), contained(root, BACKUP)
    _private(path)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('activity_ledger_size_bound_exceeded')
    record = migration_record(root)
    if record is None:
        if os.path.lexists(backup):
            raise ValueError('activity_untracked_backup_requires_review')
        conn = _ledger_connection(path)
        try:
            _validate_schema(conn, allow_empty_preexisting=True)
            legacy = _fingerprint(conn)
        finally:
            conn.close()
        record = {'format': 1, **{k: marker[k] for k in ('installation_id', 'generation')},
                  'phase': 'prepared', 'capture_started': time.time(),
                  'schema_sha256': SCHEMA_SHA256, 'legacy_sha256': legacy, 'backup': None}
        atomic_json(contained(root, RECORD), record)
    if record['phase'] == 'complete':
        if _backup_identity(backup) != record['backup']:
            raise ValueError('activity_backup_identity_mismatch')
        _prepare_reader(root)
        return local_status(root)
    if not os.path.lexists(backup):
        def journal(receipt):
            if receipt['bytes'] > MAX_BYTES:
                raise ValueError('activity_ledger_size_bound_exceeded')
            if record['backup'] is not None and receipt != record['backup']:
                raise ValueError('activity_recreated_backup_identity_mismatch')
            record['backup'] = receipt
            atomic_json(contained(root, RECORD), record)
        snapshot_ledger(path, backup, before_publish=journal)
    if _backup_identity(backup) != record['backup']:
        raise ValueError('activity_backup_identity_mismatch')
    conn = _ledger_connection(path)
    try:
        conn.execute('BEGIN EXCLUSIVE')
        if _fingerprint(conn) != record['legacy_sha256']:
            raise ValueError('activity_legacy_history_changed_during_migration')
        existing = conn.execute("SELECT name FROM sqlite_master WHERE name=?", (META,)).fetchone()
        if existing is None:
            _validate_schema(conn, allow_empty_preexisting=True)
            ledger.prepare(conn)
            conn.execute(META_SQL)
            conn.execute('INSERT INTO activity_schema_meta VALUES(1,1,?,?,?,?,?)',
                         (marker['installation_id'], marker['generation'], SCHEMA_SHA256,
                          record['legacy_sha256'], record['capture_started']))
        _witness(conn, marker, record)
        if _fingerprint(conn) != record['legacy_sha256']:
            raise ValueError('activity_legacy_history_changed_during_migration')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    _prepare_reader(root)
    atomic_json(contained(root, RECORD), {**record, 'phase': 'complete'})
    return local_status(root)


def _admitted(root, owner_lease_held, operation):
    marker, config = load(root)
    if type(owner_lease_held) is not bool:
        raise ConfigError('activity_migration_invalid_owner_admission')
    account = config['account']['expected_user_id']
    lease = (account_lease(account) if account is not None else _namespace_lease('install-' + marker['installation_id'])) if owner_lease_held else owner_lease(root)
    with lease:
        _stopped_journal(root, config)
        return operation(root, marker)


def migrate_ledger(root, *, owner_lease_held=False):
    """Actual-UID stopped migration; never initializes a missing old ledger.

    owner_lease_held is only for a root upgrade caller holding the existing
    quiescent owner FD across its identity drop. It still takes the actual-UID
    account lease. Reader storage is prepared outside worker startup too.
    """
    return _admitted(root, owner_lease_held, _migrate)


def _prepare_reader(root):
    from .activity.reader import Reader
    Reader(root)


def _reader_ready(root):
    path = _private(contained(root, 'state/activity-cache.sqlite3'))
    conn = sqlite3.connect('file:' + quote(str(path)) + '?mode=ro', uri=True, timeout=1)
    try:
        conn.execute('PRAGMA query_only=ON')
        for sql in ('SELECT user_id,ruleset_id,payload,fetched FROM cache LIMIT 0',
                    'SELECT query_key,ruleset_id,user_id,username,queued FROM queries LIMIT 0',
                    'SELECT user_id,hidden,observed FROM privacy LIMIT 0',
                    'SELECT username,ruleset_id,user_id FROM names LIMIT 0'):
            conn.execute(sql)
    finally:
        conn.close()


def initialization_record(root):
    marker, _ = load(root)
    path = contained(root, INITIALIZATION)
    if not os.path.lexists(path):
        return None
    value = read_json(path)
    if (type(value) is not dict or set(value) != {'format', 'installation_id', 'generation', 'phase'}
            or type(value['format']) is not int or value['format'] != 1
            or any(value[k] != marker[k] for k in ('installation_id', 'generation'))
            or value['phase'] not in ('prepared', 'complete')):
        raise ValueError('activity_initialization_receipt_requires_review')
    return value


def _initialize(root, marker):
    """Explicit fresh-install seam, with a durable intent before native DDL."""
    path = contained(root, LEDGER)
    intent = initialization_record(root)
    if intent is None:
        # A missing database in an old installation is loss, not a fresh start.
        # OAuth/catalog setup is permitted, but any previous provider run,
        # migration or room history forbids silently reconstructing the ledger.
        if os.path.lexists(path):
            raise ValueError('activity_fresh_initialization_requires_absent_ledger')
        for relative in ('state/provider-service.json', 'state/rating-migration.json',
                         'state/owner-migration.json', RECORD, BACKUP):
            if os.path.lexists(contained(root, relative)):
                raise ValueError('activity_missing_historical_ledger_requires_review')
        journal = contained(root, 'state/rolling/ownership.json')
        if os.path.lexists(journal) and read_json(journal).get('rooms'):
            raise ValueError('activity_prior_room_history_requires_review')
        intent = {'format': 1, **{k: marker[k] for k in ('installation_id', 'generation')}, 'phase': 'prepared'}
        atomic_json(contained(root, INITIALIZATION), intent)
    if intent['phase'] == 'complete':
        return local_status(root)
    if os.path.lexists(path):
        conn = _ledger_connection(path)
        try:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            for row in tables:
                # A previous interrupted schema commit can leave its one bound
                # migration witness. No player/history row is adopted here.
                if row[0] == META and migration_record(root) is not None:
                    continue
                if conn.execute('SELECT 1 FROM ' + _quote(row[0]) + ' LIMIT 1').fetchone():
                    raise ValueError('activity_fresh_initialization_contains_history')
        finally:
            conn.close()
    from bot.ratings import RatingStore
    from bot.ppstats import PPStatsStore
    mask = os.umask(0o077)
    try:
        # Exactly the existing native schema implementations; no fabricated
        # table definitions or migration of unrelated operator paths.
        RatingStore(str(path))
        PPStatsStore(str(path), ruleset_id=0)
        PPStatsStore(str(path), ruleset_id=3)
        # A fresh explicit Catch host prepares its namespace before workers can
        # lock schemas. Existing Standard/Mania installations are unchanged.
        if any(room['preset'].startswith('taiko-') for room in load(root)[1]['rooms']):
            from bot.taiko_ppstats import TaikoPPStatsStore
            from bot.scoped_ratings import ScopedRatingStore
            ScopedRatingStore(str(path),ruleset_id=1)
            TaikoPPStatsStore(str(path),ruleset_id=1)
        if any(room['preset'] == 'catch-fixed' for room in load(root)[1]['rooms']):
            from bot.catch_ppstats import CatchPPStatsStore
            from bot.scoped_ratings import ScopedRatingStore
            ScopedRatingStore(str(path),ruleset_id=2)
            CatchPPStatsStore(str(path), ruleset_id=2)
    finally:
        os.umask(mask)
    _migrate(root, marker)
    atomic_json(contained(root, INITIALIZATION), {**intent, 'phase': 'complete'})
    return local_status(root)


def initialize(root, *, owner_lease_held=False):
    """Explicit stopped fresh initialization; does not opt into any sharing."""
    return _admitted(root, owner_lease_held, _initialize)


def local_status(root):
    """Read-only migration status; never exposes player rows or credentials."""
    marker, _ = load(root)
    initial = initialization_record(root)
    if initial is not None and initial['phase'] != 'complete':
        return {'ok': True, 'capture_enabled': False, 'migration_phase': 'initializing', 'recovery_required': True}
    record = migration_record(root)
    if record is None:
        return {'ok': True, 'capture_enabled': False, 'migration_phase': 'absent', 'recovery_required': False}
    if record['phase'] != 'complete':
        return {'ok': True, 'capture_enabled': False, 'migration_phase': 'prepared', 'recovery_required': True}
    _reader_ready(root)
    path = _private(contained(root, LEDGER))
    conn = sqlite3.connect('file:' + quote(str(path)) + '?mode=ro', uri=True, timeout=5)
    try:
        conn.execute('PRAGMA query_only=ON')
        _witness(conn, marker, record)
    finally:
        conn.close()
    return {'ok': True, 'capture_enabled': True, 'migration_phase': 'complete',
            'recovery_required': False, 'schema_sha256': SCHEMA_SHA256}


def capture_enabled(root):
    result = local_status(root)
    if result['recovery_required']:
        raise ValueError('activity_migration_recovery_required')
    return result['capture_enabled']


def prepare(root, marker, *, uid, owner_lease_held=False):
    """Integration seam: called only inside stopped root pilot_upgrade._locked."""
    from .pilot_installation import require_no_service_processes
    from .ratings.operations import _identity
    if os.geteuid() != 0 or type(uid) is not int or uid <= 0 or owner_lease_held is not True:
        raise ConfigError('activity_migration_requires_root_stopped_upgrade_admission')
    require_no_service_processes(uid)
    # The pinned installer imports this module from a root-private temporary
    # payload. Load every lazy code dependency while root can traverse that
    # payload, then drop identity before any native SQLite or player data access.
    # Never weaken the temporary directory or grant the service UID access to
    # another installation's code/state. These imports perform no room actions.
    from bot.ratings import RatingStore
    from bot.ppstats import PPStatsStore
    # Fresh-install schema initialization selects these stores only after the
    # identity drop. Import their modules now while the staged payload remains
    # traversable; constructors still run only inside _identity(uid), where
    # the existing private-ledger checks and ownership rules apply.
    from bot.scoped_ratings import ScopedRatingStore
    from bot.catch_ppstats import CatchPPStatsStore
    from bot.taiko_ppstats import TaikoPPStatsStore
    from .activity.reader import Reader
    from . import reconciliation, ownership_recovery
    with _identity(uid):
        actual, _ = load(root)
        if actual != marker:
            raise ConfigError('activity_migration_installation_changed')
        if initialization_record(root) is not None or not os.path.lexists(contained(root, LEDGER)):
            return initialize(root, owner_lease_held=True)
        return migrate_ledger(root, owner_lease_held=True)
