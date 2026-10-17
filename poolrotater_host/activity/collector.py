"""Native evidence -> durable opt-in outbox. No network/provider calls on gameplay paths."""
import json
from pathlib import Path
import sqlite3
import time
import uuid
from urllib.parse import quote
from ..layout import load
from ..network.contracts import require, canonical, fingerprint, identifier, fields
from .storage import connection
from .contracts import event, batch, MAX_BODY, MAX_EVENTS

MAX_OUTBOX_EVENTS = 50000
MAX_PRIVACY_EVENTS = 50000


class Collector:
    def __init__(self, root, ledger_path=None, *, clock=time.time, marker=None, privacy_path=None):
        self.root=Path(root);self.marker=marker or load(root)[0];self.clock=clock
        self.ledger_path=Path(ledger_path or self.root/'state/controller-ratings.db')
        self.privacy_path=Path(privacy_path or self.ledger_path)
        self.path=self.root/'state/activity-outbox.sqlite3'
        for key in ('installation_id','generation'):identifier(self.marker[key])
        with connection(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL,kind TEXT NOT NULL,user_id INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS privacy_outbox(user_id INTEGER PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS activity_outbox_kind ON outbox(kind)')
            # Upgrade earlier candidates without leaving privacy changes behind
            # an ordinary full queue. Newest pending preference replaces older.
            for pending in db.execute("SELECT user_id,event_id,payload FROM outbox WHERE kind='privacy' ORDER BY rowid"):
                db.execute('INSERT OR REPLACE INTO privacy_outbox VALUES(?,?,?)',tuple(pending))
            db.execute("DELETE FROM outbox WHERE kind='privacy'")
            db.execute('CREATE TABLE IF NOT EXISTS seen(source_key TEXT PRIMARY KEY,digest TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS history_baselines(consent_id TEXT NOT NULL,source_key TEXT NOT NULL,payload TEXT NOT NULL,user_id INTEGER NOT NULL,queued INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(consent_id,source_key))')
            identity={k:self.marker[k] for k in ('installation_id','generation')}
            old=db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
            require(old is None or json.loads(old[0])==identity,'foreign_activity_stream')
            db.execute("INSERT OR IGNORE INTO meta VALUES('identity',?)",(canonical(identity).decode(),))

    def configure(self, *, share_statistics):
        require(type(share_statistics) is bool,'explicit_statistics_consent_required')
        with connection(self.path) as db:
            old=self._profile(db)
            if old and old['enabled']==share_statistics:return old
            value={'protocol_version':1,'enabled':share_statistics,'consent_id':str(uuid.uuid4()),'consent_at':self.clock()}
            db.execute("INSERT OR REPLACE INTO meta VALUES('consent',?)",(canonical(value).decode(),))
            # An opt-out is effective locally immediately; old epochs cannot replay after re-enabling.
            db.execute('DELETE FROM outbox');db.execute('DELETE FROM privacy_outbox');db.execute('DELETE FROM seen');db.execute('DELETE FROM history_baselines')
            db.execute("DELETE FROM meta WHERE key LIKE 'cursor_%' OR key LIKE 'anchor_%'")
            return value

    def _profile(self, db):
        row=db.execute("SELECT value FROM meta WHERE key='consent'").fetchone()
        return json.loads(row[0]) if row else None

    def consent(self):
        with connection(self.path) as db:return self._profile(db)

    def _enqueue(self, db, profile, key, kind, payload):
        digest=fingerprint(payload)
        previous=db.execute('SELECT digest FROM seen WHERE source_key=?',(key,)).fetchone()
        if previous and previous[0]==digest:return False
        if kind=='privacy':
            existing=db.execute('SELECT 1 FROM privacy_outbox WHERE user_id=?',(payload['user_id'],)).fetchone()
            if not existing and db.execute('SELECT COUNT(*) FROM privacy_outbox').fetchone()[0]>=MAX_PRIVACY_EVENTS:return None
        elif db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]>=MAX_OUTBOX_EVENTS:return None
        eid=str(uuid.uuid5(uuid.UUID(self.marker['generation']),profile['consent_id']+key+digest))
        value={'event_id':eid,**{k:self.marker[k] for k in ('installation_id','generation')},
               'consent_id':profile['consent_id'],'kind':kind,'payload':payload}
        event(value)
        if kind=='privacy':
            db.execute('INSERT OR REPLACE INTO privacy_outbox VALUES(?,?,?)',(payload['user_id'],eid,canonical(value).decode()))
        else:
            db.execute('INSERT OR IGNORE INTO outbox VALUES(?,?,?,?)',(eid,canonical(value).decode(),kind,payload['user_id']))
        db.execute('INSERT OR REPLACE INTO seen VALUES(?,?)',(key,digest));return True

    @staticmethod
    def _source(path):
        require(path.is_file() and not path.is_symlink(),'activity_ledger_required')
        db=sqlite3.connect('file:'+quote(str(path.absolute()))+'?mode=ro',uri=True,timeout=.2)
        db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        return db

    @staticmethod
    def _table(db,name):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())

    @staticmethod
    def _public_ranges(source,room,start,end):
        if not Collector._table(source,'activity_visibility'):return []
        rows=source.execute('SELECT public,observed_at,mode FROM activity_visibility WHERE room_id=? AND observed_at<=? ORDER BY observed_at,id',(room,end)).fetchall()
        if start==end:
            return [(start,end,rows[-1]['mode'])] if rows and rows[-1]['public'] else []
        result=[]
        for i,row in enumerate(rows):
            finish=min(end,rows[i+1]['observed_at'] if i+1<len(rows) else end)
            begin=max(start,row['observed_at'])
            if row['public'] and finish>begin:result.append((begin,finish,row['mode']))
        return result

    def collect(self, *, limit=500):
        require(type(limit) is int and 1<=limit<=5000,'invalid_activity_limit')
        added=0;capture_paused=False;privacy_safe=True
        with connection(self.path) as target:
            profile=self._profile(target)
            if not profile or not profile['enabled']:return {'ok':True,'capture_enabled':False,'collected':0}
            since=profile['consent_at']
            privacy=self._source(self.privacy_path)
            try:
                hidden=set()
                if self._table(privacy,'autohost_stats_privacy'):
                    for row in privacy.execute('SELECT user_id,hidden,updated_at FROM autohost_stats_privacy'):
                        payload={'user_id':int(row['user_id']),'hidden':bool(row['hidden']),'updated_at':float(row['updated_at'])}
                        if payload['hidden']:
                            hidden.add(payload['user_id']);target.execute("DELETE FROM outbox WHERE user_id=? AND kind!='privacy'",(payload['user_id'],))
                        enqueued=self._enqueue(target,profile,'privacy:'+str(row['user_id']),'privacy',payload)
                        if enqueued is None:privacy_safe=False;capture_paused=True
                        else:added+=enqueued
            finally:privacy.close()
            target.execute("INSERT OR REPLACE INTO meta VALUES('privacy_safe',?)",('true' if privacy_safe else 'false',))
            if not privacy_safe:
                return {'ok':True,'capture_enabled':True,'collected':added,'capture_paused':True,'privacy_safe':False}
            source=self._source(self.ledger_path)
            try:
                require(all(self._table(source,t) for t in ('activity_visibility','activity_presence','activity_joins','activity_leaves')),'activity_ledger_migration_required')
                for table,kind in (('activity_presence','presence'),('activity_joins','join'),('activity_leaves','leave')):
                    saved=target.execute('SELECT value FROM meta WHERE key=?',('cursor_'+table,)).fetchone();cursor=int(saved[0]) if saved else 0
                    high=source.execute('SELECT COALESCE(MAX(id),0) FROM '+table).fetchone()[0]
                    require(high>=cursor,'activity_source_restore_requires_review')
                    anchor=target.execute('SELECT value FROM meta WHERE key=?',('anchor_'+table,)).fetchone()
                    if cursor and anchor:
                        prior=source.execute('SELECT * FROM '+table+' WHERE id=?',(cursor,)).fetchone()
                        require(prior is not None and fingerprint(dict(prior))==anchor[0],'activity_source_restore_requires_review')
                    for row in source.execute('SELECT * FROM '+table+' WHERE id>? ORDER BY id LIMIT ?',(cursor,limit)):
                        uid=int(row['user_id']);start=float(row['started_at'] if kind=='presence' else row['occurred_at']);end=float(row['ended_at'] if kind=='presence' else row['occurred_at'])
                        if end<since or uid in hidden:
                            cursor=row['id'];continue
                        for begin,finish,mode in self._public_ranges(source,row['room_id'],max(start,since),end):
                            p={'room_id':int(row['room_id']),'mode':mode,'user_id':uid,'username':row['username']}
                            p.update({'started_at':begin,'ended_at':finish} if kind=='presence' else {'occurred_at':finish})
                            enqueued=self._enqueue(target,profile,kind+':'+str(row['id'])+':'+str(begin),kind,p)
                            if enqueued is None:capture_paused=True;break
                            added+=enqueued
                        if capture_paused:break
                        cursor=row['id']
                    target.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('cursor_'+table,str(cursor)))
                    if cursor:
                        last=source.execute('SELECT * FROM '+table+' WHERE id=?',(cursor,)).fetchone()
                        target.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('anchor_'+table,fingerprint(dict(last))))
                if self._table(source,'autohost_matches') and self._table(source,'autohost_match_players'):
                    # Revisit mutable completed rows: scores can arrive after MatchEnded. No timestamp watermark loses them.
                    saved=target.execute("SELECT value FROM meta WHERE key='cursor_matches'").fetchone()
                    match_cursor=int(saved[0]) if saved else 0
                    rows=source.execute("SELECT p.rowid AS source_row,m.*,p.user_id,p.username,p.score_submitted,p.accuracy FROM autohost_matches m JOIN autohost_match_players p ON p.match_key=m.match_key WHERE m.result='completed' AND m.ended_at IS NOT NULL AND m.started_at>=? AND p.rowid>? ORDER BY p.rowid LIMIT ?",(since,match_cursor,limit)).fetchall()
                    if not rows:match_cursor=0
                    for row in rows:
                        if row['user_id'] in hidden:
                            match_cursor=row['source_row'];continue
                        ranges=self._public_ranges(source,row['room_id'],row['started_at'],row['ended_at'])
                        if len(ranges)!=1 or ranges[0][:2]!=(row['started_at'],row['ended_at']):
                            match_cursor=row['source_row'];continue
                        p={k:row[k] for k in ('room_id','user_id','username','match_key','beatmap_id','started_at','ended_at')}
                        p.update(mode=ranges[0][2],score_submitted=bool(row['score_submitted']),accuracy=row['accuracy'] if row['score_submitted'] else None)
                        enqueued=self._enqueue(target,profile,'match:'+row['match_key']+':'+str(row['user_id']),'match',p)
                        if enqueued is None:capture_paused=True;break
                        added+=enqueued;match_cursor=row['source_row']
                    target.execute("INSERT OR REPLACE INTO meta VALUES('cursor_matches',?)",(str(match_cursor),))
                added+=self._achievements(source,target,profile,hidden,limit=limit)
                capture_paused=capture_paused or target.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]>=MAX_OUTBOX_EVENTS
                target.execute("INSERT OR REPLACE INTO meta VALUES('last_collected',?)",(str(self.clock()),))
            finally:source.close()
        return {'ok':True,'capture_enabled':True,'collected':added,'capture_paused':capture_paused,'privacy_safe':privacy_safe}

    def _achievements(self,source,target,profile,hidden,*,limit=500,reviewed_rooms=None):
        added=0;reviewed=set(reviewed_rooms or ())
        for table,fallback_mode in (('autohost_pp_achievements','standard'),('autohost_pp_achievements_mania','mania')):
            if not self._table(source,table):continue
            cursor_key='cursor_'+table+('_history' if reviewed_rooms is not None else '')
            saved=target.execute('SELECT value FROM meta WHERE key=?',(cursor_key,)).fetchone()
            cursor=int(saved[0]) if saved else 0
            query='SELECT rowid AS source_row,* FROM '+table+' WHERE rowid>?'
            args=[cursor]
            if reviewed_rooms is None:
                query+=' AND created_at>=?';args.append(profile['consent_at'])
            query+=' ORDER BY rowid LIMIT ?';args.append(limit)
            rows=source.execute(query,args).fetchall()
            if not rows and reviewed_rooms is None:cursor=0
            for row in rows:
                uid=int(row['user_id']);room=row['room_id'];historical=reviewed_rooms is not None
                if uid in hidden or room is None or (historical and room not in reviewed):
                    cursor=row['source_row'];continue
                ranges=self._public_ranges(source,room,row['created_at'],row['created_at'])
                if not historical and not ranges:
                    cursor=row['source_row'];continue
                mode=ranges[0][2] if ranges else fallback_mode
                p={k:row[k] for k in ('room_id','user_id','username','achievement_key','beatmap_id','playlist_item_id','created_at','profile_gain_pp','score_pp','accuracy','top_rank')}
                p.update(mode=mode,historical=historical)
                enqueued=self._enqueue(target,profile,'achievement:'+p['achievement_key']+':'+mode,'achievement',p)
                if enqueued is None:break
                added+=enqueued;cursor=row['source_row']
            target.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(cursor_key,str(cursor)))
        return added

    def import_history(self, *, public_room_ids, public_history_confirmed=False,
                       include_lobby_totals=False, exclude_unreviewed_players=False, limit=500):
        """Explicit reviewed backfill; old ledgers have no reliable private-room witness.

        Operator supplies reviewed public room IDs. Cumulative lobby totals need
        an additional confirmation because they predate room-level provenance.
        Complete repeated calls scan bounded chunks; matching snapshots replace,
        never increment, central baselines.
        """
        require(type(public_room_ids) in (list,tuple) and all(type(r) is int and r>0 for r in public_room_ids),'invalid_activity_history_rooms')
        require(type(public_history_confirmed) is bool and type(include_lobby_totals) is bool,'explicit_activity_history_review_required')
        require(type(exclude_unreviewed_players) is bool,'explicit_activity_history_review_required')
        require(type(limit) is int and 1<=limit<=5000,'invalid_activity_limit')
        require(public_history_confirmed,'explicit_activity_history_review_required')
        require(not include_lobby_totals or bool(public_room_ids),'activity_history_room_provenance_required')
        history_paused=False
        with connection(self.path) as target:
            profile=self._profile(target);require(profile and profile['enabled'],'statistics_sharing_disabled')
            source=self._source(self.ledger_path)
            try:
                hidden={r[0] for r in source.execute('SELECT user_id FROM autohost_stats_privacy WHERE hidden=1')} if self._table(source,'autohost_stats_privacy') else set()
                # A frozen legacy source must still honor today's opt-outs.
                if self.privacy_path!=self.ledger_path:
                    privacy=self._source(self.privacy_path)
                    try:
                        if self._table(privacy,'autohost_stats_privacy'):
                            hidden.update(r[0] for r in privacy.execute('SELECT user_id FROM autohost_stats_privacy WHERE hidden=1'))
                    finally:privacy.close()
                if include_lobby_totals:
                    # Cumulative legacy time cannot be separated by room. It is
                    # only admissible if the reviewed public manifest covers
                    # every room recorded in that source, and no contradictory
                    # authoritative private witness exists.
                    known={r[0] for r in source.execute('SELECT room_id FROM autohost_rooms')}
                    known.update(r[0] for r in source.execute('SELECT DISTINCT room_id FROM autohost_room_players'))
                    reviewed=set(public_room_ids)
                    excluded=set()
                    if exclude_unreviewed_players:
                        # Scalar cumulative totals cannot be split by room.
                        # Withhold the entire player's baseline if any known
                        # participation lacks reviewed public provenance.
                        participants={}
                        for uid,room in source.execute('SELECT user_id,room_id FROM autohost_room_players'):
                            participants.setdefault(uid,set()).add(room)
                        excluded={uid for uid,rooms in participants.items() if not rooms.issubset(reviewed)}
                    else:
                        require(known.issubset(reviewed),'activity_history_incomplete_public_provenance')
                    private={r[0] for r in source.execute('SELECT DISTINCT room_id FROM activity_visibility WHERE public=0')} if self._table(source,'activity_visibility') else set()
                    # Initial random-password admission precedes any players;
                    # only private witnesses overlapping actual presence are
                    # contradictory to a reviewed all-public history.
                    for room in private:
                        for interval in source.execute('SELECT started_at,ended_at FROM activity_presence WHERE room_id=?',(room,)):
                            ranges=self._public_ranges(source,room,interval[0],interval[1])
                            if sum(end-start for start,end,_ in ranges)<interval[1]-interval[0]-.001:
                                require(exclude_unreviewed_players and room not in reviewed,'activity_history_private_time_refused')
                rooms_key='history_achievement_rooms_'+profile['consent_id']
                rooms_signature=canonical(sorted(set(public_room_ids))).decode()
                prior_rooms=target.execute('SELECT value FROM meta WHERE key=?',(rooms_key,)).fetchone()
                if prior_rooms is None or prior_rooms[0]!=rooms_signature:
                    target.execute("DELETE FROM meta WHERE key IN ('cursor_autohost_pp_achievements_history','cursor_autohost_pp_achievements_mania_history')")
                    target.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(rooms_key,rooms_signature))
                added=self._achievements(source,target,profile,hidden,limit=limit,reviewed_rooms=public_room_ids)
                if include_lobby_totals:
                    row=target.execute("SELECT value FROM meta WHERE key='history_source_id'").fetchone()
                    source_id=row[0] if row else str(uuid.uuid4())
                    target.execute("INSERT OR IGNORE INTO meta VALUES('history_source_id',?)",(source_id,))
                    # Per-player cutoff is the last persisted native time checkpoint.
                    # Do not manufacture elapsed time from possibly stale active joins.
                    # Freeze once per consent epoch. Repeating import never slides
                    # the cutoff or recaptures the same cumulative hours.
                    cutoff_row=target.execute("SELECT value FROM meta WHERE key='history_cutoff_"+profile['consent_id']+"'").fetchone()
                    cutoff=float(cutoff_row[0]) if cutoff_row else self.clock()
                    if not cutoff_row:
                        target.execute("INSERT INTO meta VALUES(?,?)",('history_cutoff_'+profile['consent_id'],str(cutoff)))
                        if exclude_unreviewed_players:
                            target.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('history_public_review_'+profile['consent_id'],canonical(sorted(reviewed)).decode()))
                        for table,mode in (('autohost_lobby_time','standard'),('autohost_lobby_time_mania','mania')):
                            if not self._table(source,table):continue
                            for row in source.execute('SELECT * FROM '+table):
                                if row['user_id'] in hidden:continue
                                if exclude_unreviewed_players and (row['user_id'] in excluded or row['user_id'] not in participants):continue
                                p={'source_id':source_id,'mode':mode,'cutoff':min(cutoff,float(row['last_leave_at'] or row['first_join_at'] or cutoff)),'user_id':int(row['user_id']),'username':str(row['username'] or row['user_id']),
                                   'total_seconds':max(0,float(row['total_seconds'])),'visit_count':max(0,int(row['visit_count'])),
                                   **{k:row[k] for k in ('first_join_at','last_join_at','last_leave_at')}}
                                key='baseline:'+mode+':'+str(row['user_id'])
                                target.execute('INSERT INTO history_baselines(consent_id,source_key,payload,user_id) VALUES(?,?,?,?)',
                                               (profile['consent_id'],key,canonical(p).decode(),row['user_id']))
                    elif exclude_unreviewed_players:
                        prior=target.execute('SELECT value FROM meta WHERE key=?',('history_public_review_'+profile['consent_id'],)).fetchone()
                        require(prior is not None and json.loads(prior[0])==sorted(reviewed),'activity_history_frozen_review_mismatch')
                    # Frozen whole-source summaries are queued in bounded chunks.
                    # A full ordinary queue cannot roll back the frozen source or
                    # require importing more than the queue can ever contain.
                    for pending in target.execute('SELECT source_key,payload,user_id FROM history_baselines WHERE consent_id=? AND queued=0 ORDER BY rowid LIMIT ?',
                                                  (profile['consent_id'],limit)).fetchall():
                        if pending['user_id'] in hidden:
                            target.execute('UPDATE history_baselines SET queued=1 WHERE consent_id=? AND source_key=?',(profile['consent_id'],pending['source_key']));continue
                        enqueued=self._enqueue(target,profile,pending['source_key'],'baseline',json.loads(pending['payload']))
                        if enqueued is None:history_paused=True;break
                        added+=enqueued
                        target.execute('UPDATE history_baselines SET queued=1 WHERE consent_id=? AND source_key=?',(profile['consent_id'],pending['source_key']))
                complete=True
                for table in ('autohost_pp_achievements','autohost_pp_achievements_mania'):
                    if not self._table(source,table):continue
                    maximum=source.execute('SELECT COALESCE(MAX(rowid),0) FROM '+table).fetchone()[0]
                    cursor=target.execute('SELECT value FROM meta WHERE key=?',('cursor_'+table+'_history',)).fetchone()
                    complete=complete and (maximum==0 or bool(cursor and int(cursor[0])>=maximum))
                if include_lobby_totals:
                    complete=complete and not target.execute('SELECT 1 FROM history_baselines WHERE consent_id=? AND queued=0 LIMIT 1',(profile['consent_id'],)).fetchone()
                history_paused=history_paused or (not complete and target.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]>=MAX_OUTBOX_EVENTS)
            finally:source.close()
        return {'ok':True,'historical_events_queued':added,'public_history_explicitly_reviewed':True,
                'history_capture_paused':history_paused,'history_scan_complete':bool(complete),'provider_dispatch':False}

    def batch(self, *, limit=MAX_EVENTS):
        require(type(limit) is int and 1<=limit<=MAX_EVENTS,'invalid_activity_limit')
        value={'protocol_version':1,'events':[]}
        with connection(self.path) as db:
            profile=self._profile(db)
            if profile and profile['enabled']:
                sources=[db.execute('SELECT payload FROM privacy_outbox ORDER BY rowid LIMIT ?',(limit,))]
                safe=db.execute("SELECT value FROM meta WHERE key='privacy_safe'").fetchone()
                if safe is None or safe[0]=='true':sources.append(db.execute('SELECT payload FROM outbox ORDER BY rowid LIMIT ?',(limit,)))
                for rows in sources:
                    for row in rows:
                        if len(value['events'])>=limit:break
                        item=json.loads(row[0]);value['events'].append(item)
                        if len(canonical(value))>MAX_BODY:value['events'].pop();break
        return batch(value)

    def acknowledge(self, reply):
        fields(reply,('ok','protocol_version','outcomes','received_at'))
        require(reply['ok'] is True and reply['protocol_version']==1 and type(reply['outcomes']) is list,'invalid_activity_receipt')
        with connection(self.path) as db:
            for outcome in reply['outcomes']:
                fields(outcome,('event_id','phase'));identifier(outcome['event_id'])
                require(outcome['phase'] in ('accepted','already_accepted','rejected'),'invalid_activity_receipt')
                # Rejected records remain for review; they are not silently discarded.
                if outcome['phase'] in ('accepted','already_accepted'):
                    db.execute('DELETE FROM outbox WHERE event_id=?',(outcome['event_id'],))
                    db.execute('DELETE FROM privacy_outbox WHERE event_id=?',(outcome['event_id'],))
        return self.status()

    def status(self):
        with connection(self.path) as db:
            profile=self._profile(db);row=db.execute("SELECT value FROM meta WHERE key='last_collected'").fetchone()
            return {'ok':True,'capture_enabled':bool(profile and profile['enabled']),
                    'consent_id':profile['consent_id'] if profile else None,'consent_at':profile['consent_at'] if profile else None,
                    'pending_events':db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]+db.execute('SELECT COUNT(*) FROM privacy_outbox').fetchone()[0],
                    'pending_privacy_events':db.execute('SELECT COUNT(*) FROM privacy_outbox').fetchone()[0],
                    'last_collected_at':float(row[0]) if row else None,'provider_dispatch':False}
