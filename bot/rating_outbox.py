"""Immutable rating capture, sharing the real vote's SQLite transaction.

Only the coordinated stopped migration calls prepare(). Ordinary vote writers
never create tables. Transport and acknowledgments live outside chat commands.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid


FORMAT = 1
SCHEMA = (
    "CREATE TABLE rating_sync_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "format INTEGER NOT NULL, installation_id TEXT NOT NULL, generation TEXT NOT NULL, "
    "sequence INTEGER NOT NULL CHECK(sequence>=0), capture_started REAL NOT NULL)",
    "CREATE TABLE rating_revision_cache (beatmap_id INTEGER NOT NULL, user_id INTEGER NOT NULL, "
    "revision INTEGER NOT NULL CHECK(revision>=0), PRIMARY KEY(beatmap_id,user_id))",
    "CREATE TABLE rating_outbox (event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, "
    "beatmap_id INTEGER NOT NULL, user_id INTEGER NOT NULL, payload TEXT NOT NULL, "
    "phase TEXT NOT NULL DEFAULT 'pending', acknowledgment TEXT)",
    "CREATE INDEX rating_outbox_pending ON rating_outbox(phase,sequence)",
    "CREATE INDEX rating_outbox_key_sequence ON rating_outbox(beatmap_id,user_id,sequence)",
)


# Reserved internal SQLite namespace. Never a provider beatmap ID on the wire.
# Legacy map-only keys remain unchanged; Catch native NM has an explicit domain.
CATCH_KEY_BASE = 2**52
MAX_PROVIDER_MAP_ID = 2**32 - 1


def storage_key(beatmap_id, context=None, *, map_origin=None, key_count=None):
    if type(beatmap_id) is not int or not 0 < beatmap_id < CATCH_KEY_BASE:
        raise ValueError('rating_map_identity_invalid')
    value = provenance(context)
    if value['ruleset_id'] != 2:
        if map_origin in ('catch-native-nm', 'native-catch'):
            raise ValueError('rating_catch_identity_required')
        return beatmap_id
    if (beatmap_id > MAX_PROVIDER_MAP_ID or value['required_mods'] or key_count is not None
            or map_origin not in ('native', 'catch-native-nm', 'native-catch')):
        raise ValueError('rating_catch_native_nm_required')
    return CATCH_KEY_BASE + beatmap_id


def map_key(beatmap_id, ruleset_id=None):
    """Translate a public lookup; only explicit Catch stores use its namespace."""
    if ruleset_id == 2:
        return storage_key(beatmap_id, {'ruleset_id': 2}, map_origin='catch-native-nm')
    return storage_key(beatmap_id)


def public_key(key):
    """Closed aggregate identity, with real IDs and an explicit variant."""
    if type(key) is not int or key <= 0:
        raise ValueError('rating_map_identity_invalid')
    if CATCH_KEY_BASE < key <= CATCH_KEY_BASE + MAX_PROVIDER_MAP_ID:
        return key - CATCH_KEY_BASE, 2, 'native-NM'
    if key < CATCH_KEY_BASE:
        return key, None, 'legacy-map'
    raise ValueError('rating_map_identity_invalid')


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError('rating_identity_invalid')
    return value


def prepare(conn, *, installation_id, generation, capture_started):
    """Install capture schema inside the stopped migrator's owned transaction.

    Same identity is repeat-safe. Existing historical votes are not retroactively
    exported: seeding has a separate consistent-boundary reconciliation contract.
    This is not a stop proof; the installed migrator must independently establish
    supervisor/worker/agent absence before calling this internal primitive.
    """
    _uuid(installation_id); _uuid(generation)
    if not conn.in_transaction:
        raise ValueError('rating_migration_transaction_required')
    if type(capture_started) not in (int, float) or not math.isfinite(capture_started) or capture_started < 0:
        raise ValueError('rating_capture_boundary_invalid')
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='rating_sync_meta'").fetchone()
    if exists:
        row = conn.execute('SELECT format,installation_id,generation FROM rating_sync_meta WHERE singleton=1').fetchone()
        if row is None or tuple(row) != (FORMAT, installation_id, generation):
            raise ValueError('rating_migration_identity_mismatch')
        validate(conn)
        return False
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='map_ratings'").fetchone():
        raise ValueError('rating_legacy_schema_required')
    for sql in SCHEMA:
        conn.execute(sql)
    conn.execute('INSERT INTO rating_sync_meta VALUES(1,?,?,?,?,?)',
                 (FORMAT, installation_id, generation, 0, capture_started))
    return True


def validate(conn):
    """Read-only format check; deliberately performs no migration."""
    row = conn.execute('SELECT format,installation_id,generation,sequence FROM rating_sync_meta WHERE singleton=1').fetchone()
    if row is None or row[0] != FORMAT or type(row[3]) is not int or not 0 <= row[3] < 2**53:
        raise ValueError('rating_capture_schema_invalid')
    _uuid(row[1]); _uuid(row[2])
    conn.execute('SELECT beatmap_id,user_id,revision FROM rating_revision_cache LIMIT 0')
    conn.execute('SELECT event_id,sequence,beatmap_id,user_id,payload,phase,acknowledgment FROM rating_outbox LIMIT 0')
    return row


def provenance(context):
    """Minimal closed context, never arbitrary chat/player dictionaries."""
    value = {'ruleset_id': None, 'room_id': None, 'playlist_item_id': None, 'required_mods': []}
    if context is not None:
        if type(context) is not dict or not set(context) <= set(value):
            raise ValueError('rating_context_invalid')
        value.update(context)
    mode = value['ruleset_id']
    if mode is not None and (type(mode) is not int or mode not in (0, 1, 2, 3)):
        raise ValueError('rating_context_invalid')
    for key in ('room_id', 'playlist_item_id'):
        if value[key] is not None and (type(value[key]) is not int or not 0 < value[key] < 2**53):
            raise ValueError('rating_context_invalid')
    mods = value['required_mods']
    if (type(mods) not in (list, tuple) or len(mods) > 16
            or any(type(m) is not str or not re.fullmatch('[A-Z0-9]{1,8}', m) for m in mods)):
        raise ValueError('rating_context_invalid')
    value['required_mods'] = sorted(set(mods))
    return value


def repeat_is_fresh(conn, beatmap_id, user_id):
    """A deliberate reaffirmation after reconciliation/review is new intent.

    Updating revision knowledge alone never queues anything. Pending duplicates
    remain no-ops; the old event is immutable and never rebased. A completed
    canonical epoch review can clear old knowledge but cannot itself queue input.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='rating_sync_meta'").fetchone():
        return False
    validate(conn)
    previous = conn.execute('SELECT phase,payload,acknowledgment FROM rating_outbox WHERE beatmap_id=? AND user_id=? ORDER BY sequence DESC LIMIT 1',
                            (beatmap_id, user_id)).fetchone()
    revision = conn.execute('SELECT revision FROM rating_revision_cache WHERE beatmap_id=? AND user_id=?',
                            (beatmap_id, user_id)).fetchone()
    if previous is None or previous[0] in ('pending','retryable'):return False
    if previous[0] in ('quarantined','permanently_rejected'):
        # Only a new actual command invokes this function. An old quarantined
        # event stays immutable, regardless of whether policy now permits input.
        return True
    if previous[0] in ('accepted','already_accepted'):
        acknowledgment=json.loads(previous[2]) if previous[2] is not None else {}
        return revision is None or revision[0]!=acknowledgment.get('revision')
    return (previous[0] in ('conflict','superseded') and revision is not None
            and revision[0] != json.loads(previous[1])['base_revision'])


