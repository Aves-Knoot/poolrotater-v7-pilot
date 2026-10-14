"""Dedicated central pilot SQLite. No provider requests or shared bot schemas."""
from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

from ..layout import checked_root
from .hub_layout import check
from .contracts import (NetworkError, require, identifier, integer, number, text,
                        secret, verifier, identity, enrollment, rotation, heartbeat,
                        canonical, fingerprint, SCOPES)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invitations(
 verifier TEXT PRIMARY KEY, identity TEXT NOT NULL, label TEXT NOT NULL,
 expires REAL NOT NULL, consumed_host TEXT, request_id TEXT, payload_hash TEXT);
CREATE TABLE IF NOT EXISTS hosts(
 id TEXT PRIMARY KEY, operator_id TEXT NOT NULL, account_id INTEGER NOT NULL,
 installation_id TEXT NOT NULL, generation TEXT NOT NULL, label TEXT NOT NULL,
 directory INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
 last_sequence INTEGER NOT NULL DEFAULT -1, last_payload_hash TEXT,
 received REAL, heartbeat TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS enrolled_account ON hosts(account_id) WHERE revoked=0;
CREATE UNIQUE INDEX IF NOT EXISTS enrolled_installation ON hosts(installation_id) WHERE revoked=0;
CREATE TABLE IF NOT EXISTS credentials(
 verifier TEXT PRIMARY KEY,host_id TEXT NOT NULL REFERENCES hosts(id),
 expires REAL NOT NULL,retired INTEGER NOT NULL DEFAULT 0,scopes TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(
 host_id TEXT NOT NULL,request_id TEXT NOT NULL,kind TEXT NOT NULL,
 authorizer TEXT NOT NULL,payload_hash TEXT NOT NULL,response TEXT NOT NULL,
 PRIMARY KEY(host_id,request_id));
CREATE TABLE IF NOT EXISTS audit(
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,kind TEXT NOT NULL,
 host_id TEXT,request_id TEXT);
'''


class Store:
    def __init__(self, root, *, clock=time.time, credential_seconds=30*86400):
        self.root, self.marker = check(root); self.clock = clock
        require(self.root.exists() and not stat.S_IMODE(self.root.stat().st_mode) & 0o077, 'private_hub_root_required')
        number(credential_seconds, 60, 90*86400); self.credential_seconds = credential_seconds
        self.path = self.root/'network.sqlite3'
        if not os.path.lexists(self.path):
            fd = os.open(self.path, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600); os.close(fd)
        self._files()
        with self.transaction() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            base_tables = {'meta','invitations','hosts','credentials','operations','audit'}
            from ..ratings.repository import TABLES, _validate
            require(not tables or tables in (base_tables, base_tables|TABLES), 'foreign_hub_database')
            if tables == base_tables|TABLES: _validate(db)
            for statement in SCHEMA.split(';'):
                if statement.strip(): db.execute(statement)
            db.execute("INSERT OR IGNORE INTO meta VALUES('format','1')")
            require(db.execute("SELECT value FROM meta WHERE key='format'").fetchone()[0] == '1', 'hub_schema_refused')
            db.execute("INSERT OR IGNORE INTO meta VALUES('epoch',?)", (str(uuid.uuid4()),))

    def _files(self):
        for suffix in ('','-wal','-shm','-journal'):
            p=Path(str(self.path)+suffix)
            if not os.path.lexists(p): continue
            s=p.lstat()
            require(stat.S_ISREG(s.st_mode) and s.st_uid == os.geteuid() and s.st_nlink == 1 and not stat.S_IMODE(s.st_mode)&0o077, 'unsafe_hub_database')
            require(s.st_size <= 256*1024**2, 'hub_capacity_required', 503)

    @contextmanager
    def transaction(self):
        self._files(); db=sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON'); db.execute('PRAGMA busy_timeout=5000'); db.execute('PRAGMA max_page_count=65536')
            db.execute('BEGIN IMMEDIATE')
            # Persist the observed clock independently of a rejected request:
            # expiry must not be undone by a later wall-clock rollback.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='meta' AND type='table'").fetchone():
                now=self.clock();number(now)
                previous=db.execute("SELECT value FROM meta WHERE key='clock_high_water'").fetchone()
                require(previous is None or now>=float(previous[0]),'hub_clock_rollback',503)
                db.execute("INSERT OR REPLACE INTO meta VALUES('clock_high_water',?)",(str(now),))
                db.execute('COMMIT');db.execute('BEGIN IMMEDIATE')
            yield db; db.execute('COMMIT')
        except sqlite3.IntegrityError:
            db.rollback(); raise NetworkError('identity_or_credential_conflict',409) from None
        except sqlite3.OperationalError:
            db.rollback(); raise NetworkError('hub_storage_unavailable',503) from None
        except BaseException:
            db.rollback(); raise
        finally: db.close()

    def _audit(self, db, kind, host=None, request=None):
        db.execute('INSERT INTO audit(at,kind,host_id,request_id) VALUES(?,?,?,?)',(self.clock(),kind,host,request))

    def invite(self, binding, *, label, expires_seconds=3600):
        require(type(binding) is dict and set(binding)=={'operator_id','account_id','installation_id','generation','directory'}, 'invalid_invitation_identity')
        identity(binding); text(label,80); number(expires_seconds,60,86400)
        invitation=secret('pri_')
        with self.transaction() as db:
            db.execute('INSERT INTO invitations(verifier,identity,label,expires) VALUES(?,?,?,?)',
                       (verifier(invitation,'pri_'),canonical(binding).decode(),label,self.clock()+expires_seconds))
            self._audit(db,'invitation_issued')
        return {'invitation':invitation,'expires_at':self.clock()+expires_seconds}

    def enroll(self, value):
        enrollment(value); iv=verifier(value['invitation'],'pri_'); cv=verifier(value['credential']); digest=fingerprint(value)
        binding={k:value[k] for k in ('operator_id','account_id','installation_id','generation','directory')}
        with self.transaction() as db:
            invitation=db.execute('SELECT * FROM invitations WHERE verifier=?',(iv,)).fetchone()
            require(invitation is not None,'invitation_refused',401)
            require(json.loads(invitation['identity'])==binding,'invitation_identity_mismatch',403)
            if invitation['consumed_host']:
                require(invitation['request_id']==value['request_id'] and hmac.compare_digest(invitation['payload_hash'],digest),'invitation_used',409)
                host, credential=self._authenticate(db,cv,'heartbeat')
                require(host['id']==invitation['consumed_host'],'invitation_used',409)
                return self._enrollment_result(host,credential,db)
            require(invitation['expires']>self.clock(),'invitation_expired',401)
            host_id=str(uuid.uuid4())
            db.execute('INSERT INTO hosts(id,operator_id,account_id,installation_id,generation,label,directory) VALUES(?,?,?,?,?,?,?)',
                       (host_id,binding['operator_id'],binding['account_id'],binding['installation_id'],binding['generation'],invitation['label'],int(binding['directory'])))
            db.execute('INSERT INTO credentials VALUES(?,?,?,?,?)',(cv,host_id,self.clock()+self.credential_seconds,0,json.dumps(SCOPES)))
            db.execute('UPDATE invitations SET consumed_host=?,request_id=?,payload_hash=? WHERE verifier=?',(host_id,value['request_id'],digest,iv))
            self._audit(db,'enrolled',host_id,value['request_id'])
            host,credential=self._authenticate(db,cv,'heartbeat')
            return self._enrollment_result(host,credential,db)

    def _enrollment_result(self,host,credential,db):
        return {'ok':True,'protocol_version':1,'host_id':host['id'],'generation':host['generation'],
                'credential_expires_at':credential['expires'],'directory':bool(host['directory']),
                'server_epoch':db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]}

    def _authenticate(self,db,token_hash,scope,*,retired=False):
        credential=db.execute('SELECT * FROM credentials WHERE verifier=?',(token_hash,)).fetchone()
        require(credential is not None,'credential_refused',401)
        host=db.execute('SELECT * FROM hosts WHERE id=?',(credential['host_id'],)).fetchone()
        require(host is not None and not host['revoked'],'host_revoked',403)
        require(credential['expires']>self.clock(),'credential_expired',401)
        require(retired or not credential['retired'],'credential_retired',401)
        require(scope in json.loads(credential['scopes']),'scope_refused',403)
        return host,credential

    def rotate(self, token, value):
        rotation(value); old=verifier(token); new=verifier(value['credential']); require(old!=new,'rotation_requires_new_credential')
        digest=fingerprint(value)
        with self.transaction() as db:
            host,credential=self._authenticate(db,old,'rotate',retired=True)
            previous=db.execute('SELECT * FROM operations WHERE host_id=? AND request_id=?',(host['id'],value['request_id'])).fetchone()
            if previous:
                require(previous['kind']=='rotate' and hmac.compare_digest(previous['authorizer'],old) and previous['payload_hash']==digest,'request_id_conflict',409)
                self._authenticate(db,new,'heartbeat'); return json.loads(previous['response'])
            require(not credential['retired'],'credential_retired',401)
            expires=self.clock()+self.credential_seconds
            db.execute('INSERT INTO credentials VALUES(?,?,?,?,?)',(new,host['id'],expires,0,credential['scopes']))
            db.execute('UPDATE credentials SET retired=1 WHERE verifier=?',(old,))
            result={'ok':True,'host_id':host['id'],'credential_expires_at':expires}
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)',(host['id'],value['request_id'],'rotate',old,digest,canonical(result).decode()))
            self._audit(db,'rotated',host['id'],value['request_id']); return result

    def revoke(self, host_id):
        identifier(host_id)
        with self.transaction() as db:
            require(db.execute('SELECT 1 FROM hosts WHERE id=?',(host_id,)).fetchone() is not None,'host_not_found',404)
            db.execute('UPDATE hosts SET revoked=1,heartbeat=NULL WHERE id=?',(host_id,)); self._audit(db,'revoked',host_id)
        return {'ok':True,'host_id':host_id,'revoked':True}

    def leave(self, token):
        hashed=verifier(token)
        with self.transaction() as db:
            credential=db.execute('SELECT * FROM credentials WHERE verifier=?',(hashed,)).fetchone();require(credential is not None,'credential_refused',401)
            host=db.execute('SELECT * FROM hosts WHERE id=?',(credential['host_id'],)).fetchone()
            if not host['revoked']: self._authenticate(db,hashed,'leave')
            db.execute('UPDATE hosts SET revoked=1,heartbeat=NULL WHERE id=?',(host['id'],));self._audit(db,'left',host['id'])
            return {'ok':True,'host_id':host['id'],'revoked':True}

    def heartbeat(self,token,value,*,protocol_version=1):
        from .display import heartbeat_v2
        (heartbeat_v2 if protocol_version == 2 else heartbeat)(value);hashed=verifier(token);digest=fingerprint(value);now=self.clock()
        with self.transaction() as db:
            host,credential=self._authenticate(db,hashed,'heartbeat')
            require(value['installation_id']==host['installation_id'] and value['generation']==host['generation'],'host_identity_mismatch',403)
            require(host['directory'] or not value['rooms'],'directory_not_opted_in',403)
            require(all(r['observed_at']<=now+30 for r in value['rooms']),'future_room_observation')
            sequence=value['sequence']
            require(sequence>=host['last_sequence'],'stale_sequence',409)
            if sequence==host['last_sequence']:
                require(host['last_payload_hash']==digest,'sequence_conflict',409)
                return {'ok':True,'sequence':sequence,'replayed':True,'received_at':host['received']}
            # Receipt quota prevents a host from amplifying status/audit writes.
            require(host['received'] is None or now-host['received']>=5,'heartbeat_rate_limited',429)
            stored = value
            if protocol_version == 2:
                from .display import V1_ROOM_FIELDS
                # Older hub readers enumerate only rooms; never let a rollback
                # accidentally expose v2 additions through the closed v1 route.
                stored = {**value, 'rooms': [{k:r[k] for k in V1_ROOM_FIELDS} for r in value['rooms']],
                          'display_rooms': {r['alias']:r['display'] for r in value['rooms']}}
            db.execute('UPDATE hosts SET last_sequence=?,last_payload_hash=?,received=?,heartbeat=? WHERE id=?',
                       (sequence,digest,now,canonical(stored).decode(),host['id']))
            return {'ok':True,'sequence':sequence,'replayed':False,'received_at':now}

    def directory(self,*,offset=0,limit=50,protocol_version=1):
        integer(offset,0,1000000);integer(limit,1,100);now=self.clock()
        with self.transaction() as db:
            epoch=db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
            rows=db.execute('SELECT * FROM hosts WHERE revoked=0 AND directory=1 AND heartbeat IS NOT NULL AND EXISTS (SELECT 1 FROM credentials WHERE host_id=hosts.id AND retired=0 AND expires>?) ORDER BY id', (now,)).fetchall()
        rooms=[]
        for host in rows:
            payload=json.loads(host['heartbeat'])
            for stored in payload['rooms']:
                from .display import V1_ROOM_FIELDS
                observation = {k: stored[k] for k in V1_ROOM_FIELDS}
                skew = host['received'] - payload['sent_at'] if payload.get('protocol_version') == 2 else None
                correction = skew if skew is not None and abs(skew) <= 30 else None
                observed = observation['observed_at'] + (correction or 0)
                age=max(0,now-min(host['received'],observed))
                if age>=600: continue
                stale=age>=180
                rich = None
                display_value = payload.get('display_rooms',{}).get(stored['alias'])
                if protocol_version == 2 and display_value is not None:
                    import copy
                    rich = copy.deepcopy(display_value)
                    if stale:
                        rich['match'] = None; rich['playing_players'] = None
                    elif rich['match']:
                        rich['match']['clock_skew_seconds'] = correction
                rooms.append({**observation,**({'display':rich} if protocol_version == 2 else {}),'host_id':host['id'],'operator':host['label'],
                              'age_seconds':round(age,2),'freshness':'stale' if stale else 'fresh',
                              'state':'unavailable' if stale else observation['state'],
                              'occupants':None if stale else observation['occupants'],
                              'current_map':None if stale else observation['current_map'],
                              'join_url':None if stale or observation['state']=='unavailable' else 'https://osu.ppy.sh/multiplayer/rooms/'+str(observation['osu_room_id']),
                              'distribution':'self-reported'})
        return {'ok':True,'protocol_version':protocol_version,'server_epoch':epoch,'generated_at':now,'rooms':rooms[offset:offset+limit],
                'next_offset':offset+limit if len(rooms)>offset+limit else None}

    def capabilities(self):
        with self.transaction() as db:
            supported = db.execute("SELECT value FROM meta WHERE key='ratings_format'").fetchone() is not None
        return {'ok':True,'protocol_version':1,'server_time':self.clock(),'maximum_rooms':4,'maximum_body_bytes':65536,
                'heartbeat_seconds':60,'stale_seconds':180,'hide_seconds':600,'scopes':list(SCOPES),'ratings_supported':supported,
                'rating_batch_limits':{'events':500,'bytes':512*1024} if supported else None}

    def status(self, token):
        hashed=verifier(token)
        with self.transaction() as db:
            host,credential=self._authenticate(db,hashed,'heartbeat')
            result=self._enrollment_result(host,credential,db)
            return {**result,'last_sequence':host['last_sequence'],'last_receipt_at':host['received'],
                    'scopes':json.loads(credential['scopes'])}

    def backup(self, destination):
        destination=Path(destination).absolute()
        require(destination.parent==self.root and not os.path.lexists(destination),'fresh_hub_backup_required')
        fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
        self._files()
        try:
            with sqlite3.connect(self.path,timeout=5) as source,sqlite3.connect(destination) as target:
                source.backup(target)
                require(target.execute('PRAGMA quick_check').fetchone()[0]=='ok','backup_verification_failed')
            with destination.open('rb') as stream:os.fsync(stream.fileno())
        except BaseException:
            destination.unlink(missing_ok=True);raise
        return {'ok':True,'backup_created':True,'contains_private_identity_data':True}

    def restore_authority_reset(self):
        # Required after an administrator restores a snapshot into a fresh hub.
        # Old credentials/invites cannot silently resurrect deleted authority.
        epoch=str(uuid.uuid4())
        with self.transaction() as db:
            db.execute('UPDATE hosts SET revoked=1,heartbeat=NULL')
            db.execute('UPDATE credentials SET retired=1')
            db.execute('UPDATE invitations SET expires=0')
            db.execute("UPDATE meta SET value=? WHERE key='epoch'",(epoch,))
            self._audit(db,'restored_authority_reset')
        return {'ok':True,'server_epoch':epoch,'all_hosts_revoked':True,'reenrollment_required':True}
