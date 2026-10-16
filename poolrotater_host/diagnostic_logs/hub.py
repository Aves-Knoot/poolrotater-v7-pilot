"""Private authenticated central diagnostics with independent consent.

No public directory route exposes this database. Search is for a local hub
administrator only; output remains untrusted text and must be escaped in HTML.
"""
import json
import time
import uuid

from ..network.contracts import (canonical, identifier, integer, number, require,
                                 text, verifier)
from .common import digest, label, private_database, transaction, unpack
from .redact import scrub

SCHEMA = '''
CREATE TABLE IF NOT EXISTS consent(host TEXT PRIMARY KEY,installation TEXT NOT NULL,generation TEXT NOT NULL,
 operational INTEGER NOT NULL,public_chat INTEGER NOT NULL,changed REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chunks(host TEXT NOT NULL,id TEXT NOT NULL,digest TEXT NOT NULL,
 receipt TEXT NOT NULL,received REAL NOT NULL,PRIMARY KEY(host,id));
CREATE TABLE IF NOT EXISTS logs(sequence INTEGER PRIMARY KEY AUTOINCREMENT,host TEXT NOT NULL,
 event_id TEXT NOT NULL,event_sequence INTEGER NOT NULL,event_hash TEXT NOT NULL,at REAL NOT NULL,received REAL NOT NULL,
 room TEXT,version TEXT NOT NULL,source TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,
 epoch TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,bytes INTEGER NOT NULL,
 UNIQUE(host,event_id));
CREATE INDEX IF NOT EXISTS logs_search ON logs(host,room,version,at);
CREATE TABLE IF NOT EXISTS losses(host TEXT NOT NULL,reason TEXT NOT NULL,count INTEGER NOT NULL,
 at REAL NOT NULL,PRIMARY KEY(host,reason));
'''