def capture(conn, *, beatmap_id, user_id, rating, observed_at, map_origin, key_count, context=None):
    """Append immutable intent before the caller commits its real vote.

    Legacy ledgers remain local-only. A migrated ledger failing validation raises,
    causing the caller to roll back the real vote as well as the export attempt.
    """
    if not conn.in_transaction:
        raise ValueError('rating_capture_transaction_required')
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='rating_sync_meta'").fetchone():
        return None
    meta = validate(conn)
    if any(type(v) is not int or not 0 < v < 2**53 for v in (beatmap_id, user_id)) or type(rating) is not int or rating not in (-1, 0, 1):
        raise ValueError('rating_intent_invalid')
    if (type(observed_at) not in (int, float) or not math.isfinite(observed_at) or not 0 <= observed_at < 2**53
            or type(map_origin) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,32}', map_origin)
            or (key_count is not None and (type(key_count) is not int or key_count not in (4, 7)))):
        raise ValueError('rating_intent_invalid')
    if meta[3] >= 2**53-1:
        raise ValueError('rating_sequence_exhausted')
    key = storage_key(beatmap_id, context, map_origin=map_origin, key_count=key_count)
    previous = conn.execute('SELECT event_id FROM rating_outbox WHERE beatmap_id=? AND user_id=? ORDER BY sequence DESC LIMIT 1',
                            (key, user_id)).fetchone()
    revision = conn.execute('SELECT revision FROM rating_revision_cache WHERE beatmap_id=? AND user_id=?',
                            (key, user_id)).fetchone()
    event_id = str(uuid.uuid4())
    event = {'format': 2 if key >= CATCH_KEY_BASE else FORMAT, 'event_id': event_id, 'installation_id': meta[1],
             'generation': meta[2], 'sequence': meta[3]+1, 'kind': 'rating_set',
             'beatmap_id': beatmap_id, 'user_id': user_id, 'rating': rating,
             'base_revision': revision[0] if revision else None,
             'predecessor': previous[0] if previous else None,
             'observed_at': observed_at, 'map_origin': map_origin, 'key_count': key_count,
             'context': provenance(context)}
    payload = json.dumps(event, sort_keys=True, separators=(',', ':'), allow_nan=False)
    conn.execute('INSERT INTO rating_outbox(event_id,sequence,beatmap_id,user_id,payload) VALUES(?,?,?,?,?)',
                 (event_id, event['sequence'], key, user_id, payload))
    conn.execute('UPDATE rating_sync_meta SET sequence=? WHERE singleton=1', (event['sequence'],))
    return event_id
