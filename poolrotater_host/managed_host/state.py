"""Private crash-durable room intent, capacity reservations and admin receipts.

No provider calls, service control, shell, credential reads or network occur here.
The controller supplies verified room observations and applies typed operations.
"""
from contextlib import contextmanager
import copy
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from ..diagnostic_logs.common import private_database, transaction
from ..network.contracts import identifier, integer, require, canonical
from .contracts import alias as check_alias, request, digest, TERMINAL, ERRORS
from .settings import validate, public_settings, merge

SCHEMA='''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rooms(alias TEXT PRIMARY KEY,desired TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0,applied_revision INTEGER NOT NULL DEFAULT 0,
 config TEXT NOT NULL,effective TEXT NOT NULL,phase TEXT NOT NULL DEFAULT 'unobserved',
 room_id INTEGER,room_generation TEXT,pending TEXT,failure TEXT);
CREATE TABLE IF NOT EXISTS reservations(alias TEXT PRIMARY KEY,generation TEXT NOT NULL,
 room_id INTEGER,phase TEXT NOT NULL,operation TEXT,at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS reservation_room ON reservations(room_id) WHERE room_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,digest TEXT NOT NULL,payload TEXT NOT NULL,
 alias TEXT NOT NULL,phase TEXT NOT NULL,error TEXT,result TEXT,created REAL NOT NULL,
 changed REAL NOT NULL,desired_before TEXT,revision_after INTEGER,previous_config TEXT);
CREATE TABLE IF NOT EXISTS audit(sequence INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,
 operation TEXT,alias TEXT,actor TEXT NOT NULL,action TEXT NOT NULL,phase TEXT NOT NULL,
 revision INTEGER,config_digest TEXT);
CREATE TABLE IF NOT EXISTS delivered(id TEXT PRIMARY KEY,digest TEXT NOT NULL,refusal TEXT,acknowledged TEXT);
CREATE TABLE IF NOT EXISTS local_actions(id TEXT PRIMARY KEY,pin TEXT NOT NULL);
'''

