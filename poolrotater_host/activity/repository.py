"""Authenticated private activity storage and complete privacy-filtered export."""
import json
from pathlib import Path
import time
from ..network.contracts import require, verifier, canonical, fingerprint, identifier
from .contracts import consent, batch
from .storage import connection


class Repository:
    def __init__(self, store):
        self.store=store;self.path=Path(store.path).with_name('activity.sqlite3')
        with connection(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS consents(host_id TEXT PRIMARY KEY,consent_id TEXT NOT NULL,enabled INTEGER NOT NULL,consent_at REAL NOT NULL,received REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS consent_history(host_id TEXT NOT NULL,consent_id TEXT NOT NULL,payload_hash TEXT NOT NULL,PRIMARY KEY(host_id,consent_id))')
            db.execute('CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,host_id TEXT NOT NULL,consent_id TEXT NOT NULL,kind TEXT NOT NULL,user_id INTEGER NOT NULL,room_id INTEGER,source_key TEXT NOT NULL,payload_hash TEXT NOT NULL,payload TEXT NOT NULL,received REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS activity_host_events ON events(host_id,received)')
            db.execute('CREATE TABLE IF NOT EXISTS privacy(host_id TEXT NOT NULL,user_id INTEGER NOT NULL,hidden INTEGER NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(host_id,user_id))')
            db.execute('CREATE TABLE IF NOT EXISTS records(host_id TEXT NOT NULL,source_key TEXT NOT NULL,event_id TEXT NOT NULL,payload TEXT NOT NULL,kind TEXT NOT NULL,user_id INTEGER NOT NULL,room_id INTEGER,PRIMARY KEY(host_id,source_key))')
            db.execute('CREATE INDEX IF NOT EXISTS activity_records_user ON records(user_id,kind)')
            new_names=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='names'").fetchone() is None
            db.execute('CREATE TABLE IF NOT EXISTS names(host_id TEXT NOT NULL,user_id INTEGER NOT NULL,username TEXT NOT NULL,normalized TEXT NOT NULL,seen_at REAL NOT NULL,PRIMARY KEY(host_id,user_id))')
            db.execute('CREATE INDEX IF NOT EXISTS activity_names_lookup ON names(normalized)')
            if new_names:
                for row in db.execute('SELECT host_id,payload FROM records'):
                    self._name(db,row['host_id'],json.loads(row['payload']))

    @staticmethod
    def _name(db,host_id,payload):
        if 'username' not in payload:return
        at=payload.get('created_at',payload.get('ended_at',payload.get('occurred_at',payload.get('cutoff',0))))
        db.execute('INSERT INTO names VALUES(?,?,?,?,?) ON CONFLICT(host_id,user_id) DO UPDATE SET username=excluded.username,normalized=excluded.normalized,seen_at=excluded.seen_at WHERE excluded.seen_at>=names.seen_at',
                   (host_id,payload['user_id'],payload['username'],payload['username'].casefold(),at))

    def lookup_user_ids(self,usernames):
        """Indexed names; disabled/revoked sources never resolve identities."""
        if not usernames:return set()
        with self.store.transaction() as auth:
            active={r[0] for r in auth.execute('SELECT id FROM hosts WHERE revoked=0')}
            with connection(self.path) as db:
                enabled={r[0] for r in db.execute('SELECT host_id FROM consents WHERE enabled=1')}&active
                placeholders=','.join('?' for _ in usernames)
                rows=db.execute('SELECT host_id,user_id FROM names WHERE normalized IN ('+placeholders+')',[name.casefold() for name in usernames])
                hidden={r[0] for r in db.execute('SELECT user_id FROM privacy WHERE hidden=1')}
                return {r['user_id'] for r in rows if r['host_id'] in enabled and r['user_id'] not in hidden}


    def consent(self, token, value):
        consent(value);now=self.store.clock()
        require(value['consent_at']<=now+300,'activity_consent_future')
        with self.store.transaction() as auth:
            host,_=self.store._authenticate(auth,verifier(token),'heartbeat')
            with connection(self.path) as db:
                old=db.execute('SELECT * FROM consents WHERE host_id=?',(host['id'],)).fetchone()
                seen=db.execute('SELECT payload_hash FROM consent_history WHERE host_id=? AND consent_id=?',(host['id'],value['consent_id'])).fetchone()
                digest=fingerprint(value)
                if seen:
                    require(seen[0]==digest,'activity_consent_conflict',409)
                    require(old is not None and old['consent_id']==value['consent_id'],'activity_consent_superseded',409)
                else:
                    require(old is None or value['consent_at']>=old['consent_at'],'activity_consent_rollback',409)
                    db.execute('INSERT INTO consent_history VALUES(?,?,?)',(host['id'],value['consent_id'],digest))
                    db.execute('INSERT OR REPLACE INTO consents VALUES(?,?,?,?,?)',(host['id'],value['consent_id'],int(value['enabled']),value['consent_at'],now))
                    if not value['enabled']:
                        # Historical records stay private but disappear immediately from public projections.
                        pass
            self.store._audit(auth,'activity_consent_enabled' if value['enabled'] else 'activity_consent_disabled',host['id'])
        return {'ok':True,'protocol_version':1,'host_id':host['id'],**{k:value[k] for k in ('enabled','consent_id','consent_at')}}

    def ingest(self, token, value):
        batch(value);now=self.store.clock();outcomes=[]
        with self.store.transaction() as auth:
            host,_=self.store._authenticate(auth,verifier(token),'heartbeat')
            with connection(self.path) as db:
                policy=db.execute('SELECT * FROM consents WHERE host_id=?',(host['id'],)).fetchone()
                require(policy is not None and policy['enabled'],'statistics_sharing_disabled',403)
                for item in value['events']:
                    require(all(item[k]==host[k] for k in ('installation_id','generation')),'activity_host_identity_mismatch',403)
                    require(item['consent_id']==policy['consent_id'],'activity_consent_mismatch',409)
                    p=item['payload'];kind=item['kind'];digest=fingerprint(item)
                    previous=db.execute('SELECT host_id,payload_hash FROM events WHERE event_id=?',(item['event_id'],)).fetchone()
                    if previous:
                        require(previous['host_id']==host['id'] and previous['payload_hash']==digest,'activity_event_id_conflict',409)
                        outcomes.append({'event_id':item['event_id'],'phase':'already_accepted'});continue
                    timestamp=p['updated_at'] if kind=='privacy' else p['cutoff'] if kind=='baseline' else p['created_at'] if kind=='achievement' else p['occurred_at'] if kind in ('join','leave') else p['started_at']
                    if kind not in ('privacy','baseline') and not(kind=='achievement' and p['historical']):require(timestamp>=policy['consent_at'],'activity_before_consent',403)
                    require(timestamp<=now+300 and p.get('ended_at',timestamp)<=now+300,'activity_event_future')
                    if kind=='baseline':key='baseline:'+p['source_id']+':'+p['mode']+':'+str(p['user_id'])
                    elif kind=='achievement':key='achievement:'+p['achievement_key']+':'+p['mode']
                    elif kind=='match':key='match:'+p['match_key']+':'+str(p['user_id'])
                    elif kind=='privacy':key='privacy:'+str(p['user_id'])
                    else:key=kind+':'+item['event_id']
                    room=p.get('room_id')
                    db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)',(item['event_id'],host['id'],item['consent_id'],kind,p['user_id'],room,key,digest,canonical(p).decode(),now))
                    if kind=='privacy':
                        prior=db.execute('SELECT updated_at FROM privacy WHERE host_id=? AND user_id=?',(host['id'],p['user_id'])).fetchone()
                        if prior is None or p['updated_at']>=prior[0]:
                            db.execute('INSERT OR REPLACE INTO privacy VALUES(?,?,?,?)',(host['id'],p['user_id'],int(p['hidden']),p['updated_at']))
                    elif kind in ('baseline','achievement'):
                        prior=db.execute('SELECT payload FROM records WHERE host_id=? AND source_key=?',(host['id'],key)).fetchone()
                        replace=True
                        if prior:
                            old=json.loads(prior[0])
                            if kind=='achievement':
                                require(all(old[k]==p[k] for k in ('room_id','user_id','beatmap_id','playlist_item_id','profile_gain_pp','score_pp','created_at')),'activity_achievement_conflict',409)
                            else:replace=p['cutoff']>=old['cutoff']
                        if replace:
                            db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?,?)',(host['id'],key,item['event_id'],canonical(p).decode(),kind,p['user_id'],room))
                    elif kind=='match':
                        # A delayed older participant snapshot cannot erase a server-confirmed score.
                        prior=db.execute('SELECT payload FROM records WHERE host_id=? AND source_key=?',(host['id'],key)).fetchone()
                        merged=dict(p)
                        if prior:
                            old=json.loads(prior[0]);require(all(old[k]==p[k] for k in ('room_id','user_id','beatmap_id','started_at','ended_at','mode')),'activity_match_conflict',409)
                            if old['score_submitted'] and not p['score_submitted']:merged=old
                        db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?,?)',(host['id'],key,item['event_id'],canonical(merged).decode(),kind,p['user_id'],room))
                    else:
                        db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?)',(host['id'],key,item['event_id'],canonical(p).decode(),kind,p['user_id'],room))
                    self._name(db,host['id'],p)
                    outcomes.append({'event_id':item['event_id'],'phase':'accepted'})
            self.store._audit(auth,'activity_batch',host['id'])
        return {'ok':True,'protocol_version':1,'outcomes':outcomes,'received_at':now}

    def query(self, token, value):
        from .query import query
        return query(self,token,value)

    def snapshot(self, *, now=None, hidden_user_ids=(), include_hidden_for_totals=False, user_ids=None, detached_authority=False):
        """Complete private intermediate export, not a public HTTP endpoint.

        No leaderboard cap. Host-provided evidence is authenticated as its source,
        not independently re-fetched from osu!. Public consumers must HTML escape
        usernames/labels and never publish hidden_user_ids themselves.
        """
        require(type(include_hidden_for_totals) is bool,'invalid_activity_privacy_projection')
        require(user_ids is None or (type(user_ids) in (list,tuple,set) and len(user_ids)<=30 and all(type(uid) is int and uid>0 for uid in user_ids)),'activity_query_too_large')
        now=self.store.clock() if now is None else now
        from contextlib import ExitStack
        require(type(detached_authority) is bool,'invalid_activity_privacy_projection')
        with ExitStack() as authority:
            auth=authority.enter_context(self.store.transaction())
            hosts={r['id']:dict(r) for r in auth.execute('SELECT id,label,revoked,account_id FROM hosts')}
            if detached_authority:authority.close()
            with connection(self.path) as db:
                hidden=set(int(x) for x in hidden_user_ids)
                # Conservative global preference: a host cannot unhide another host's player choice.
                hidden.update(r[0] for r in db.execute('SELECT user_id FROM privacy WHERE hidden=1'))
                consents={r['host_id']:dict(r) for r in db.execute('SELECT * FROM consents')}
                result={'format':1,'generated_at':now,'hosts':[],'presence':[],'joins':[],'leaves':[],'matches':[],'achievements':[],'baselines':[],
                        'hidden_user_ids':sorted(hidden),'coverage':{'historical_backfill':False,'private_rooms':True,'pp_attribution':False,'tracked_pp_supported':True,'source':'native_activity_ledger'}}
                for hid,host in hosts.items():
                    policy=consents.get(hid);latest=db.execute('SELECT MAX(received) FROM events WHERE host_id=?',(hid,)).fetchone()[0]
                    result['hosts'].append({'host_id':hid,'label':host['label'],'account_id':host['account_id'],
                        'enabled':bool(policy and policy['enabled'] and not host['revoked']),
                        'consent_at':policy['consent_at'] if policy else None,'updated_at':latest,'revoked':bool(host['revoked'])})
                active={r['host_id'] for r in result['hosts'] if r['enabled']}
                if user_ids is None:
                    records=db.execute('SELECT * FROM records ORDER BY rowid')
                elif not user_ids:
                    records=()
                else:
                    records=db.execute('SELECT * FROM records WHERE user_id IN ('+','.join('?' for _ in user_ids)+') ORDER BY rowid',list(user_ids))
                for row in records:
                    if row['host_id'] not in active or (row['user_id'] in hidden and not include_hidden_for_totals):continue
                    item={**json.loads(row['payload']),'host_id':row['host_id'],'event_id':row['event_id']}
                    result[{'presence':'presence','join':'joins','leave':'leaves','match':'matches','achievement':'achievements','baseline':'baselines'}[row['kind']]].append(item)
        return result