class HubLogs:
    def __init__(self, network_store, *, secret_provider=lambda: (), clock=None,
                 retention_seconds=14*86400, host_quota_bytes=64*1024**2,
                 quota_bytes=512*1024**2):
        self.network = network_store
        self.clock = clock or network_store.clock
        self.secret_provider = secret_provider
        require(type(retention_seconds) is int and 60 <= retention_seconds <= 90*86400, 'invalid_log_retention')
        require(type(host_quota_bytes) is int and 65536 <= host_quota_bytes <= 512*1024**2, 'invalid_log_quota')
        require(type(quota_bytes) is int and host_quota_bytes <= quota_bytes <= 2*1024**3, 'invalid_log_quota')
        self.retention, self.host_quota, self.quota = retention_seconds, host_quota_bytes, quota_bytes
        self.path = private_database(network_store.root, 'diagnostic-logs.sqlite3')
        with transaction(self.path) as db:
            for statement in SCHEMA.split(';'):
                if statement.strip(): db.execute(statement)

    @staticmethod
    def _binding(host, value):
        identifier(value.get('installation_id')); identifier(value.get('generation'))
        require(value['installation_id'] == host['installation_id'] and value['generation'] == host['generation'], 'host_identity_mismatch', 403)

    def consent(self, token, value):
        require(type(value) is dict and set(value) == {'format', 'installation_id', 'generation', 'operational', 'public_chat'}, 'invalid_log_consent')
        require(type(value['format']) is int and value['format'] == 1 and type(value['operational']) is bool and type(value['public_chat']) is bool and (value['operational'] or not value['public_chat']), 'invalid_log_consent')
        with self.network.transaction() as auth_db:
            host, _ = self.network._authenticate(auth_db, verifier(token), 'heartbeat')
            self._binding(host, value)
            with transaction(self.path) as db:
                db.execute('INSERT OR REPLACE INTO consent VALUES(?,?,?,?,?,?)',
                           (host['id'], host['installation_id'], host['generation'], int(value['operational']), int(value['public_chat']), self.clock()))
                if not value['operational']:
                    db.execute('DELETE FROM logs WHERE host=?', (host['id'],))
                    db.execute('DELETE FROM chunks WHERE host=?', (host['id'],))
                elif not value['public_chat']:
                    db.execute("DELETE FROM logs WHERE host=? AND kind IN ('chat','command')", (host['id'],))
                return {'ok': True, 'host_id': host['id'], 'operational': value['operational'],
                        'public_chat': value['public_chat'], 'changed_at': self.clock()}

    def _loss(self, db, host, reason, count):
        if not count: return
        db.execute('INSERT INTO losses VALUES(?,?,?,?) ON CONFLICT(host,reason) DO UPDATE SET count=count+excluded.count,at=excluded.at', (host, reason, count, self.clock()))

    def _prune(self, db):
        expired = db.execute('SELECT host,count(*) n FROM logs WHERE received<? GROUP BY host', (self.clock()-self.retention,)).fetchall()
        for row in expired: self._loss(db, row['host'], 'hub_retention_expired', row['n'])
        db.execute('DELETE FROM logs WHERE received<?', (self.clock()-self.retention,))
        db.execute('DELETE FROM chunks WHERE received<?', (self.clock()-self.retention,))
        # Two bounded quotas, with durable explicit coverage loss counters.
        hosts = db.execute('SELECT host,sum(bytes) used FROM logs GROUP BY host').fetchall()
        for host in hosts:
            used, count = host['used'], 0
            if used > self.host_quota:
                for row in db.execute('SELECT sequence,bytes FROM logs WHERE host=? ORDER BY sequence', (host['host'],)):
                    if used <= self.host_quota: break
                    db.execute('DELETE FROM logs WHERE sequence=?', (row['sequence'],))
                    used -= row['bytes']; count += 1
            self._loss(db, host['host'], 'hub_host_quota_evicted_oldest', count)
        used = db.execute('SELECT COALESCE(sum(bytes),0) FROM logs').fetchone()[0]
        counts = {}
        if used > self.quota:
            for row in db.execute('SELECT sequence,host,bytes FROM logs ORDER BY sequence'):
                if used <= self.quota: break
                db.execute('DELETE FROM logs WHERE sequence=?', (row['sequence'],))
                used -= row['bytes']; counts[row['host']] = counts.get(row['host'], 0) + 1
        for host, count in counts.items(): self._loss(db, host, 'hub_global_quota_evicted_oldest', count)

    def ingest(self, token, value):
        require(type(value) is dict and set(value) == {'format', 'installation_id', 'generation', 'chunk_id', 'payload'}, 'invalid_log_chunk')
        require(type(value['format']) is int and value['format'] == 1, 'invalid_log_chunk')
        require(type(value['chunk_id']) is str and len(value['chunk_id']) == 64, 'invalid_log_chunk')
        # Authenticate before spending decompression or secret-provider work.
        with self.network.transaction() as auth_db:
            host, _ = self.network._authenticate(auth_db, verifier(token), 'heartbeat')
            self._binding(host, value)
            with transaction(self.path) as db:
                choice = db.execute('SELECT * FROM consent WHERE host=?', (host['id'],)).fetchone()
                require(choice is not None and choice['operational'] and choice['installation'] == host['installation_id'] and choice['generation'] == host['generation'], 'log_sharing_not_opted_in', 403)
                records = unpack(value['payload'])
                require(digest(records) == value['chunk_id'], 'log_chunk_digest_mismatch')
                require(choice['public_chat'] or all(row['kind'] not in ('chat', 'command') for row in records), 'log_chat_not_opted_in', 403)
                request_digest = digest(value)
                previous = db.execute('SELECT * FROM chunks WHERE host=? AND id=?', (host['id'], value['chunk_id'])).fetchone()
                if previous:
                    require(previous['digest'] == request_digest, 'log_chunk_conflict', 409)
                    return json.loads(previous['receipt'])
                secrets = tuple(self.secret_provider()) + (token,)
                accepted, expired = 0, 0
                for row in records:
                    require(row['at'] <= self.clock() + 300, 'future_log_observation')
                    if row['at'] < self.clock() - self.retention:
                        expired += 1; continue
                    safe = scrub(row['text'], secrets)
                    old = db.execute('SELECT event_hash FROM logs WHERE host=? AND event_id=?', (host['id'], row['id'])).fetchone()
                    if old:
                        require(old['event_hash'] == digest(row), 'log_event_conflict', 409)
                        continue
                    db.execute('INSERT INTO logs(host,event_id,event_sequence,event_hash,at,received,room,version,source,kind,text,epoch,start,end,bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (host['id'], row['id'], row['sequence'], digest(row), row['at'], self.clock(),
                                scrub(row['room'], secrets) if row['room'] is not None else None,
                                scrub(row['version'], secrets), scrub(row['source'], secrets),
                                row['kind'], safe, row['epoch'], row['start'], row['end'], len(safe.encode()) + 400))
                    accepted += 1
                self._loss(db, host['id'], 'upload_older_than_hub_retention', expired)
                receipt = {'ok': True, 'accepted': True, 'host_id': host['id'],
                           'chunk_id': value['chunk_id'], 'stored_records': accepted,
                           'expired_records': expired, 'received_at': self.clock()}
                db.execute('INSERT INTO chunks VALUES(?,?,?,?,?)', (host['id'], value['chunk_id'], request_digest, canonical(receipt).decode(), self.clock()))
                self._prune(db)
                return receipt

    def search(self, *, host_id=None, room=None, version=None, since=None, until=None,
               contains=None, limit=100, cursor=0):
        """Local administrator API only. Never bind this to the public HTTP app."""
        integer(limit, 1, 500); integer(cursor, 0)
        clauses, params = ['sequence>?'], [cursor]
        for column, value in [('host', host_id), ('room', room), ('version', version)]:
            if value is not None:
                if column == 'host': identifier(value)
                else: label(value)
                clauses.append(column + '=?'); params.append(value)
        for comparison, value in [('>=', since), ('<=', until)]:
            if value is not None:
                number(value); clauses.append('at' + comparison + '?'); params.append(value)
        if contains is not None:
            text(contains, 200); clauses.append('instr(text,?)>0'); params.append(contains)
        with self.network.transaction() as auth_db:
            eligible = [r[0] for r in auth_db.execute('SELECT id FROM hosts WHERE revoked=0')]
            if not eligible: return {'ok': True, 'records': [], 'next_cursor': None, 'losses': []}
            clauses.append('host IN (' + ','.join('?' for _ in eligible) + ')'); params.extend(eligible)
            with transaction(self.path) as db:
                self._prune(db)
                rows = db.execute('SELECT * FROM logs WHERE ' + ' AND '.join(clauses) + ' ORDER BY sequence LIMIT ?', params + [limit+1]).fetchall()
                result = [dict(row) for row in rows[:limit]]
                losses = [dict(row) for row in db.execute('SELECT * FROM losses' + (' WHERE host=?' if host_id else ''), (host_id,) if host_id else ()) if row['host'] in eligible]
                return {'ok': True, 'records': result, 'next_cursor': result[-1]['sequence'] if len(rows)>limit else None,
                        'losses': losses, 'retention_seconds': self.retention,
                        'untrusted_log_content': True}

    def support(self, token):
        with self.network.transaction() as auth_db:
            host, _ = self.network._authenticate(auth_db, verifier(token), 'heartbeat')
            with transaction(self.path) as db:
                self._prune(db)
                choice = db.execute('SELECT operational,public_chat FROM consent WHERE host=?', (host['id'],)).fetchone()
                count, size = db.execute('SELECT count(*),COALESCE(sum(bytes),0) FROM logs WHERE host=?', (host['id'],)).fetchone()
                return {'ok': True, 'operational': bool(choice and choice['operational']), 'public_chat': bool(choice and choice['public_chat']),
                        'stored_records': count, 'stored_bytes': size, 'retention_seconds': self.retention,
                        'host_quota_bytes': self.host_quota, 'hub_quota_bytes': self.quota,
                        'losses': [dict(row) for row in db.execute('SELECT reason,count,at FROM losses WHERE host=?', (host['id'],))]}
