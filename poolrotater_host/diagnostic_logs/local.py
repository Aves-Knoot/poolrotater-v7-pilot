"""Durable local diagnostic queue, explicit allowlist, and bounded file tailer.

Only sanitized records reach SQLite. Collection is outside the gameplay thread;
the installation's network agent owns scheduling and retry backoff.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from ..network.contracts import canonical, identifier, require
from .common import (COMPRESSED_LIMIT, RAW_LIMIT, RECORD_LIMIT, VERSION, digest,
                     label, pack, private_database, timestamp, transaction)
from .redact import scrub

SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records(sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,
 at REAL NOT NULL,body TEXT NOT NULL,bytes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cursors(source TEXT PRIMARY KEY,path TEXT NOT NULL,device INTEGER NOT NULL,
 inode INTEGER NOT NULL,epoch TEXT NOT NULL,offset INTEGER NOT NULL,anchor TEXT NOT NULL,
 discarding INTEGER NOT NULL DEFAULT 0,pem INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS batch(id TEXT PRIMARY KEY,envelope TEXT NOT NULL,last_sequence INTEGER NOT NULL);
'''
_ROOM = re.compile(r'\[(room[0-9]+)\]')
_CHAT = re.compile(r'(?i)(?:\bcommand from\b|public command suppressed|AI request queued|\b(?:chat|message|say)\s+(?:from|to)\b|\b(?:received|sending|sent)\s+(?:chat|message)\b|^\s*<[^>]+>)')


@dataclass(frozen=True)
class Source:
    name: str
    path: object
    room: str = None
    version: str = 'unknown'
    private: bool = False
    public_rooms: tuple = ()
    start_at_end: bool = True
    structured: bool = False


class LocalLogs:
    def __init__(self, root, *, installation_id, generation, secret_provider=lambda: (),
                 sources=(), clock=time.time, quota_bytes=32*1024**2,
                 retention_seconds=7*86400):
        identifier(installation_id); identifier(generation)
        require(type(quota_bytes) is int and 65536 <= quota_bytes <= 128*1024**2, 'invalid_log_quota')
        require(type(retention_seconds) is int and 60 <= retention_seconds <= 30*86400, 'invalid_log_retention')
        self.path = private_database(root, 'diagnostic-logs.sqlite3')
        self.installation_id, self.generation = installation_id, generation
        self.secret_provider, self.clock = secret_provider, clock
        self.quota, self.retention = quota_bytes, retention_seconds
        self.sources = {}
        for source in sources:
            self._validate_source(source)
            require(source.name not in self.sources, 'duplicate_log_source')
            self.sources[source.name] = source
        require(len(self.sources) <= 32, 'too_many_log_sources')
        with transaction(self.path) as db:
            for statement in SCHEMA.split(';'):
                if statement.strip(): db.execute(statement)
            binding = canonical([installation_id, generation]).decode()
            previous = db.execute("SELECT value FROM settings WHERE key='binding'").fetchone()
            require(previous is None or previous[0] == binding, 'log_identity_mismatch')
            db.execute("INSERT OR IGNORE INTO settings VALUES('binding',?)", (binding,))
            for key, value in [('operational', 'false'), ('public_chat', 'false'), ('filtered', '0'), ('dropped', '0'), ('capture_since', '0'), ('chat_since', '0')]:
                db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (key, value))

    @staticmethod
    def _validate_source(source):
        require(isinstance(source, Source), 'invalid_log_source')
        label(source.name); label(source.room, nullable=True)
        require(type(source.version) is str and VERSION.fullmatch(source.version), 'invalid_log_version')
        require(type(source.private) is bool and type(source.start_at_end) is bool and type(source.structured) is bool and type(source.public_rooms) is tuple, 'invalid_log_source')
        for alias in source.public_rooms: label(alias)
        require(Path(source.path).is_absolute(), 'absolute_log_source_required')

    def _settings(self, db):
        return {row['key']: json.loads(row['value']) for row in db.execute("SELECT * FROM settings WHERE key!='binding'")}

    def configure(self, *, operational, public_chat=False):
        require(type(operational) is bool and type(public_chat) is bool and (operational or not public_chat), 'invalid_log_consent')
        with transaction(self.path) as db:
            previous = self._settings(db)
            for key, value in [('operational', operational), ('public_chat', public_chat)]:
                db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(value), key))
            if not operational or (previous['public_chat'] and not public_chat):
                # Withdrawn content cannot remain in an in-flight envelope.
                db.execute('DELETE FROM batch')
                if not operational: db.execute('DELETE FROM records')
                else:
                    for row in db.execute('SELECT sequence,body FROM records').fetchall():
                        if json.loads(row['body'])['kind'] in ('chat', 'command'):
                            db.execute('DELETE FROM records WHERE sequence=?', (row['sequence'],))
            if previous['operational'] != operational:
                # A future opt-in must not read the disabled interval.
                db.execute('DELETE FROM cursors')
                if operational:
                    db.execute("UPDATE settings SET value=? WHERE key='capture_since'", (json.dumps(self.clock()),))
            if public_chat and not previous['public_chat']:
                db.execute("UPDATE settings SET value=? WHERE key='chat_since'", (json.dumps(self.clock()),))
        return self.support()

    def _increment(self, db, key, count=1):
        db.execute('UPDATE settings SET value=CAST(CAST(value AS INTEGER)+? AS TEXT) WHERE key=?', (count, key))

    def _insert(self, db, text, *, at, source, room, version, kind, epoch, start, end):
        row = {'id': str(uuid.uuid4()), 'sequence': 0, 'at': at, 'source': source,
               'room': room, 'version': version, 'kind': kind, 'text': text,
               'epoch': epoch, 'start': start, 'end': end}
        cursor = db.execute('INSERT INTO records(id,at,body,bytes) VALUES(?,?,?,?)', (row['id'], at, '', len(text.encode()) + 400))
        row['sequence'] = cursor.lastrowid
        db.execute('UPDATE records SET body=? WHERE sequence=?', (canonical(row).decode(), cursor.lastrowid))

    def _gap(self, db, reason, count, *, source='collector', room=None, epoch=None, start=0, end=0):
        self._insert(db, canonical({'reason': reason, 'records_or_bytes': count}).decode(), at=self.clock(),
                     source=source, room=room, version='collector', kind='gap',
                     epoch=epoch or str(uuid.uuid4()), start=start, end=end)

    def _prune(self, db):
        expired = db.execute('SELECT sequence FROM records WHERE at<?', (self.clock() - self.retention,)).fetchall()
        dropped = len(expired)
        for row in expired: db.execute('DELETE FROM records WHERE sequence=?', (row[0],))
        used = db.execute('SELECT COALESCE(sum(bytes),0) FROM records').fetchone()[0]
        quota_dropped = 0
        # Preserve enough room for the explicit loss record itself.
        if used > self.quota - 2048:
            for row in db.execute('SELECT sequence,bytes FROM records ORDER BY sequence'):
                if used <= self.quota - 2048: break
                db.execute('DELETE FROM records WHERE sequence=?', (row['sequence'],))
                used -= row['bytes']; quota_dropped += 1
        if dropped or quota_dropped:
            db.execute('DELETE FROM batch')
            self._increment(db, 'dropped', dropped + quota_dropped)
            if dropped: self._gap(db, 'local_retention_expired', dropped)
            if quota_dropped: self._gap(db, 'local_quota_evicted_oldest', quota_dropped)

    def append(self, text, *, room=None, version='unknown', kind='operational',
               private=False, at=None, source='runtime', epoch=None, start=0, end=0):
        label(source); label(room, nullable=True)
        require(kind in ('operational', 'chat', 'command'), 'invalid_log_kind')
        require(type(version) is str and VERSION.fullmatch(version), 'invalid_log_version')
        timestamp(self.clock() if at is None else at)
        require(type(private) is bool and type(text) is str, 'invalid_log_record')
        require(len(text.encode()) <= RAW_LIMIT, 'log_record_too_large', 413)
        epoch = epoch or str(uuid.uuid4()); identifier(epoch)
        # Secret loading happens before the transaction; failures never persist
        # the original message and do not claim successful collection.
        safe = scrub(text, self.secret_provider())
        with transaction(self.path) as db:
            settings = self._settings(db)
            if not settings['operational']: return False
            observed = self.clock() if at is None else at
            if observed < settings['capture_since'] or private or (kind in ('chat', 'command') and (not settings['public_chat'] or room is None or observed < settings['chat_since'])):
                self._increment(db, 'filtered'); return False
            for index in range(0, max(1, len(safe)), 4096):
                self._insert(db, safe[index:index+4096], at=self.clock() if at is None else at,
                             source=source, room=room, version=version, kind=kind,
                             epoch=epoch, start=start, end=end)
            self._prune(db)
        return True

    def _read_file(self, path):
        path = Path(path)
        # A replaced directory component must not redirect an allowlisted path.
        for parent in (path, *path.parents):
            require(not parent.is_symlink(), 'unsafe_log_source')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
            os.close(fd); require(False, 'unsafe_log_source')
        return fd, st

    def collect(self, source):
        self._validate_source(source)
        require(self.sources.get(source.name) == source, 'log_source_not_allowlisted')
        with transaction(self.path) as db:
            if not self._settings(db)['operational']: return {'ok': True, 'read': 0}
        try: fd, st = self._read_file(source.path)
        except FileNotFoundError: return {'ok': True, 'read': 0, 'source_absent': True}
        try:
            with transaction(self.path) as db:
                cursor = db.execute('SELECT * FROM cursors WHERE source=?', (source.name,)).fetchone()
                epoch, offset, discarding, pem = str(uuid.uuid4()), 0, False, False
                if cursor:
                    require(cursor['path'] == str(Path(source.path)), 'log_source_binding_changed')
                    epoch, offset, discarding, pem = cursor['epoch'], cursor['offset'], bool(cursor['discarding']), bool(cursor['pem'])
                    if (cursor['device'], cursor['inode']) != (st.st_dev, st.st_ino):
                        self._gap(db, 'source_rotated_unread_tail_unknown', 0, source=source.name, room=source.room)
                        epoch, offset, discarding, pem = str(uuid.uuid4()), 0, False, False
                    elif st.st_size < offset:
                        self._gap(db, 'source_truncated', offset - st.st_size, source=source.name, room=source.room, epoch=epoch)
                        epoch, offset, discarding, pem = str(uuid.uuid4()), 0, False, False
                    elif offset:
                        anchor = os.pread(fd, min(offset, 256), max(0, offset - 256))
                        import hashlib
                        if hashlib.sha256(anchor).hexdigest() != cursor['anchor']:
                            self._gap(db, 'source_rewritten', offset, source=source.name, room=source.room, epoch=epoch)
                            epoch, offset, discarding, pem = str(uuid.uuid4()), 0, False, False
                elif source.start_at_end:
                    offset = st.st_size
                    if offset: self._gap(db, 'collection_begins_at_opt_in_tail', offset, source=source.name, room=source.room)
                raw = os.pread(fd, RAW_LIMIT, offset)
                consumed = raw.rfind(b'\n') + 1
                discarded_prefix = 0
                if discarding and consumed:
                    prefix = raw.find(b'\n') + 1
                    self._gap(db, 'oversized_line_continuation_omitted', prefix, source=source.name, room=source.room, epoch=epoch, start=offset, end=offset+prefix)
                    discarded_prefix = prefix
                    discarding = False
                if not consumed and len(raw) == RAW_LIMIT:
                    # A malicious unbounded line cannot fill memory or leak a
                    # credential straddling independent redaction chunks.
                    self._gap(db, 'oversized_unterminated_line', len(raw), source=source.name, room=source.room, epoch=epoch, start=offset, end=offset+len(raw))
                    consumed = len(raw); raw = b''; discarding = True
                data = raw[discarded_prefix:consumed].decode('utf-8', errors='replace')
                settings = self._settings(db)
                secrets = tuple(self.secret_provider())
                if source.private:
                    if data: self._increment(db, 'filtered', len(data.splitlines()))
                    data = ''
                if pem and not source.structured:
                    marker = re.search(r'-----END [^-\r\n]*PRIVATE KEY-----', data)
                    data = data[marker.end():] if marker else ''
                    pem = not bool(marker)
                if not source.structured:
                    begin = data.rfind('-----BEGIN ')
                    if begin >= 0 and 'PRIVATE KEY-----' in data[begin:]:
                        pem = not bool(re.search(r'-----END [^-\r\n]*PRIVATE KEY-----', data[begin:]))
                safe = data if source.structured else scrub(data, secrets)
                # Redact the entire read before splitting to cover PEM blocks.
                for line in safe.splitlines(keepends=True):
                    room = source.room
                    at, version, explicit_kind = self.clock(), source.version, None
                    if source.structured:
                        try:
                            from ..network.contracts import decode
                            entry = decode(line.encode(), maximum=RAW_LIMIT)
                            require(type(entry) is dict and set(entry) == {'at', 'room', 'version', 'kind', 'private', 'text'}, 'invalid_structured_log')
                            label(entry['room'], nullable=True); timestamp(entry['at'])
                            require(type(entry['private']) is bool and entry['kind'] in ('operational', 'chat', 'command') and type(entry['text']) is str and type(entry['version']) is str and VERSION.fullmatch(entry['version']), 'invalid_structured_log')
                            if entry['private']:
                                self._increment(db, 'filtered'); continue
                            room, at, version, explicit_kind = entry['room'], entry['at'], entry['version'], entry['kind']
                            if at < settings['capture_since'] or (explicit_kind in ('chat', 'command') and at < settings['chat_since']):
                                self._increment(db, 'filtered'); continue
                            line = scrub(entry['text'], secrets)
                        except Exception:
                            self._gap(db, 'invalid_structured_log_omitted', 1, source=source.name, room=source.room, epoch=epoch, start=offset, end=offset+consumed)
                            continue
                    tagged = _ROOM.search(line)
                    if tagged and not source.structured:
                        room = tagged[1]
                        if room not in source.public_rooms and room != source.room:
                            self._increment(db, 'filtered'); continue
                    chat = explicit_kind in ('chat', 'command') or bool(_CHAT.search(line))
                    if chat and (not settings['public_chat'] or room is None):
                        self._increment(db, 'filtered'); continue
                    kind = explicit_kind or ('command' if 'command from' in line.lower() else 'chat' if chat else 'operational')
                    for index in range(0, len(line), 4096):
                        self._insert(db, line[index:index+4096], at=at, source=source.name,
                                     room=room, version=version, kind=kind, epoch=epoch,
                                     start=offset, end=offset+consumed)
                offset += consumed
                import hashlib
                anchor = hashlib.sha256(os.pread(fd, min(offset, 256), max(0, offset-256))).hexdigest()
                db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?,?,?,?,?,?,?,?)',
                           (source.name, str(Path(source.path)), st.st_dev, st.st_ino, epoch, offset, anchor, int(discarding), int(pem)))
                self._prune(db)
            return {'ok': True, 'read': consumed}
        finally: os.close(fd)

    def batch(self):
        with transaction(self.path) as db:
            if not self._settings(db)['operational']: return None
            self._prune(db)
            previous = db.execute('SELECT envelope FROM batch').fetchone()
            if previous: return json.loads(previous[0])
            rows = db.execute('SELECT body FROM records ORDER BY sequence LIMIT ?', (RECORD_LIMIT,)).fetchall()
            selected, payload = [], None
            for row in rows:
                candidate = selected + [json.loads(row[0])]
                try: encoded = pack(candidate)
                except Exception as error:
                    from ..network.contracts import NetworkError
                    if isinstance(error, NetworkError) and error.code == 'log_batch_too_large': break
                    raise
                selected, payload = candidate, encoded
            if not selected: return None
            envelope = {'format': 1, 'installation_id': self.installation_id,
                        'generation': self.generation, 'chunk_id': digest(selected), 'payload': payload}
            db.execute('INSERT INTO batch VALUES(?,?,?)', (envelope['chunk_id'], canonical(envelope).decode(), selected[-1]['sequence']))
            return envelope

    def acknowledge(self, receipt):
        require(type(receipt) is dict and receipt.get('ok') is True and receipt.get('accepted') is True, 'invalid_log_receipt')
        with transaction(self.path) as db:
            row = db.execute('SELECT * FROM batch WHERE id=?', (receipt.get('chunk_id'),)).fetchone()
            require(row is not None, 'unknown_log_receipt')
            db.execute('DELETE FROM records WHERE sequence<=?', (row['last_sequence'],))
            db.execute('DELETE FROM batch WHERE id=?', (row['id'],))
        return self.support()

    def support(self):
        with transaction(self.path) as db:
            settings = self._settings(db)
            count, size = db.execute('SELECT count(*),COALESCE(sum(bytes),0) FROM records').fetchone()
            return {'ok': True, 'operational': settings['operational'], 'public_chat': settings['public_chat'],
                    'pending_records': count, 'pending_bytes': size, 'quota_bytes': self.quota,
                    'retention_seconds': self.retention, 'dropped_records': settings['dropped'],
                    'capture_since': settings['capture_since'], 'chat_since': settings['chat_since'],
                    'privacy_filtered_records': settings['filtered'], 'sources': sorted(self.sources),
                    'pending_chunk': bool(db.execute('SELECT 1 FROM batch').fetchone())}
