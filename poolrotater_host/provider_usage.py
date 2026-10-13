"""Observational provider usage; pacing JSON remains the sole admission ledger.

Private fixed-path telemetry never accepts credentials, IDs, paths or bodies.
Independent epochs distinguish replaced pacing ledgers and pre-observation
counter baselines. HTTP starts include OAuth/REST and referee HTTP handshakes;
referee WebSocket message frames are not observed here. Readers do not create,
repair or update anything. Telemetry failures never retry a provider request.
Raw records retain six hours, UTC daily rollups retain 366 days, and independent
epoch totals survive both pruning windows. Old v6 usage remains a separate
archive; pre-observation counters never fabricate missing detailed history.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

from .config import ConfigError, contained
from .layout import atomic_json, checked_root, file_lease, read_json

PATH = 'state/provider-usage.db'
LOCK = 'state/provider-usage.lock'
HEALTH = 'state/provider-usage-health.json'
RETENTION = 21600.
MAX_ROWS = 50000
MAX_EPOCHS = 64
DAILY_RETENTION_DAYS = 366
MAX_BYTES = 32 * 1024**2
COUNTERS = ('attempted', 'dispatched', 'replies', 'rate_limited', 'uncertain', 'deferred')
EXECUTIONS = ('provider-disabled', 'provider-fixture', 'provider-pilot', 'provider-live-pilot')
WINDOWS = (('30s', 30.), ('1m', 60.), ('5m', 300.), ('10m', 600.), ('30m', 1800.), ('1h', 3600.), ('4h', 14400.))
log = logging.getLogger(__name__)

SCHEMA = (
    'CREATE TABLE usage_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
    'CREATE TABLE usage_epochs(epoch TEXT PRIMARY KEY,source_ledger TEXT UNIQUE NOT NULL,execution TEXT NOT NULL,started_at REAL NOT NULL,last_at REAL NOT NULL,baseline TEXT NOT NULL,counters TEXT NOT NULL,gaps INTEGER NOT NULL,http_started INTEGER NOT NULL DEFAULT 0,http_replied INTEGER NOT NULL DEFAULT 0,http_429 INTEGER NOT NULL DEFAULT 0,http_uncertain INTEGER NOT NULL DEFAULT 0,delayed_reservations INTEGER NOT NULL DEFAULT 0,scheduled_seconds REAL NOT NULL DEFAULT 0,max_scheduled_seconds REAL NOT NULL DEFAULT 0,observed_elapsed_seconds REAL NOT NULL DEFAULT 0)',
    'CREATE TABLE usage_daily(epoch TEXT NOT NULL,day TEXT NOT NULL,http_started INTEGER NOT NULL DEFAULT 0,http_replied INTEGER NOT NULL DEFAULT 0,http_429 INTEGER NOT NULL DEFAULT 0,http_uncertain INTEGER NOT NULL DEFAULT 0,delayed_reservations INTEGER NOT NULL DEFAULT 0,scheduled_seconds REAL NOT NULL DEFAULT 0,max_scheduled_seconds REAL NOT NULL DEFAULT 0,observed_elapsed_seconds REAL NOT NULL DEFAULT 0,PRIMARY KEY(epoch,day))',
    'CREATE TABLE api_request_events(id INTEGER PRIMARY KEY AUTOINCREMENT,requested_at REAL NOT NULL,status_code INTEGER)',
    'CREATE INDEX idx_api_request_events_time ON api_request_events(requested_at)',
    'CREATE TABLE provider_requests(epoch TEXT NOT NULL,sequence INTEGER NOT NULL,kind TEXT NOT NULL,event_id INTEGER NOT NULL,requested_at REAL NOT NULL,replied_at REAL,duration_seconds REAL,status_code INTEGER,uncertain INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(epoch,sequence))',
    'CREATE TABLE api_rate_waiters(reservation_key TEXT PRIMARY KEY,created_at REAL NOT NULL,ready_at REAL NOT NULL)',
    'CREATE TABLE usage_waits(id INTEGER PRIMARY KEY,epoch TEXT NOT NULL,sequence INTEGER NOT NULL,created_at REAL NOT NULL,scheduled_seconds REAL NOT NULL,elapsed_seconds REAL,UNIQUE(epoch,sequence))',
)


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1e12:
        raise ConfigError('provider usage sample refused')
    return float(value)


def _counters(value):
    if (type(value) is not dict or set(value) != set(COUNTERS)
            or any(type(v) is not int or not 0 <= v <= 2**63 - 1 for v in value.values())):
        raise ConfigError('provider usage counter sample refused')
    return dict(value)


def _private(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_BYTES):
        raise ConfigError('provider usage file refused')
    return info.st_dev, info.st_ino


@contextmanager
def _database(root, *, write=False):
    path = contained(checked_root(Path(root)), PATH)
    fresh = False
    if write and not os.path.lexists(path):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd); fresh = True
    identity = _private(path)
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=.05)
    try:
        db.execute('PRAGMA busy_timeout=50')
        if write:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute('BEGIN IMMEDIATE')
        else: db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
        if fresh:
            for statement in SCHEMA: db.execute(statement)
            db.execute('INSERT INTO usage_meta VALUES(?,?)', ('format', '1'))
            db.execute('INSERT INTO usage_meta VALUES(?,?)', ('discarded_through', '0'))
        expected = {statement.split()[2].split('(')[0]: statement for statement in SCHEMA}
        actual = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        if actual != expected: raise ConfigError('provider usage schema refused')
        if db.execute("SELECT value FROM usage_meta WHERE key='format'").fetchone() != ('1',):
            raise ConfigError('provider usage format refused')
        yield db
        if _private(path) != identity: raise ConfigError('provider usage file changed')
        if write: db.commit()
    finally: db.close()


def _health(root):
    path = contained(root, HEALTH)
    if not os.path.lexists(path): return {'format': 1, 'failures': 0, 'last_failure_at': None}
    _private(path)
    value = read_json(path, maximum=4096)
    if (not isinstance(value, dict) or set(value) != {'format', 'failures', 'last_failure_at'}
            or value['format'] != 1 or type(value['failures']) is not int or not 0 <= value['failures'] <= 2**63 - 1):
        raise ConfigError('provider usage health refused')
    if value['last_failure_at'] is not None: _number(value['last_failure_at'])
    return value


def _failure(root):
    # Best effort evidence, with a fixed safe log even if the entire state mount
    # is unavailable. No exception string, credential or filesystem path leaks.
    log.warning('provider usage telemetry unavailable; pacing remains authoritative')
    try:
        root = checked_root(Path(root))
        health = _health(root)
        health.update(failures=min(2**63 - 1, health['failures'] + 1), last_failure_at=time.time())
        atomic_json(contained(root, HEALTH), health)
    except Exception: pass


def _prune(db, now):
    discarded = db.execute('SELECT MAX(requested_at) FROM provider_requests WHERE requested_at<?', (now - RETENTION,)).fetchone()[0]
    db.execute('DELETE FROM provider_requests WHERE requested_at<?', (now - RETENTION,))
    overflow = db.execute('SELECT MAX(requested_at) FROM provider_requests WHERE rowid NOT IN (SELECT rowid FROM provider_requests ORDER BY rowid DESC LIMIT ?)', (MAX_ROWS,)).fetchone()[0]
    db.execute('DELETE FROM provider_requests WHERE rowid NOT IN (SELECT rowid FROM provider_requests ORDER BY rowid DESC LIMIT ?)', (MAX_ROWS,))
    db.execute('DELETE FROM api_request_events WHERE id NOT IN (SELECT event_id FROM provider_requests)')
    db.execute('DELETE FROM usage_waits WHERE created_at<? OR id NOT IN (SELECT id FROM usage_waits ORDER BY id DESC LIMIT ?)', (now - RETENTION, MAX_ROWS))
    db.execute('DELETE FROM api_rate_waiters WHERE ready_at<?', (now - RETENTION,))
    db.execute('DELETE FROM usage_daily WHERE day<?', (_day(max(0., now - DAILY_RETENTION_DAYS * 86400)),))
    previous = float(db.execute("SELECT value FROM usage_meta WHERE key='discarded_through'").fetchone()[0])
    db.execute("UPDATE usage_meta SET value=? WHERE key='discarded_through'", (str(max(previous, discarded or 0., overflow or 0.)),))


def _day(when):
    return datetime.fromtimestamp(when, timezone.utc).strftime('%Y-%m-%d')


def _daily(db, epoch, when, **metrics):
    allowed = {'http_started','http_replied','http_429','http_uncertain','delayed_reservations',
               'scheduled_seconds','max_scheduled_seconds','observed_elapsed_seconds'}
    if not metrics or not set(metrics) <= allowed: raise ConfigError('provider usage rollup refused')
    day = _day(when)
    db.execute('INSERT OR IGNORE INTO usage_daily(epoch,day) VALUES(?,?)', (epoch, day))
    clauses = [name + '=MAX(' + name + ',?)' if name == 'max_scheduled_seconds' else name + '=' + name + '+?'
               for name in metrics]
    values = tuple(metrics.values())
    db.execute('UPDATE usage_daily SET ' + ','.join(clauses) + ' WHERE epoch=? AND day=?', (*values, epoch, day))
    db.execute('UPDATE usage_epochs SET ' + ','.join(clauses) + ' WHERE epoch=?', (*values, epoch))


def record(root, *, ledger, execution, before, after, event='counters', sequence=None,
           kind='http', status=None, duration=None, delay=0., elapsed=None, now=None):
    """Return success/failure only. Never change pacing, authority or dispatch."""
    try:
        root = checked_root(Path(root)); now = _number(time.time() if now is None else now)
        if str(uuid.UUID(ledger)) != ledger or execution not in EXECUTIONS:
            raise ConfigError('provider usage epoch refused')
        before, after = _counters(before), _counters(after)
        if any(after[key] < before[key] for key in COUNTERS): raise ConfigError('provider usage counter regression')
        if event not in ('counters', 'started', 'replied', 'uncertain', 'wait', 'wait_finished') or kind not in ('http', 'referee'):
            raise ConfigError('provider usage event refused')
        if sequence is not None and (type(sequence) is not int or not 1 <= sequence <= 2**63 - 1):
            raise ConfigError('provider usage sequence refused')
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise ConfigError('provider usage status refused')
        delay = _number(delay)
        if duration is not None: duration = _number(duration)
        if elapsed is not None: elapsed = _number(elapsed)
        with file_lease(contained(root, LOCK), timeout=.05), _database(root, write=True) as db:
            row = db.execute('SELECT epoch,counters,execution FROM usage_epochs WHERE source_ledger=?', (ledger,)).fetchone()
            if row is None:
                if db.execute('SELECT COUNT(*) FROM usage_epochs').fetchone()[0] >= MAX_EPOCHS:
                    raise ConfigError('provider usage epoch budget exceeded')
                epoch = str(uuid.uuid4())
                db.execute('INSERT INTO usage_epochs(epoch,source_ledger,execution,started_at,last_at,baseline,counters,gaps) VALUES(?,?,?,?,?,?,?,0)',
                    (epoch, ledger, execution, now, now, json.dumps(before), json.dumps(before)))
                last = before
            else:
                epoch, raw, prior_execution = row
                if prior_execution != execution: raise ConfigError('provider usage epoch execution changed')
                last = _counters(json.loads(raw))
            gaps = int(last != before)
            db.execute('UPDATE usage_epochs SET counters=?,last_at=?,gaps=gaps+? WHERE epoch=?',
                       (json.dumps(after), now, gaps, epoch))
            if event == 'started':
                existing = db.execute('SELECT event_id FROM provider_requests WHERE epoch=? AND sequence=?', (epoch, sequence)).fetchone()
                if existing is None:
                    cursor = db.execute('INSERT INTO api_request_events(requested_at,status_code) VALUES(?,NULL)', (now,))
                    db.execute('INSERT INTO provider_requests(epoch,sequence,kind,event_id,requested_at) VALUES(?,?,?,?,?)',
                               (epoch, sequence, kind, cursor.lastrowid, now))
                    _daily(db, epoch, now, http_started=1)
            elif event in ('replied', 'uncertain'):
                row = db.execute('SELECT event_id,replied_at,uncertain,requested_at FROM provider_requests WHERE epoch=? AND sequence=?', (epoch, sequence)).fetchone()
                if row is None: db.execute('UPDATE usage_epochs SET gaps=gaps+1 WHERE epoch=?', (epoch,))
                elif event == 'replied':
                    if row[1] is None:
                        _daily(db, epoch, row[3], http_replied=1, http_429=int(status == 429))
                    db.execute('UPDATE provider_requests SET replied_at=?,status_code=?,duration_seconds=? WHERE epoch=? AND sequence=?', (now, status, duration, epoch, sequence))
                    db.execute('UPDATE api_request_events SET status_code=? WHERE id=?', (status, row[0]))
                else:
                    if not row[2]: _daily(db, epoch, row[3], http_uncertain=1)
                    db.execute('UPDATE provider_requests SET uncertain=1,duration_seconds=COALESCE(duration_seconds,?) WHERE epoch=? AND sequence=?', (duration, epoch, sequence))
            elif event == 'wait':
                key = epoch + ':' + str(sequence)
                inserted = db.execute('INSERT OR IGNORE INTO usage_waits(epoch,sequence,created_at,scheduled_seconds) VALUES(?,?,?,?)', (epoch, sequence, now, delay))
                if inserted.rowcount:
                    db.execute('INSERT OR IGNORE INTO api_rate_waiters VALUES(?,?,?)', (key, now, now + delay))
                    _daily(db, epoch, now, delayed_reservations=1, scheduled_seconds=delay, max_scheduled_seconds=delay)
            elif event == 'wait_finished':
                db.execute('DELETE FROM api_rate_waiters WHERE reservation_key=?', (epoch + ':' + str(sequence),))
                prior = db.execute('SELECT created_at,elapsed_seconds FROM usage_waits WHERE epoch=? AND sequence=?', (epoch, sequence)).fetchone()
                if prior is not None and prior[1] is None and elapsed is not None:
                    _daily(db, epoch, prior[0], observed_elapsed_seconds=elapsed)
                    db.execute('UPDATE usage_waits SET elapsed_seconds=? WHERE epoch=? AND sequence=?', (elapsed, epoch, sequence))
            _prune(db, now)
        return True
    except Exception:
        _failure(root)
        return False


def snapshot(root, *, authoritative=None, now=None):
    """Bounded read-only public metrics. No source-ledger or authority hashes."""
    result = {'format': 1, 'source': 'observed-provider-exchanges', 'available': False,
              'history_complete': False, 'telemetry_gap': True, 'retention_seconds': RETENTION,
              'legacy_epoch_included': False}
    try:
        root = checked_root(Path(root)); now = _number(time.time() if now is None else now)
        health = _health(root)
        with _database(root) as db:
            rows = db.execute('SELECT epoch,execution,started_at,last_at,baseline,counters,gaps,http_started,http_replied,http_429,http_uncertain,delayed_reservations,scheduled_seconds,max_scheduled_seconds,observed_elapsed_seconds FROM usage_epochs ORDER BY started_at,rowid').fetchall()
            if len(rows) > MAX_EPOCHS: raise ConfigError('provider usage epoch budget exceeded')
            epochs = [{'epoch': row[0], 'execution': row[1], 'started_at': row[2], 'last_at': row[3],
                       'pre_observation_counters': _counters(json.loads(row[4])),
                       'authoritative_counters': _counters(json.loads(row[5])), 'coverage_gaps': row[6],
                       'observed_http_totals': dict(zip(('attempted','replied','rate_limited','uncertain'), row[7:11])),
                       'observed_wait_totals': dict(zip(('count','scheduled_seconds','max_scheduled_seconds','observed_elapsed_seconds'), row[11:]))} for row in rows]
            for epoch in epochs:
                if (str(uuid.UUID(epoch['epoch'])) != epoch['epoch'] or epoch['execution'] not in EXECUTIONS
                        or type(epoch['coverage_gaps']) is not int or epoch['coverage_gaps'] < 0):
                    raise ConfigError('provider usage epoch refused')
                _number(epoch['started_at']); _number(epoch['last_at'])
                for total in (*epoch['observed_http_totals'].values(), *epoch['observed_wait_totals'].values()): _number(total)
            current = epochs[-1]['authoritative_counters'] if epochs else None
            gap = health['failures'] > 0 or any(row['coverage_gaps'] for row in epochs)
            if authoritative is not None: gap = gap or current != _counters(authoritative)
            discarded = float(db.execute("SELECT value FROM usage_meta WHERE key='discarded_through'").fetchone()[0])
            windows = []
            for label, seconds in WINDOWS:
                count, limited, uncertain, pending = db.execute('SELECT COUNT(*),SUM(status_code=429),SUM(uncertain),SUM(replied_at IS NULL AND uncertain=0) FROM provider_requests WHERE requested_at>=?', (now - seconds,)).fetchone()
                windows.append({'label': label, 'seconds': seconds, 'count': count, 'rpm': count * 60. / seconds,
                    'rate_limited': limited or 0, 'uncertain': uncertain or 0,
                    'unfinished_exchanges': pending or 0,
                    'coverage_complete': bool(epochs) and not gap and not pending and epochs[0]['started_at'] <= now - seconds and discarded < now - seconds})
            waiting, ready = db.execute('SELECT COUNT(*),MIN(ready_at) FROM api_rate_waiters WHERE ready_at>?', (now,)).fetchone()
            waits = db.execute('SELECT COUNT(*),SUM(scheduled_seconds),MAX(scheduled_seconds),SUM(elapsed_seconds) FROM usage_waits WHERE created_at>=?', (now - 14400.,)).fetchone()
            kinds = dict(db.execute('SELECT kind,COUNT(*) FROM provider_requests WHERE requested_at>=? GROUP BY kind', (now - 14400.,)))
            if not set(kinds) <= {'http','referee'}: raise ConfigError('provider usage kind refused')
            incomplete = db.execute('SELECT COUNT(*) FROM provider_requests WHERE replied_at IS NULL AND uncertain=0').fetchone()[0]
            daily = [dict(zip(('epoch','day','http_started','http_replied','http_429','http_uncertain',
                              'delayed_reservations','scheduled_seconds','max_scheduled_seconds','observed_elapsed_seconds'), row))
                     for row in db.execute('SELECT * FROM usage_daily ORDER BY day,epoch LIMIT ?', (MAX_EPOCHS * (DAILY_RETENTION_DAYS + 2),))]
            known = {epoch['epoch'] for epoch in epochs}
            for row in daily:
                if row['epoch'] not in known or datetime.strptime(row['day'], '%Y-%m-%d').strftime('%Y-%m-%d') != row['day']:
                    raise ConfigError('provider usage daily epoch refused')
                for key in set(row) - {'epoch','day'}: _number(row[key])
        totals = {key: sum(epoch['authoritative_counters'][key] for epoch in epochs) for key in COUNTERS}
        http_totals = {key: sum(epoch['observed_http_totals'][key] for epoch in epochs)
                       for key in ('attempted','replied','rate_limited','uncertain')}
        wait_totals = {key: (max((epoch['observed_wait_totals'][key] for epoch in epochs), default=0.) if key == 'max_scheduled_seconds'
                            else sum(epoch['observed_wait_totals'][key] for epoch in epochs))
                       for key in ('count','scheduled_seconds','max_scheduled_seconds','observed_elapsed_seconds')}
        return {**result, 'available': True, 'telemetry_gap': gap, 'health': health,
                'epochs': epochs, 'overall_provider_counters': totals, 'overall_observed_http_totals': http_totals,
                'windows': windows, 'history_complete': bool(epochs) and not gap and not incomplete,
                'overall_observed_wait_totals': wait_totals, 'daily': daily,
                'daily_retention_days': DAILY_RETENTION_DAYS,
                'http_kind_counts_4h': kinds,
                'unfinished_exchanges': incomplete,
                'queue': {'observed_waiting': waiting, 'head_ready_seconds': max(0., ready - now) if ready else 0.,
                          'lease_waiters_included': False},
                'waits_4h': {'count': waits[0], 'scheduled_seconds': waits[1] or 0.,
                            'max_scheduled_seconds': waits[2] or 0., 'observed_elapsed_seconds': waits[3] or 0.}}
    except Exception: return result


def summary(root, *, authoritative=None):
    value = snapshot(root, authoritative=authoritative)
    if not value['available']: return 'provider usage unavailable; telemetry coverage gap; pacing remains authoritative'
    hour = value['windows'][-2]
    return ('provider HTTP usage 1h: ' + str(hour['count']) + ' attempts, ' + str(hour['rate_limited'])
            + ' HTTP 429, ' + str(hour['uncertain']) + ' uncertain; telemetry_gap=' + str(value['telemetry_gap']).lower())