class State:
    def __init__(self,root,*,host_id,installation_id,generation,clock=time.time):
        for v in (host_id,installation_id,generation):identifier(v)
        self.root=Path(root);self.clock=clock
        self.binding={'host_id':host_id,'installation_id':installation_id,'generation':generation}
        self.path=private_database(self.root/'state','managed-host.sqlite3')
        with transaction(self.path) as db:
            for sql in SCHEMA.split(';'):
                if sql.strip():db.execute(sql)
            columns={r[1] for r in db.execute('PRAGMA table_info(operations)')}
            if 'previous_config' not in columns:db.execute('ALTER TABLE operations ADD COLUMN previous_config TEXT')
            old=db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
            if old:require(json.loads(old[0])==self.binding,'host_identity_mismatch',409)
            else:
                self._meta(db,'binding',self.binding);self._meta(db,'max_concurrent_rooms',4)
                self._meta(db,'provider_room_limit',4);self._meta(db,'external_rooms',0)
                self._meta(db,'policy_version',0)
    @staticmethod
    def _meta(db,key,value):db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,canonical(value).decode()))
    @staticmethod
    def _get(db,key):return json.loads(db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0])
    def _intent_changed(self,db):
        old=db.execute("SELECT value FROM meta WHERE key='intent_sequence'").fetchone()
        self._meta(db,'intent_sequence',json.loads(old[0])+1 if old else 1)
    def intent_sequence(self):
        """Explicit decisions only, never progress/diagnostic observations."""
        with transaction(self.path) as db:
            row=db.execute("SELECT value FROM meta WHERE key='intent_sequence'").fetchone()
            return json.loads(row[0]) if row else 0
    def _audit(self,db,operation,alias,actor,action,phase,revision=None,settings=None):
        db.execute('INSERT INTO audit(at,operation,alias,actor,action,phase,revision,config_digest) VALUES(?,?,?,?,?,?,?,?)',
            (self.clock(),operation,alias,actor,action,phase,revision,digest(settings) if settings is not None else None))
    def _room(self,db,alias):
        row=db.execute('SELECT * FROM rooms WHERE alias=?',(alias,)).fetchone()
        require(row is not None,'room_not_found',404)
        return row
    def refresh_binding(self):
        with transaction(self.path) as db:
            value=self._get(db,'binding')
            require(all(value[k]==self.binding[k] for k in ('installation_id','generation')),'host_identity_mismatch',409)
            self.binding=value
    def rebind(self,host_id):
        """Local enrollment transition; preserve intent, ceilings and reservations.

        Caller must verify the installed network credential. An old in-flight
        provider mutation stays uncertain, never migrates as executable work.
        Old hub commands/receipts cannot be delivered to the new authority.
        """
        identifier(host_id)
        with transaction(self.path) as db:
            old=self._get(db,'binding')
            require(old==self.binding,'host_identity_mismatch',409)
            if host_id==old['host_id']:return
            for op in db.execute("SELECT * FROM operations WHERE phase NOT IN ('applied','refused','uncertain','cancelled','expired')").fetchall():
                if op['phase']=='applying':self._finish(db,op,'uncertain','managed_application_requires_review',None)
                else:
                    self._restore(db,op)
                    self._finish(db,op,'cancelled','host_identity_mismatch',None)
            db.execute('DELETE FROM delivered')
            db.execute("DELETE FROM meta WHERE key IN ('exchange_pending','exchange_sequence')")
            self.binding={**old,'host_id':host_id};self._meta(db,'binding',self.binding)
            self._meta(db,'policy_version',0)
            self._meta(db,'hub_authority',None)
            self._audit(db,None,None,'local-enrollment','rebind','applied')
    def accepted_authority(self,server_epoch):
        identifier(server_epoch);self.refresh_binding()
        with transaction(self.path) as db:
            self._meta(db,'hub_authority',{'host_id':self.binding['host_id'],'server_epoch':server_epoch})
    def sharing_ready(self,credential):
        if credential is None:return False
        self.refresh_binding()
        with transaction(self.path) as db:
            row=db.execute("SELECT value FROM meta WHERE key='hub_authority'").fetchone()
            return bool(row and json.loads(row[0])=={k:credential[k] for k in ('host_id','server_epoch')}
                        and self._get(db,'policy_version')==1 and credential['host_id']==self.binding['host_id'])
    def receive(self,value):
        """Persist even a valid but stale command's refusal, preventing poison retries."""
        self.refresh_binding();request(value)
        require(all(value[k]==v for k,v in self.binding.items()),'host_identity_mismatch',403)
        pin=digest(value)
        with transaction(self.path) as db:
            old=db.execute('SELECT * FROM delivered WHERE id=?',(value['id'],)).fetchone()
            if old:require(old['digest']==pin,'managed_operation_conflict',409)
            else:
                # An operation admitted locally is not a hub delivery, even
                # when the hub later supplies the same UUID and payload.
                require(db.execute('SELECT 1 FROM operations WHERE id=?',(value['id'],)).fetchone() is None
                        and db.execute('SELECT 1 FROM local_actions WHERE id=?',(value['id'],)).fetchone() is None,
                        'managed_operation_conflict',409)
                db.execute('INSERT INTO delivered(id,digest) VALUES(?,?)',(value['id'],pin))
            if old and old['refusal']:return json.loads(old['refusal'])
        from ..network.contracts import NetworkError
        try:return self._enqueue(value,from_hub=True)
        except (NetworkError,ValueError,TypeError) as exc:
            code=exc.code if isinstance(exc,NetworkError) and exc.code in ERRORS else 'invalid_settings'
            receipt={'id':value['id'],'alias':value['alias'],'phase':'expired' if code=='operation_expired' else 'refused',
                     'failure_code':code,'result':None}
            with transaction(self.path) as db:
                # Another receiver may have admitted the exact command. Never
                # replace its authoritative progress with a synthetic refusal.
                op=db.execute('SELECT * FROM operations WHERE id=?',(value['id'],)).fetchone()
                if op:return self._receipt(op)
                db.execute('UPDATE delivered SET refusal=? WHERE id=?',(canonical(receipt).decode(),value['id']))
            return receipt
    def exchange(self,*,controller_phase='unavailable'):
        """Write the exact outgoing body before dispatch; only one agent holds its lease."""
        # Snapshot has its own short transaction. Never hold a database lock
        # across a transport call or across another SQLite connection.
        caps=self.snapshot(controller_phase=controller_phase)
        with transaction(self.path) as db:
            raw=db.execute("SELECT value FROM meta WHERE key='exchange_pending'").fetchone()
            if raw:return json.loads(raw[0])
            receipts=[]
            for row in db.execute('SELECT d.id,d.refusal,d.acknowledged,o.phase,o.error,o.result,o.alias '
                    'FROM delivered d LEFT JOIN operations o ON o.id=d.id ORDER BY d.rowid'):
                receipt=json.loads(row['refusal']) if row['refusal'] else self._receipt(row) if row['phase'] is not None else None
                if receipt is not None and row['acknowledged']!=digest(receipt):
                    receipts.append(receipt)
                    if len(receipts)==32:break
            previous=db.execute("SELECT value FROM meta WHERE key='exchange_sequence'").fetchone()
            sequence=json.loads(previous[0])+1 if previous else 1
            value={'protocol_version':1,**{k:self.binding[k] for k in ('installation_id','generation')},
                   'sequence':sequence,'sent_at':self.clock(),'capabilities':caps,'receipts':receipts}
            self._meta(db,'exchange_sequence',sequence);self._meta(db,'exchange_pending',value)
            return value
    def acknowledge_exchange(self,value):
        with transaction(self.path) as db:
            raw=db.execute("SELECT value FROM meta WHERE key='exchange_pending'").fetchone()
            require(raw is not None and digest(json.loads(raw[0]))==digest(value),'managed_exchange_changed',409)
            for receipt in value['receipts']:
                db.execute('UPDATE delivered SET acknowledged=? WHERE id=?',(digest(receipt),receipt['id']))
            db.execute("DELETE FROM meta WHERE key='exchange_pending'")
    def supersede_expired_exchange(self,*,maximum_age=240):
        """A higher sequence supersedes only observation, never provider execution."""
        with transaction(self.path) as db:
            raw=db.execute("SELECT value FROM meta WHERE key='exchange_pending'").fetchone()
            if raw and self.clock()-json.loads(raw[0])['sent_at']>maximum_age:
                db.execute("DELETE FROM meta WHERE key='exchange_pending'")
    def seed(self,rooms):
        """Adopt existing configuration, without asserting provider ownership."""
        require(type(rooms) is list and 1<=len(rooms)<=4,'invalid_managed_rooms')
        seen=set()
        with transaction(self.path) as db:
            for row in rooms:
                a=check_alias(row['alias']);require(a not in seen,'invalid_managed_rooms');seen.add(a)
                settings=validate(row['settings'],preset=row['settings'].get('preset'))
                if db.execute('SELECT 1 FROM rooms WHERE alias=?',(a,)).fetchone():continue
                desired=row.get('desired','running');require(desired in ('running','stopped'),'invalid_managed_desired')
                raw=canonical(settings).decode()
                db.execute('INSERT INTO rooms(alias,desired,config,effective) VALUES(?,?,?,?)',(a,desired,raw,raw))
                self._audit(db,None,a,'local-controller','adopt','unobserved',0,settings)
    def seed_dormant(self,rooms):
        """Atomically append witnessed stopped slots, preserving all old intent."""
        require(type(rooms) is list and 1<=len(rooms)<=4,'invalid_managed_rooms')
        seen=set()
        with transaction(self.path) as db:
            for row in rooms:
                a=check_alias(row['alias']);require(a not in seen,'invalid_managed_rooms');seen.add(a)
                settings=validate(row['settings'],preset=row['settings'].get('preset'))
                require(settings.get('visibility','public')=='public' and 'password' not in settings,'invalid_managed_rooms')
                raw=canonical(settings).decode();old=db.execute('SELECT * FROM rooms WHERE alias=?',(a,)).fetchone()
                require(not db.execute('SELECT 1 FROM reservations WHERE alias=?',(a,)).fetchone(),'managed_room_topology_requires_review')
                if old is not None:
                    require(old['revision']==0 and old['applied_revision']==0 and old['desired']=='stopped'
                            and old['config']==raw and old['effective']==raw and old['phase'] in ('unobserved','waiting_boundary')
                            and all(old[k] is None for k in ('room_id','room_generation','failure')),
                            'managed_room_topology_requires_review')
                    if old['pending'] is not None:
                        op=db.execute('SELECT * FROM operations WHERE id=?',(old['pending'],)).fetchone()
                        require(op is not None and op['alias']==a and op['revision_after'] is None
                                and op['phase'] in ('received','validated','waiting_boundary'),
                                'managed_room_topology_requires_review')
                    continue
                require(db.execute('SELECT count(*) FROM rooms').fetchone()[0]<4,'invalid_managed_rooms')
                db.execute('INSERT INTO rooms(alias,desired,config,effective) VALUES(?,?,?,?)',(a,'stopped',raw,raw))
                self._audit(db,None,a,'local-upgrade','append-stopped-slot','unobserved',0,settings)
    def policy_applied(self,version):
        integer(version,1,1)
        with transaction(self.path) as db:self._meta(db,'policy_version',version)
    def limits(self):
        with transaction(self.path) as db:return self._limits(db)
    def _limits(self,db):
        maximum=self._get(db,'max_concurrent_rooms');provider=self._get(db,'provider_room_limit')
        external=self._get(db,'external_rooms');local=db.execute('SELECT count(*) FROM reservations').fetchone()[0]
        return {'max_concurrent_rooms':maximum,'provider_room_limit':provider,'effective_limit':min(maximum,max(0,provider-external)),
                'capacity_used':local,'external_rooms':external,'over_capacity_draining':local>min(maximum,max(0,provider-external))}
    def set_limit(self,maximum,*,actor='local-operator'):
        """Local operator entry only. No wire action permits changing this field."""
        integer(maximum,0,1024)
        with transaction(self.path) as db:
            if self._get(db,'max_concurrent_rooms')!=maximum:self._intent_changed(db)
            self._meta(db,'max_concurrent_rooms',maximum)
            self._audit(db,None,None,actor,'set-host-ceiling','applied')
            return self._limits(db)
    def operator_stop(self,*,operation_id=None,actor='local-operator'):
        """Persist a global stop before touching systemd; never clear ownership.

        A later explicit local start may resume exactly these running intents.
        A remote edit/open/close invalidates that resume through its revision.
        Automatic starts never consume this record.
        """
        with transaction(self.path) as db:
            if operation_id is not None:
                identifier(operation_id);pin=digest({'action':'stop-all','actor':actor})
                require(db.execute('SELECT 1 FROM operations WHERE id=?',(operation_id,)).fetchone() is None,'managed_operation_conflict',409)
                previous=db.execute('SELECT pin FROM local_actions WHERE id=?',(operation_id,)).fetchone()
                if previous:
                    require(previous['pin']==pin,'managed_operation_conflict',409)
                    return False
                db.execute('INSERT INTO local_actions VALUES(?,?)',(operation_id,pin))
            saved=db.execute("SELECT value FROM meta WHERE key='operator_resume_intent'").fetchone()
            resume=json.loads(saved[0]) if saved else {}
            affected=set()
            for op in db.execute("SELECT * FROM operations WHERE phase NOT IN ('applied','refused','uncertain','cancelled','expired')").fetchall():
                affected.add(op['alias'])
                if op['phase']=='applying':
                    self._finish(db,op,'uncertain','managed_application_requires_review',None)
                else:
                    self._restore(db,op)
                    self._finish(db,op,'cancelled','operation_cancelled',None)
            for room in db.execute('SELECT * FROM rooms ORDER BY alias').fetchall():
                alias=room['alias']
                if alias in resume and (room['desired']!='stopped' or room['revision']!=resume[alias]):
                    resume.pop(alias)
                if room['desired']=='running' or alias in affected:
                    revision=room['revision']+1
                    if room['desired']=='running':resume[alias]=revision
                    else:resume.pop(alias,None)
                    db.execute("UPDATE rooms SET desired='stopped',revision=? WHERE alias=?",(revision,alias))
                    self._audit(db,operation_id,alias,actor,'stop-intent','applied',revision)
            self._meta(db,'operator_resume_intent',resume)
            if affected or resume:self._intent_changed(db)
            return True
    def local_lifecycle(self,alias,action,*,operation_id,actor):
        """Local exact retries reuse the original revision/expiry/binding.

        Local operations retain their journal and audit receipts, but must not
        enter the hub delivery outbox: the hub never issued these commands.
        """
        self.refresh_binding()
        check_alias(alias);identifier(operation_id)
        require(action in ('open','close','restart'),'invalid_managed_action')
        with transaction(self.path) as db:
            # A rebind may race the refresh above. Refuse the old snapshot rather
            # than returning a retained receipt from the previous enrollment.
            require(self._get(db,'binding')==self.binding,'host_identity_mismatch',409)
            require(db.execute('SELECT 1 FROM local_actions WHERE id=?',(operation_id,)).fetchone() is None,'managed_operation_conflict',409)
            require(db.execute('SELECT 1 FROM delivered WHERE id=?',(operation_id,)).fetchone() is None,'managed_operation_conflict',409)
            old=db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone()
            if old:
                value=json.loads(old['payload'])
                require(all(value[k]==v for k,v in {'alias':alias,'action':action,'actor':actor}.items()),'managed_operation_conflict',409)
                require(all(value.get(k)==v for k,v in self.binding.items()),'managed_operation_conflict',409)
                return self._receipt(old)
        row=self.room(alias)
        value={'protocol_version':1,'policy_version':1,'id':operation_id,**self.binding,
            'alias':alias,'actor':actor,'role':'FLEET_ADMIN','action':action,'timing':'when_empty','patch':{},
            'expected_revision':row['revision'],'expected_room_generation':row['room_generation'],
            'expires_at':self.clock()+600,'cancel_operation_id':None}
        return self.enqueue(value)
    def operator_start(self):
        """Explicit local action only; never reopen individually stopped slots."""
        with transaction(self.path) as db:
            saved=db.execute("SELECT value FROM meta WHERE key='operator_resume_intent'").fetchone()
            resume=json.loads(saved[0]) if saved else {}
            restored=[]
            for alias,revision in resume.items():
                room=self._room(db,alias)
                if room['desired']=='stopped' and room['revision']==revision and room['pending'] is None:
                    db.execute("UPDATE rooms SET desired='running',revision=revision+1 WHERE alias=?",(alias,))
                    restored.append(alias)
                    self._audit(db,None,alias,'local-operator','start-intent','applied',revision+1)
            self._meta(db,'operator_resume_intent',{})
            self._intent_changed(db)
            return restored
    def provider_capacity(self,maximum,external_rooms):
        integer(maximum,0,1024);integer(external_rooms,0,1024)
        with transaction(self.path) as db:
            self._meta(db,'provider_room_limit',maximum);self._meta(db,'external_rooms',external_rooms)
            return self._limits(db)
    def reserve(self,alias,generation,*,operation=None):
        check_alias(alias);require(type(generation) is str and re.fullmatch('[a-f0-9]{32}',generation) is not None,'invalid_managed_generation')
        if operation is not None:identifier(operation)
        with transaction(self.path) as db:
            room=self._room(db,alias);require(room['desired']=='running','room_intentionally_stopped',409)
            old=db.execute('SELECT * FROM reservations WHERE alias=?',(alias,)).fetchone()
            if old:
                require(old['generation']==generation and old['phase']=='creating' and old['room_id'] is None,
                        'room_ownership_requires_review',409)
                return {'reserved':True,'replayed':True}
            limits=self._limits(db);require(limits['capacity_used']<limits['effective_limit'],'host_capacity_reached',409)
            db.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?)',(alias,generation,None,'creating',operation,self.clock()))
            self._audit(db,operation,alias,'controller','reserve','applying')
            return {'reserved':True,'replayed':False}
    def observe_owned(self,alias,generation,room_id,*,adopt=False):
        check_alias(alias);integer(room_id,1,2**63-1)
        require(type(generation) is str and re.fullmatch('[a-f0-9]{32}',generation) is not None,'invalid_managed_generation')
        with transaction(self.path) as db:
            room=self._room(db,alias)
            old=db.execute('SELECT * FROM reservations WHERE alias=?',(alias,)).fetchone()
            require(adopt or old is not None and old['generation']==generation,'room_ownership_requires_review',409)
            if old:require(old['room_id'] in (None,room_id),'room_ownership_requires_review',409)
            # Existing rooms are adopted even over a lowered ceiling, never closed
            # merely to initialize administration. Their slots still count.
            db.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?) ON CONFLICT(alias) DO UPDATE SET '
                'generation=excluded.generation,room_id=excluded.room_id,phase=excluded.phase,at=excluded.at',
                (alias,generation,room_id,'owned',None,self.clock()))
            # One worker can refresh several upstream rooms. Its process UUID
            # is therefore not a valid CAS identity for the room itself.
            incarnation=room['room_generation'] if room['room_id']==room_id and room['room_generation'] else uuid.uuid4().hex
            db.execute("UPDATE rooms SET phase='running',room_id=?,room_generation=?,failure=NULL WHERE alias=?",(room_id,incarnation,alias))
    def uncertain_creation(self,alias,generation,candidate_id=None):
        if candidate_id is not None:integer(candidate_id,1,2**63-1)
        with transaction(self.path) as db:
            row=db.execute('SELECT * FROM reservations WHERE alias=?',(alias,)).fetchone()
            require(row is not None and row['generation']==generation,'room_ownership_requires_review',409)
            db.execute("UPDATE reservations SET phase='uncertain',room_id=COALESCE(?,room_id) WHERE alias=?",(candidate_id,alias))
            db.execute("UPDATE rooms SET phase='uncertain',failure='room_ownership_requires_review' WHERE alias=?",(alias,))
    def confirmed_closed(self,alias,generation,room_id):
        """Call only after upstream confirmation, never after transport teardown."""
        check_alias(alias);integer(room_id,1,2**63-1)
        with transaction(self.path) as db:
            old=db.execute('SELECT * FROM reservations WHERE alias=?',(alias,)).fetchone()
            if old is None:
                row=self._room(db,alias)
                require(row['room_id'] is None,'room_ownership_requires_review',409)
                return
            require(old['generation']==generation and old['room_id']==room_id,'room_generation_changed',409)
            db.execute('DELETE FROM reservations WHERE alias=?',(alias,))
            db.execute("UPDATE rooms SET phase='stopped',room_id=NULL,room_generation=NULL,failure=NULL WHERE alias=?",(alias,))
    def reconcile_closed_journal(self,journal):
        """Startup/recovery only: consume exact clean native closure evidence.

        This never changes the authoritative ownership journal. A reservation
        from a newer/unknown generation cannot be erased by an older clean row.
        Caller must hold the stopped controller's owner lease.
        """
        from ..reconciliation import _journal
        aliases={r['alias'] for r in self.snapshot()['rooms']}
        _journal(journal,aliases)
        with transaction(self.path) as db:
            for reservation in db.execute('SELECT * FROM reservations').fetchall():
                row=journal['rooms'].get(reservation['alias'],{})
                require(row.get('phase') in ('stopped','parked') and row.get('operation')=='closed'
                        and not row.get('ownership_unknown') and row.get('room_id') is None
                        and row.get('generation')==reservation['generation'],
                        'room_ownership_requires_review',409)
                db.execute('DELETE FROM reservations WHERE alias=?',(reservation['alias'],))
                db.execute("UPDATE rooms SET phase='stopped',room_id=NULL,room_generation=NULL,failure=NULL WHERE alias=?",(reservation['alias'],))
                self._audit(db,None,reservation['alias'],'native-recovery','confirmed-closed','applied')
    def cancelled_before_dispatch(self,alias,generation):
        """Release only an unused reservation with no upstream dispatch intent."""
        with transaction(self.path) as db:
            row=db.execute('SELECT * FROM reservations WHERE alias=?',(alias,)).fetchone()
            require(row is not None and row['generation']==generation and row['phase']=='creating'
                    and row['room_id'] is None,'room_ownership_requires_review',409)
            db.execute('DELETE FROM reservations WHERE alias=?',(alias,))
    def mark_dispatched(self,alias,generation):
        with transaction(self.path) as db:
            row=db.execute('SELECT * FROM reservations WHERE alias=?',(alias,)).fetchone()
            require(row is not None and row['generation']==generation and row['phase']=='creating',
                    'room_ownership_requires_review',409)
            db.execute("UPDATE reservations SET phase='uncertain' WHERE alias=?",(alias,))
    def enqueue(self,value):
        """Admit a local command without manufacturing a remote receipt."""
        return self._enqueue(value,from_hub=False)
    def _enqueue(self,value,*,from_hub):
        self.refresh_binding();request(value);require(all(value[k]==v for k,v in self.binding.items()),'host_identity_mismatch',403)
        now=self.clock()
        raw=canonical(value).decode();pin=digest(value)
        with transaction(self.path) as db:
            delivered=db.execute('SELECT digest FROM delivered WHERE id=?',(value['id'],)).fetchone()
            require((delivered is not None and delivered['digest']==pin) if from_hub else delivered is None,
                    'managed_operation_conflict',409)
            require(db.execute('SELECT 1 FROM local_actions WHERE id=?',(value['id'],)).fetchone() is None,
                    'managed_operation_conflict',409)
            old=db.execute('SELECT * FROM operations WHERE id=?',(value['id'],)).fetchone()
            if old:
                require(old['digest']==pin,'managed_operation_conflict',409)
                return self._receipt(old)
            require(now<value['expires_at']<=now+(7*86400 if value['action']=='catalog' else 1800),'operation_expired',409)
            room=self._room(db,value['alias'])
            require(room['revision']==value['expected_revision'],'configuration_revision_changed',409)
            require(room['room_generation']==value['expected_room_generation'],'room_generation_changed',409)
            require(room['pending'] is None or value['action'] in ('inspect','cancel'),'managed_operation_pending',409)
            if value['action']=='configure':
                try:validate(value['patch'],current=json.loads(room['config']))
                except (ValueError,TypeError):raise ValueError('invalid_settings') from None
            db.execute('INSERT INTO operations(id,digest,payload,alias,phase,created,changed) VALUES(?,?,?,?,?,?,?)',
                       (value['id'],pin,raw,value['alias'],'received',now,now))
            if value['action'] not in ('inspect','cancel'):
                db.execute('UPDATE rooms SET pending=? WHERE alias=?',(value['id'],value['alias']))
            if value['action']!='inspect':self._intent_changed(db)
            self._audit(db,value['id'],value['alias'],value['actor'],value['action'],'received',room['revision'])
            return self._receipt(db.execute('SELECT * FROM operations WHERE id=?',(value['id'],)).fetchone())
    @staticmethod
    def _receipt(row):
        return {'id':row['id'],'alias':row['alias'],'phase':row['phase'],'failure_code':row['error'],
                'result':json.loads(row['result']) if row['result'] else None}
    def pending(self):
        with transaction(self.path) as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM operations WHERE phase IN ('received','validated','waiting_boundary') ORDER BY created,id")]
    def operation(self,id):
        identifier(id)
        with transaction(self.path) as db:
            row=db.execute('SELECT * FROM operations WHERE id=?',(id,)).fetchone()
            require(row is not None,'managed_operation_not_found',404);return self._receipt(row)
    def begin(self,id,*,ready):
        """Persist intent before mutation; applying intents are never blindly replayed."""
        with transaction(self.path) as db:
            op=db.execute('SELECT * FROM operations WHERE id=?',(id,)).fetchone()
            require(op is not None,'managed_operation_not_found',404)
            if op['phase'] in TERMINAL:return self._receipt(op)
            require(op['phase']!='applying','managed_application_requires_review',409)
            value=json.loads(op['payload']);room=self._room(db,op['alias'])
            error=('operation_expired' if value['expires_at']<=self.clock() else
                   'configuration_revision_changed' if room['revision']!=(op['revision_after'] if op['revision_after'] is not None else value['expected_revision']) else
                   'room_generation_changed' if room['room_generation']!=value['expected_room_generation'] else None)
            if error:
                if op['revision_after'] is not None:self._restore(db,op)
                return self._finish(db,op,'expired' if error=='operation_expired' else 'refused',error,None)
            if not ready:
                db.execute("UPDATE operations SET phase='waiting_boundary',changed=? WHERE id=?",(self.clock(),id))
                db.execute("UPDATE rooms SET phase='waiting_boundary' WHERE alias=?",(op['alias'],))
            elif op['revision_after'] is not None:
                db.execute("UPDATE operations SET phase='applying',changed=? WHERE id=?",(self.clock(),id))
                db.execute("UPDATE rooms SET phase='applying' WHERE alias=?",(op['alias'],))
            else:
                desired='stopped' if value['action']=='close' else 'running' if value['action'] in ('open','restart') else room['desired']
                settings=(json.loads(room['config']) if value['action']=='catalog'
                          else merge(json.loads(room['config']),value['patch']))
                revision=room['revision']+int(value['action'] in ('open','close','restart','configure'))
                db.execute("UPDATE operations SET phase='applying',desired_before=?,revision_after=?,previous_config=?,changed=? WHERE id=?",
                           (room['desired'],revision,room['config'],self.clock(),id))
                db.execute("UPDATE rooms SET desired=?,revision=?,config=?,phase='applying' WHERE alias=?",
                           (desired,revision,canonical(settings).decode(),op['alias']))
                self._audit(db,id,op['alias'],value['actor'],value['action'],'applying',revision,settings)
            return self._receipt(db.execute('SELECT * FROM operations WHERE id=?',(id,)).fetchone())
    def defer(self,id):
        """Only for a proven pre-dispatch occupancy/deployment change."""
        with transaction(self.path) as db:
            op=db.execute('SELECT * FROM operations WHERE id=?',(id,)).fetchone()
            require(op is not None and op['phase']=='applying','managed_application_requires_review',409)
            db.execute("UPDATE operations SET phase='waiting_boundary',changed=? WHERE id=?",(self.clock(),id))
            db.execute("UPDATE rooms SET phase='waiting_boundary' WHERE alias=?",(op['alias'],))
    def _restore(self,db,op):
        if op['previous_config'] is not None:
            db.execute('UPDATE rooms SET config=?,desired=?,applied_revision=revision WHERE alias=?',
                       (op['previous_config'],op['desired_before'],op['alias']))
    def reject_before_mutation(self,id,error):
        require(error in ERRORS,'invalid_managed_receipt')
        with transaction(self.path) as db:
            op=db.execute('SELECT * FROM operations WHERE id=?',(id,)).fetchone()
            require(op is not None and op['phase'] in ('received','validated','waiting_boundary','applying'),'managed_application_requires_review',409)
            self._restore(db,op)
            return self._finish(db,op,'refused',error,None)
    def finish(self,id,phase,*,error=None,result=None):
        require(phase in TERMINAL and error in ERRORS,'invalid_managed_receipt')
        with transaction(self.path) as db:
            op=db.execute('SELECT * FROM operations WHERE id=?',(id,)).fetchone()
            require(op is not None,'managed_operation_not_found',404)
            if op['phase'] in TERMINAL:
                require(op['phase']==phase and op['error']==error,'managed_receipt_conflict',409)
                return self._receipt(op)
            require(phase not in ('applied','uncertain') or op['phase']=='applying','managed_application_requires_review',409)
            return self._finish(db,op,phase,error,result)
    def cancel_target(self,id):
        with transaction(self.path) as db:
            op=db.execute('SELECT * FROM operations WHERE id=?',(id,)).fetchone()
            require(op is not None and op['phase']=='applying','managed_application_requires_review',409)
            value=json.loads(op['payload']);require(value['action']=='cancel','invalid_managed_cancel')
            target=db.execute('SELECT * FROM operations WHERE id=?',(value['cancel_operation_id'],)).fetchone()
            require(target is not None and target['alias']==op['alias'] and target['phase'] in ('received','validated','waiting_boundary'),'managed_application_requires_review',409)
            self._restore(db,target)
            self._finish(db,target,'cancelled','operation_cancelled',None)
            return self._finish(db,op,'applied',None,{'application':'cancel','revision':self._room(db,op['alias'])['revision']})
    def _finish(self,db,op,phase,error,result):
        # Result is a fixed controller projection; never serialize arbitrary errors,
        # configuration/passwords or provider bodies into public telemetry.
        require(result is None or type(result) is dict and not set(result)-{'room_id','revision','application','catalog_content_id','catalog_set_id'},'invalid_managed_result')
        if result is not None:
            for key in ('catalog_content_id','catalog_set_id'):
                if key in result:require(type(result[key]) is str and re.fullmatch('[a-f0-9]{64}',result[key]) is not None,'invalid_managed_result')
            if 'room_id' in result and result['room_id'] is not None:integer(result['room_id'],1,2**63-1)
            if 'revision' in result:integer(result['revision'],0,2**53-1)
            if 'application' in result:require(result['application'] in ('live','next_map','recreate','lifecycle','inspect','cancel'),'invalid_managed_result')
        value=json.loads(op['payload']);room=self._room(db,op['alias'])
        db.execute('UPDATE operations SET phase=?,error=?,result=?,changed=? WHERE id=?',
                   (phase,error,canonical(result).decode() if result is not None else None,self.clock(),op['id']))
        if room['pending']==op['id']:
            db.execute('UPDATE rooms SET pending=NULL,failure=?,phase=CASE WHEN ?=\'uncertain\' THEN \'uncertain\' WHEN room_id IS NULL THEN \'stopped\' ELSE \'running\' END WHERE alias=?',
                       (error,phase,op['alias']))
        if phase=='applied' and value['action'] in ('configure','open','restart'):
            db.execute('UPDATE rooms SET effective=config,applied_revision=revision WHERE alias=?',(op['alias'],))
        self._audit(db,op['id'],op['alias'],value['actor'],value['action'],phase,room['revision'])
        return self._receipt(db.execute('SELECT * FROM operations WHERE id=?',(op['id'],)).fetchone())
    def recover_applying(self):
        """Mark interrupted mutations uncertain; reconciler must inspect reality."""
        with transaction(self.path) as db:
            for op in db.execute("SELECT * FROM operations WHERE phase='applying'").fetchall():
                self._finish(db,op,'uncertain','managed_application_requires_review',None)
    def room(self,alias,*,private=False):
        check_alias(alias)
        with transaction(self.path) as db:
            row=dict(self._room(db,alias))
            for key in ('config','effective'):
                value=json.loads(row[key]);row[key]=value if private else public_settings(value)
            return row
    def snapshot(self,*,controller_phase='unavailable'):
        self.refresh_binding()
        with transaction(self.path) as db:
            limits=self._limits(db);rows=[]
            for row in db.execute('SELECT * FROM rooms ORDER BY alias'):
                cfg=json.loads(row['config'])
                rows.append({'alias':row['alias'],'desired':row['desired'],'revision':row['revision'],
                    'applied_revision':row['applied_revision'],'phase':row['phase'],'room_id':row['room_id'],
                    'room_generation':row['room_generation'],'preset':cfg['preset'],'visibility':cfg.get('visibility','public'),
                    'pending_operation':row['pending'],'failure_code':row['failure']})
            return {'protocol_version':1,'policy_version':self._get(db,'policy_version'),**self.binding,
                    **{k:limits[k] for k in ('max_concurrent_rooms','provider_room_limit','capacity_used')},
                    'rooms':rows,'controller_phase':controller_phase}
