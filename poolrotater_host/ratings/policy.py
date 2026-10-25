"""Private suppression journal, persisted before canonical mutation.

Restore requires the latest independently retained journal, not just an old DB.
Targets are keyed digests. Deletion retains replay hashes, never voter payloads.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid

from ..layout import read_json,atomic_json,file_lease
from ..network.contracts import require,fields,identifier,integer,canonical,fingerprint


class Policy:
    def __init__(self,store):self.store=store;self.path=store.root/'ratings-policy.json'

    def initialize(self,db):
        row=db.execute("SELECT value FROM meta WHERE key='ratings_policy_id'").fetchone()
        if row is None:
            if os.path.lexists(self.path):value=self.read()
            else:
                value={'format':1,'policy_id':str(uuid.uuid4()),'salt':secrets.token_hex(32),'operations':[]}
                atomic_json(self.path,value)
            require(not value['operations'],'ratings_policy_requires_review',409)
            db.execute("INSERT INTO meta VALUES('ratings_policy_id',?)",(value['policy_id'],))
            db.execute("INSERT INTO meta VALUES('ratings_policy_applied','0')")
            db.execute("INSERT INTO meta VALUES('ratings_policy_sha256',?)",(fingerprint(value),))
        else:self.check(db)

    def read(self):
        value=read_json(self.path,maximum=512*1024)
        fields(value,('format','policy_id','salt','operations'))
        require(type(value['format']) is int and value['format']==1,'ratings_policy_requires_review',409)
        identifier(value['policy_id'])
        require(type(value['salt']) is str and re.fullmatch('[a-f0-9]{64}',value['salt'])
                and type(value['operations']) is list and len(value['operations'])<=2048,'ratings_policy_requires_review',409)
        seen=set();watermark=0
        for index,op in enumerate(value['operations'],1):
            fields(op,('operation_id','sequence','subject','target','effect','watermark'))
            identifier(op['operation_id']);integer(op['sequence'])
            require(op['operation_id'] not in seen and op['sequence']==index
                    and op['subject'] in ('user','key','host') and op['effect'] in ('suppress','allow','delete')
                    and (op['effect']!='delete' or op['subject']=='user')
                    and type(op['target']) is str and re.fullmatch('[a-f0-9]{64}',op['target']),
                    'ratings_policy_requires_review',409)
            integer(op['watermark']);require(op['watermark']>watermark,'ratings_policy_requires_review',409)
            watermark=op['watermark'];seen.add(op['operation_id'])
        return value

    def check(self,db,*,allow_pending=False):
        value=self.read()
        binding=db.execute("SELECT value FROM meta WHERE key='ratings_policy_id'").fetchone()
        applied=db.execute("SELECT value FROM meta WHERE key='ratings_policy_applied'").fetchone()
        digest=db.execute("SELECT value FROM meta WHERE key='ratings_policy_sha256'").fetchone()
        require(binding is not None and binding[0]==value['policy_id'] and applied is not None
                and applied[0].isdigit() and 0<=int(applied[0])<=len(value['operations']),
                'ratings_policy_requires_review',409)
        prefix={**value,'operations':value['operations'][:int(applied[0])]}
        require(digest is not None and digest[0]==fingerprint(prefix),'ratings_policy_requires_review',409)
        require(allow_pending or int(applied[0])==len(value['operations']),'ratings_policy_recovery_required',503)
        return value,int(applied[0])

    @staticmethod
    def token(value,subject,*ids):
        return hmac.new(bytes.fromhex(value['salt']),canonical([subject,*ids]),hashlib.sha256).hexdigest()

    def rules(self,value):
        return {(o['subject'],o['target']):o for o in value['operations']}

    def excluded(self,value,*,bid,uid,host=None):
        rules=self.rules(value)
        for subject,ids in (('user',(uid,)),('key',(bid,uid)),('host',(host,))):
            if subject=='host' and host is None:continue
            rule=rules.get((subject,self.token(value,subject,*ids)))
            if rule and rule['effect']!='allow':return rule
        return None

    def effective_state(self,value,state,*,bid,uid,host):
        # Global allow cutoffs also cover keys never observed before. A stale
        # unknown-revision backlog cannot slip through after reallowing a user.
        rules=self.rules(value);floor=0
        for subject,ids in (('user',(uid,)),('key',(bid,uid)),('host',(host,))):
            rule=rules.get((subject,self.token(value,subject,*ids)))
            if rule:floor=max(floor,rule['watermark'])
        if state is None and floor==0:return None
        result=dict(state) if state is not None else {'rating':None,'revision':0,'event_id':None,'host_id':None,'suppressed':0}
        result['revision']=max(result['revision'],floor)
        result['suppressed']=bool(result['suppressed'] or self.excluded(value,bid=bid,uid=uid,host=host))
        return result

    def _matches(self,value,op,*,bid,uid,host=None):
        ids={'user':(uid,),'key':(bid,uid),'host':(host,)}[op['subject']]
        return (op['subject']!='host' or host is not None) and self.token(value,op['subject'],*ids)==op['target']

    def apply(self,db,value,op):
        # Use only rules up to this operation when replaying a restored database.
        value={**value,'operations':value['operations'][:op['sequence']]}
        sources=db.execute('SELECT * FROM ratings_sources').fetchall();affected=set()
        for row in sources:
            if self._matches(value,op,bid=row['beatmap_id'],uid=row['user_id'],host=row['host_id']):
                affected.add((row['beatmap_id'],row['user_id']))
                if op['effect']!='allow':
                    db.execute('DELETE FROM ratings_sources WHERE host_id=? AND beatmap_id=? AND user_id=?',
                               (row['host_id'],row['beatmap_id'],row['user_id']))
        for row in db.execute('SELECT * FROM ratings_current').fetchall():
            if self._matches(value,op,bid=row['beatmap_id'],uid=row['user_id'],host=row['host_id']):
                affected.add((row['beatmap_id'],row['user_id']))
        for bid,uid in affected:
            if op['effect']=='delete':
                db.execute('DELETE FROM ratings_current WHERE beatmap_id=? AND user_id=?',(bid,uid));continue
            excluded=self.excluded(value,bid=bid,uid=uid)
            candidate=None
            if excluded is None:
                for row in db.execute('SELECT * FROM ratings_sources WHERE beatmap_id=? AND user_id=? ORDER BY revision DESC',(bid,uid)):
                    if self.excluded(value,bid=bid,uid=uid,host=row['host_id']) is None:candidate=row;break
            db.execute('INSERT INTO ratings_current VALUES(?,?,?,?,?,?,?) ON CONFLICT(beatmap_id,user_id) DO UPDATE SET '
                       'rating=excluded.rating,revision=excluded.revision,event_id=excluded.event_id,host_id=excluded.host_id,suppressed=excluded.suppressed',
                       (bid,uid,candidate['rating'] if candidate else None,op['watermark'],candidate['event_id'] if candidate else None,
                        candidate['host_id'] if candidate else None,int(excluded is not None)))
        if op['effect']=='delete':
            for row in db.execute('SELECT event_id,beatmap_id,user_id,host_id FROM ratings_events WHERE user_id IS NOT NULL').fetchall():
                if self._matches(value,op,bid=row['beatmap_id'],uid=row['user_id'],host=row['host_id']):
                    db.execute("UPDATE ratings_events SET beatmap_id=NULL,user_id=NULL,payload='{}' WHERE event_id=?",(row['event_id'],))
        current=int(db.execute("SELECT value FROM meta WHERE key='ratings_watermark'").fetchone()[0])
        db.execute("UPDATE meta SET value=? WHERE key='ratings_watermark'",(str(max(current,op['watermark'])),))
        db.execute("UPDATE meta SET value=? WHERE key='ratings_policy_applied'",(str(op['sequence']),))
        db.execute("UPDATE meta SET value=? WHERE key='ratings_policy_sha256'",(fingerprint(value),))

    def change(self,*,subject,effect,user_id=None,beatmap_id=None,host_id=None,ruleset_id=None):
        require(subject in ('user','key','host') and effect in ('suppress','allow','delete')
                and (effect!='delete' or subject=='user'),'invalid_ratings_policy')
        if subject in ('user','key'):integer(user_id)
        require(ruleset_id is None or (subject=='key' and type(ruleset_id) is int and ruleset_id in (1,2)),
                'invalid_ratings_policy')
        if subject=='key':
            integer(beatmap_id)
            from bot.rating_outbox import map_key
            beatmap_id=map_key(beatmap_id,ruleset_id)
        if subject=='host':identifier(host_id)
        require((subject=='host')==(host_id is not None) and (subject=='key')==(beatmap_id is not None)
                and (subject!='host')==(user_id is not None),'invalid_ratings_policy')
        with file_lease(self.store.root/'ratings-policy.lock'),self.store.transaction() as db:
            value,_=self.check(db);ids={'user':(user_id,),'key':(beatmap_id,user_id),'host':(host_id,)}[subject]
            target=self.token(value,subject,*ids);previous=self.rules(value).get((subject,target))
            require(previous is None or previous['effect']!='delete' or effect=='delete','ratings_deleted_subject_refused',409)
            if previous and previous['effect']==effect:return {'ok':True,'already_applied':True}
            require(len(value['operations'])<2048,'ratings_policy_capacity_required',503)
            watermark=int(db.execute("SELECT value FROM meta WHERE key='ratings_watermark'").fetchone()[0])+1
            integer(watermark)
            op={'operation_id':str(uuid.uuid4()),'sequence':len(value['operations'])+1,'subject':subject,
                'target':target,'effect':effect,'watermark':watermark}
            value={**value,'operations':[*value['operations'],op]}
            require(len(canonical(value))<=512*1024,'ratings_policy_capacity_required',503)
            atomic_json(self.path,value)  # durable fence before destructive mutation
            self.apply(db,value,op);self.store._audit(db,'ratings_policy_'+effect)
        return {'ok':True,'policy_applied':True,'processing_watermark':watermark,'provider_access':False}

    def recover(self):
        with file_lease(self.store.root/'ratings-policy.lock'),self.store.transaction() as db:
            value,applied=self.check(db,allow_pending=True)
            for op in value['operations'][applied:]:self.apply(db,value,op)
        return {'ok':True,'policy_recovered':True,'operations_replayed':len(value['operations'])-applied}
