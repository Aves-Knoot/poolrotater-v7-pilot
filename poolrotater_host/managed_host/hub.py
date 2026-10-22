"""Private fleet command queue, bound to fresh authenticated host capabilities."""
import json
import time
import uuid
from ..network.contracts import fields, identifier, integer, number, require, verifier, canonical
from ..diagnostic_logs.common import private_database, transaction
from .contracts import request, capabilities, digest, PHASES, TERMINAL, ERRORS

PATH='/v1/hosts/self/managed-rooms'
INFER=object()

class Repository:
    def __init__(self,store):
        self.store=store;self.path=private_database(store.root,'managed-rooms.sqlite3')
        with transaction(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS reports(host TEXT PRIMARY KEY,sequence INTEGER NOT NULL,pin TEXT NOT NULL,capabilities TEXT NOT NULL,received REAL NOT NULL,response TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,host TEXT NOT NULL,alias TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,receipt TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS pending_host_rooms ON requests(host,alias,expires)')
            columns={r[1] for r in db.execute('PRAGMA table_info(requests)')}
            if 'submission' not in columns:db.execute('ALTER TABLE requests ADD COLUMN submission TEXT')
    def _participation(self,db,host):
        """New managed clients join the existing sharing policy, not a bypass.

        Revocation/authentication precede this; explicitly disabled or suppressed
        contributors remain fenced. Old clients never enter this route.
        """
        from ..ratings.repository import _validate
        from ..ratings.policy import Policy
        from ..network.contracts import SCOPES
        _validate(db);Policy(self.store).check(db)
        old=db.execute('SELECT mode FROM ratings_policy WHERE host_id=?',(host['id'],)).fetchone()
        require(old is None or old['mode']!='disabled','scope_refused',403)
        if old is None or old['mode']!='contribute':
            db.execute("INSERT INTO ratings_policy(host_id,mode) VALUES(?,'contribute') ON CONFLICT(host_id) DO UPDATE SET mode='contribute'",(host['id'],))
            self.store._audit(db,'managed_participation',host['id'])
        for credential in db.execute('SELECT verifier,scopes FROM credentials WHERE host_id=? AND retired=0',(host['id'],)).fetchall():
            scopes=set(json.loads(credential['scopes']))
            require(scopes<=set(SCOPES)|{'ratings-submit','ratings-query','ratings-seed','ratings-read'},'scope_refused',403)
            required={'ratings-submit','ratings-query','ratings-read'}
            if not required<=scopes:
                db.execute('UPDATE credentials SET scopes=? WHERE verifier=?',(json.dumps(sorted(scopes|required)),credential['verifier']))
        if not host['directory']:db.execute('UPDATE hosts SET directory=1 WHERE id=?',(host['id'],))
    @staticmethod
    def _receipt(value):
        fields(value,('id','alias','phase','failure_code','result'))
        identifier(value['id']);from .contracts import alias
        alias(value['alias'])
        require(type(value['phase']) is str and value['phase'] in PHASES,'invalid_managed_receipt')
        require(value['failure_code'] is None or type(value['failure_code']) is str and value['failure_code'] in ERRORS,'invalid_managed_receipt')
        result=value['result']
        require(result is None or type(result) is dict and not set(result)-{'room_id','revision','application','catalog_content_id','catalog_set_id'},'invalid_managed_result')
        if result is not None:
            import re
            for key in ('catalog_content_id','catalog_set_id'):
                if key in result:require(type(result[key]) is str and re.fullmatch('[a-f0-9]{64}',result[key]) is not None,'invalid_managed_result')
            if result.get('room_id') is not None:integer(result['room_id'],1,2**63-1)
            if 'revision' in result:integer(result['revision'],0,2**53-1)
            require('application' not in result or result['application'] in ('live','next_map','recreate','lifecycle','inspect','cancel'),'invalid_managed_result')
        return value
    def accept(self,token,value):
        fields(value,('protocol_version','installation_id','generation','sequence','sent_at','capabilities','receipts'))
        integer(value['protocol_version'],1,1);integer(value['sequence'],1,2**53-1);number(value['sent_at'])
        caps=capabilities(value['capabilities'])
        require(type(value['receipts']) is list and len(value['receipts'])<=32,'invalid_managed_receipts')
        for row in value['receipts']:self._receipt(row)
        pin=digest(value);now=self.store.clock()
        with self.store.transaction() as auth:
            host,_=self.store._authenticate(auth,verifier(token),'heartbeat')
            require(all(value[k]==host[k] and caps[k]==host[k] for k in ('installation_id','generation'))
                    and caps['host_id']==host['id'],'host_identity_mismatch',403)
            with transaction(self.path) as db:
                previous=db.execute('SELECT * FROM reports WHERE host=?',(host['id'],)).fetchone()
                if previous:
                    require(value['sequence']>=previous['sequence'],'stale_sequence',409)
                    if value['sequence']==previous['sequence']:
                        require(pin==previous['pin'],'sequence_conflict',409)
                        self._participation(auth,host)
                        return json.loads(previous['response'])
                require(abs(now-value['sent_at'])<=300,'invalid_request')
                self._participation(auth,host)
                for receipt in value['receipts']:
                    item=db.execute('SELECT * FROM requests WHERE id=? AND host=?',(receipt['id'],host['id'])).fetchone()
                    require(item is not None and item['alias']==receipt['alias'],'unknown_managed_receipt',409)
                    old=json.loads(item['receipt']) if item['receipt'] else None
                    if old and old['phase'] in TERMINAL:
                        require(old==receipt,'managed_receipt_conflict',409)
                    db.execute('UPDATE requests SET receipt=? WHERE id=?',(canonical(receipt).decode(),receipt['id']))
                selected=[]
                for row in db.execute('SELECT payload,receipt FROM requests WHERE host=? AND expires>? ORDER BY created,id',(host['id'],now)):
                    # Any acknowledged receipt proves durable local admission.
                    # The host owns subsequent execution/progress. Repeatedly
                    # returning an occupied room's waiting operation can starve
                    # unrelated rooms when large requests fill the reply budget.
                    if row['receipt']:continue
                    item=json.loads(row['payload'])
                    if len(canonical({'ok':True,'sequence':value['sequence'],'requests':[*selected,item]}))>65536:break
                    selected.append(item)
                    if len(selected)>=8:break
                response={'ok':True,'sequence':value['sequence'],'requests':selected}
                db.execute('INSERT OR REPLACE INTO reports VALUES(?,?,?,?,?,?)',(host['id'],value['sequence'],pin,canonical(caps).decode(),now,canonical(response).decode()))
                return response
    def queue(self,host_id,alias,action,*,patch=None,timing='when_empty',actor='hub-administrator',role='FLEET_ADMIN',
              expected_revision=None,expected_room_generation=INFER,expires_seconds=600,cancel_operation_id=None,
              operation_id=None):
        """Root/local administrator API only. No host token may enqueue commands."""
        identifier(host_id);integer(expires_seconds,30,7*86400 if action=='catalog' else 1800)
        operation_id=str(uuid.uuid4()) if operation_id is None else operation_id;identifier(operation_id)
        submission=digest({'host_id':host_id,'alias':alias,'action':action,'patch':patch,'timing':timing,
            'actor':actor,'role':role,'expected_revision':expected_revision,
            'expected_room_generation':{'infer':True} if expected_room_generation is INFER else expected_room_generation,
            'expires_seconds':expires_seconds,'cancel_operation_id':cancel_operation_id})
        now=self.store.clock()
        with self.store.transaction() as auth:
            host=auth.execute('SELECT * FROM hosts WHERE id=? AND revoked=0',(host_id,)).fetchone()
            require(host is not None,'host_not_found',404)
            with transaction(self.path) as db:
                previous=db.execute('SELECT * FROM requests WHERE id=?',(operation_id,)).fetchone()
                if previous:
                    require(previous['host']==host_id and previous['submission']==submission,'managed_operation_conflict',409)
                    return {'ok':True,'request':json.loads(previous['payload']),'replayed':True,
                            'receipt':json.loads(previous['receipt']) if previous['receipt'] else None}
                report=db.execute('SELECT * FROM reports WHERE host=?',(host_id,)).fetchone()
                require(report is not None and 0<=now-report['received']<=120,'managed_fresh_capabilities_required',409)
                caps=json.loads(report['capabilities']);capabilities(caps)
                require(all(caps[k]==host[k] for k in ('installation_id','generation')),'host_identity_mismatch',409)
                row=next((r for r in caps['rooms'] if r['alias']==alias),None)
                require(row is not None,'room_not_found',404)
                # Explicit user-supplied revision/generation is never replaced by
                # newer observations; a stale UI must receive a conflict.
                rev=row['revision'] if expected_revision is None else expected_revision
                gen=row['room_generation'] if expected_room_generation is INFER else expected_room_generation
                require(rev==row['revision'] and gen==row['room_generation'],'configuration_revision_changed',409)
                value=request({'protocol_version':1,'policy_version':1,'id':operation_id,'host_id':host_id,
                    'installation_id':host['installation_id'],'generation':host['generation'],'alias':alias,
                    'actor':actor,'role':role,'action':action,'expected_revision':rev,'expected_room_generation':gen,
                    'timing':timing,'patch':patch or {},'expires_at':now+expires_seconds,'cancel_operation_id':cancel_operation_id})
                require(len(canonical(value))<=60000,'request_too_large',413)
                if action not in ('inspect','cancel'):
                    for pending in db.execute('SELECT receipt FROM requests WHERE host=? AND alias=? AND expires>?',(host_id,alias,now)):
                        require(pending['receipt'] and json.loads(pending['receipt'])['phase'] in TERMINAL,'managed_operation_pending',409)
                db.execute('INSERT INTO requests(id,host,alias,payload,created,expires,receipt,submission) VALUES(?,?,?,?,?,?,NULL,?)',
                    (value['id'],host_id,alias,canonical(value).decode(),now,value['expires_at'],submission))
                self.store._audit(auth,'managed_room_requested',host_id,value['id'])
                return {'ok':True,'request':value}
    def inspect(self,host_id):
        identifier(host_id)
        with transaction(self.path) as db:
            row=db.execute('SELECT * FROM reports WHERE host=?',(host_id,)).fetchone()
            # Never return request payloads/passwords through generic inspection.
            requests=[]
            for item in db.execute('SELECT id,alias,created,expires,receipt FROM requests WHERE host=? ORDER BY created DESC LIMIT 50',(host_id,)):
                requests.append({**{k:item[k] for k in ('id','alias','created','expires')},
                    'receipt':json.loads(item['receipt']) if item['receipt'] else None})
            return {'ok':True,'capabilities':json.loads(row['capabilities']) if row else None,
                    'received_at':row['received'] if row else None,'operations':requests}
