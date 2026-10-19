"""Private hub recovery queue. Administrative requests have no public route."""
from contextlib import contextmanager
import json
import os
import sqlite3
import stat
import uuid

from ..network.contracts import canonical, fingerprint, identifier, require, verifier
from .contracts import ACTIONS, exchange, request, digest


class Repository:
    def __init__(self, store):
        self.store = store
        self.path = store.root / 'recovery.sqlite3'
        if not os.path.lexists(self.path):
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        with self.database() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reports(host TEXT PRIMARY KEY, sequence INTEGER NOT NULL, '
                       'digest TEXT NOT NULL, payload TEXT NOT NULL, received REAL NOT NULL, response TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, host TEXT NOT NULL, '
                       'payload TEXT NOT NULL, receipt TEXT, created REAL NOT NULL, expires REAL NOT NULL)')

    @contextmanager
    def database(self):
        for suffix in ('', '-journal', '-wal', '-shm'):
            path = self.path.with_name(self.path.name + suffix)
            if not os.path.lexists(path):
                continue
            info = path.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and info.st_nlink == 1 and not info.st_mode & 0o077
                    and info.st_size <= 16 * 1024**2, 'hub_storage_unavailable', 503)
        db = sqlite3.connect(self.path, timeout=2, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA max_page_count=4096')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def accept(self, token, value):
        exchange(value)
        now = self.store.clock()
        require(abs(now - value['sent_at']) <= 300, 'invalid_request')
        with self.store.transaction() as authority:
            host, _ = self.store._authenticate(authority, verifier(token), 'heartbeat')
            require(all(value[k] == host[k] for k in ('installation_id', 'generation')),
                    'rescue_binding_changed', 403)
            with self.database() as db:
                previous = db.execute('SELECT * FROM reports WHERE host=?', (host['id'],)).fetchone()
                if previous:
                    require(value['sequence'] >= previous['sequence'], 'stale_sequence', 409)
                    if value['sequence'] == previous['sequence']:
                        require(fingerprint(value) == previous['digest'], 'sequence_conflict', 409)
                        return json.loads(previous['response'])
                    require(now - previous['received'] >= 5, 'heartbeat_rate_limited', 429)
                if value['receipt'] is not None:
                    receipt = value['receipt']
                    row = db.execute('SELECT * FROM requests WHERE id=? AND host=?',
                                     (receipt['id'], host['id'])).fetchone()
                    require(row is not None, 'unknown_rescue_receipt', 409)
                    raw = canonical(receipt).decode()
                    require(row['receipt'] is None or row['receipt'] == raw, 'rescue_receipt_conflict', 409)
                    db.execute('UPDATE requests SET receipt=? WHERE id=?', (raw, receipt['id']))
                pending = db.execute('SELECT payload FROM requests WHERE host=? AND receipt IS NULL '
                                     'AND expires>? ORDER BY created,id LIMIT 1', (host['id'], now)).fetchone()
                response = {'ok': True, 'sequence': value['sequence'], 'received_at': now,
                            'request': json.loads(pending['payload']) if pending else None}
                db.execute('INSERT OR REPLACE INTO reports VALUES(?,?,?,?,?,?)',
                           (host['id'], value['sequence'], fingerprint(value), canonical(value).decode(),
                            now, canonical(response).decode()))
                db.execute('DELETE FROM requests WHERE expires<?', (now - 7 * 86400,))
                db.execute('DELETE FROM reports WHERE received<?', (now - 7 * 86400,))
                return response

    def queue(self, host_id, action, *, expected_job_sha256=None, expires_seconds=600):
        """Local administrator only; bind to a fresh independent observation."""
        identifier(host_id)
        require(action in ACTIONS and type(expires_seconds) is int and 60 <= expires_seconds <= 1800,
                'invalid_rescue_action')
        now = self.store.clock()
        with self.store.transaction() as authority:
            host = authority.execute('SELECT * FROM hosts WHERE id=? AND revoked=0', (host_id,)).fetchone()
            require(host is not None, 'host_not_found', 404)
            with self.database() as db:
                report = db.execute('SELECT * FROM reports WHERE host=?', (host_id,)).fetchone()
                require(report is not None and 0 <= now - report['received'] <= 180,
                        'rescue_fresh_observation_required', 409)
                value = json.loads(report['payload'])
                require(all(value[k] == host[k] for k in ('installation_id', 'generation')),
                        'rescue_binding_changed', 409)
                seen = value['observation']
                if action == 'complete-update':
                    digest(expected_job_sha256)
                    require(seen['phase'] not in ('absent', 'complete', 'cancelled', 'unavailable')
                            and seen['job_sha256'] == expected_job_sha256,
                            'rescue_operation_changed', 409)
                if action == 'start-failed-service':
                    digest(expected_job_sha256)
                    require(seen['phase'] == 'service_failed' and seen['job_sha256'] == expected_job_sha256,
                            'rescue_operation_changed', 409)
                require(db.execute('SELECT 1 FROM requests WHERE host=? AND receipt IS NULL AND expires>?',
                                   (host_id, now)).fetchone() is None, 'rescue_request_pending', 409)
                command = request({'id': str(uuid.uuid4()), 'host_id': host_id,
                    'installation_id': host['installation_id'], 'generation': host['generation'],
                    'action': action, 'expires_at': now + expires_seconds,
                    **{k: seen[k] for k in ('operation_id', 'candidate_sha256', 'job_sha256')}})
                db.execute('INSERT INTO requests VALUES(?,?,?,NULL,?,?)',
                           (command['id'], host_id, canonical(command).decode(), now, command['expires_at']))
                self.store._audit(authority, 'recovery_requested', host_id, command['id'])
                return command

    def local(self, host_id):
        identifier(host_id)
        with self.database() as db:
            report = db.execute('SELECT payload,received FROM reports WHERE host=?', (host_id,)).fetchone()
            rows = db.execute('SELECT payload,receipt FROM requests WHERE host=? ORDER BY created DESC LIMIT 8',
                              (host_id,)).fetchall()
        return {'ok': True, 'report': json.loads(report['payload']) if report else None,
                'received_at': report['received'] if report else None,
                'requests': [{'request': json.loads(r['payload']),
                              'receipt': json.loads(r['receipt']) if r['receipt'] else None} for r in rows]}
