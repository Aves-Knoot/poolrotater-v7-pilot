"""Complete local player totals; additive protocol, isolated background downloads.

Raw event/audit databases never leave the hub. Immutable pages contain aggregate
public totals only. A separate short policy lease bounds stale privacy exposure;
ordinary statistic freshness is twenty minutes, not the old two-minute cache.
"""
from collections import defaultdict
from contextlib import contextmanager
import sqlite3
from urllib.parse import quote
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from ..layout import load, read_json, atomic_json, file_lease
from ..network.contracts import (canonical, fingerprint, fields, require, integer,
                                 number, identifier, verifier, NetworkError)
from ..ratings.migration import _private
from .storage import connection
from .query import player

SCHEME='player-totals-v1'
TAIKO_SCHEME='player-totals-v3'
INTERVAL=1200
POLICY_INTERVAL=30
POLICY_LEASE=120
MAX_AGE=86400
MAX_PAGE=512*1024
PAGE_ROWS=250
MAX_ROWS=200000
MAX_PAGES=(MAX_ROWS+PAGE_ROWS-1)//PAGE_ROWS
COLLECTIONS=('presence','joins','leaves','matches','achievements','baselines')
TOTALS=('total_gain','achievement_count','best_gain','total_lobby_seconds','visit_count','map_plays')


def digest(value):
    require(type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value),'invalid_statistics_replica')
    return value


def manifest(value):
    fields(value,('ok','format','scheme','server_epoch','host_id','policy_id','watermark','generated_at','rows','pages','content_id'))
    require(value['ok'] is True and type(value['format']) is int and value['format']==1
            and (value['scheme']==SCHEME or value['scheme']==TAIKO_SCHEME),'incompatible_statistics_replica')
    identifier(value['server_epoch']);identifier(value['host_id']);digest(value['policy_id'])
    integer(value['watermark'],0);number(value['generated_at']);integer(value['rows'],0,MAX_ROWS)
    require(type(value['pages']) is list and len(value['pages'])<=MAX_PAGES,'invalid_statistics_replica')
    total=0
    for i,item in enumerate(value['pages']):
        fields(item,('index','sha256','bytes','rows'));require(item['index']==i and type(item['index']) is int,'invalid_statistics_replica')
        digest(item['sha256']);integer(item['bytes'],1,MAX_PAGE);integer(item['rows'],1,PAGE_ROWS);total+=item['rows']
    require(total==value['rows'] and fingerprint({k:v for k,v in value.items() if k!='content_id'})==digest(value['content_id']),'invalid_statistics_replica')
    return value


def validate_player(item):
    # Reuse the legacy reader's precise public-row contract in both protocols.
    from .reader import validate_player as check
    check(item);require(item['hidden'] is False,'invalid_statistics_replica')
    return item


def page(value,entry):
    fields(value,('ok','format','index','rows'))
    require(value['ok'] is True and type(value['format']) is int and value['format']==1 and type(value['index']) is int
            and value['index']==entry['index'] and type(value['rows']) is list and len(value['rows'])==entry['rows']
            and len(canonical(value))==entry['bytes'] and fingerprint(value)==entry['sha256'],'invalid_statistics_replica')
    previous=(-1,0)
    for row in value['rows']:
        fields(row,('ruleset_id','player'));require(type(row['ruleset_id']) is int and row['ruleset_id'] in (0,1,2,3),'invalid_statistics_replica')
        validate_player(row['player']);key=(row['ruleset_id'],row['player']['user_id'])
        require(key>previous,'invalid_statistics_replica');previous=key
    return value


def directory(path):
    path.mkdir(mode=0o700,exist_ok=True);return _private(path,directory=True)


class Publisher:
    """One process-wide source projection, shared by authenticated consumers."""
    def __init__(self,repository,*,background=False):
        self.repository=repository;self.store=repository.store;self.lock=threading.Lock()
        self.projection=None;self.stamp=None;self.include_catch=False;self.include_taiko=False
        self.background=background
        self._schedule_lock=threading.Lock();self._pending={};self._failed_at={}
        self._worker=None;self._closed=False

    def close(self):
        with self._schedule_lock:
            self._closed=True;self._pending.clear();worker=self._worker
        if worker is not None:worker.join()

    @staticmethod
    def _scope_dir(*,include_catch=False,include_taiko=False):
        return 'activity-replicas-v3' if include_taiko else 'activity-replicas-v2' if include_catch else 'activity-replicas'

    def _cached(self,host,epoch,policy,*,include_catch=False,include_taiko=False):
        base=self.store.root/self._scope_dir(include_catch=include_catch,include_taiko=include_taiko)/host
        if not base.exists():return None
        _private(base,directory=True);pointer=base/'current.json'
        if not pointer.exists():return None
        value=manifest(read_json(_private(pointer)))
        require(value['scheme']==(TAIKO_SCHEME if include_taiko else SCHEME),
                'incompatible_statistics_replica')
        age=self.store.clock()-value['generated_at']
        if (value['host_id']!=host or value['server_epoch']!=epoch
                or value['policy_id']!=policy or not 0<=age<MAX_AGE):return None
        return value

    def _schedule(self,token,host,include_catch,include_taiko):
        key=(host,include_catch,include_taiko)
        with self._schedule_lock:
            if (self._closed or key in self._pending or len(self._pending)>=64
                    or self.store.clock()-self._failed_at.get(key,float('-inf'))<15):return
            self._pending[key]=token
            if self._worker is None:
                self._worker=threading.Thread(target=self._refresh,name='statistics-projection',daemon=True)
                self._worker.start()

    def _refresh(self):
        while True:
            with self._schedule_lock:
                if self._closed or not self._pending:
                    self._worker=None;return
                key=next(iter(self._pending));token=self._pending[key]
            try:self._current(token,include_catch=key[1],include_taiko=key[2])
            except Exception:
                import logging
                logging.getLogger(__name__).warning('statistics projection refresh deferred; retained previous verified generation')
                with self._schedule_lock:self._failed_at[key]=self.store.clock()
            finally:
                with self._schedule_lock:self._pending.pop(key,None)

    def current(self,token,*,include_catch=False,include_taiko=False):
        include_catch=include_catch or include_taiko
        host,epoch,policy,_=self.authority(token)
        old=self._cached(host,epoch,policy,include_catch=include_catch,include_taiko=include_taiko)
        if (old is not None and self.store.clock()-old['generated_at']<INTERVAL
                and (self.background or self.stamp is not None)):
            return old
        if not self.background:return self._current(token,include_catch=include_catch,include_taiko=include_taiko)
        self._schedule(token,host,include_catch,include_taiko)
        if old is not None:return old
        raise NetworkError('hub_unavailable',503)

    def authority(self,token):
        with self.store.transaction() as auth:
            host,_=self.store._authenticate(auth,verifier(token),'heartbeat')
            epoch=auth.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
            hosts=[tuple(r) for r in auth.execute('SELECT id,revoked FROM hosts ORDER BY id')]
            with connection(self.repository.path) as db:
                policy=fingerprint({'epoch':epoch,'hosts':hosts,
                    'privacy':[tuple(r) for r in db.execute('SELECT host_id,user_id,hidden,updated_at FROM privacy ORDER BY host_id,user_id')],
                    'consents':[tuple(r) for r in db.execute('SELECT host_id,consent_id,enabled FROM consents ORDER BY host_id')]})
                watermark=db.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]
        return host['id'],epoch,policy,watermark

    def _project(self,epoch,policy,watermark,now,*,include_catch=False,include_taiko=False):
        include_catch=include_catch or include_taiko
        snapshot=self.repository.snapshot(now=now,detached_authority=True)
        groups={}
        for kind in COLLECTIONS:
            for row in snapshot[kind]:
                uid=row['user_id']
                if uid not in groups:groups[uid]={key:[] for key in COLLECTIONS}
                groups[uid][kind].append(row)
        rows=[]
        require(len(groups)*(4 if include_taiko else 3 if include_catch else 2)<=MAX_ROWS,'statistics_replica_capacity_required',503)
        for uid in sorted(groups):
            group={**snapshot,**groups[uid]}
            # Cross-mode name resolution must work even when one mode has zero totals.
            identity=max((r for key in COLLECTIONS for r in groups[uid][key]),
                         key=lambda r:r.get('created_at',r.get('ended_at',r.get('occurred_at',r.get('cutoff',0)))))['username']
            host_groups=defaultdict(lambda:{key:[] for key in COLLECTIONS})
            for key in COLLECTIONS:
                for r in groups[uid][key]:host_groups[r['host_id']][key].append(r)
            for mode in ((0,1,2,3) if include_taiko else (0,2,3) if include_catch else (0,3)):
                result=player(group,uid,ruleset_id=mode)
                # Hidden IDs are not included in snapshot records at all.
                require(not result['hidden'],'invalid_statistics_replica')
                result['username']=identity
                own={}
                for host,collections in host_groups.items():
                    totals=player({**group,**collections},uid,ruleset_id=mode)
                    own[host]={key:totals[key] for key in TOTALS}
                rows.append({'ruleset_id':mode,'player':result,'own':own})
        rows.sort(key=lambda r:(r['ruleset_id'],r['player']['user_id']))
        self.projection=rows;self.stamp=(epoch,policy,watermark,now);self.include_catch=include_catch;self.include_taiko=include_taiko

    def _current(self,token,*,include_catch=False,include_taiko=False):
        with self.lock:
            host,epoch,policy,watermark=self.authority(token);now=self.store.clock()
            if (self.include_catch!=include_catch or self.include_taiko!=include_taiko or self.stamp is None
                    or self.stamp[:2]!=(epoch,policy) or not 0<=now-self.stamp[3]<INTERVAL):
                self._project(epoch,policy,watermark,now,include_catch=include_catch,include_taiko=include_taiko)
                # A hide/revoke/consent change during projection must never publish stale policy.
                check=self.authority(token)
                require(check[:3]==(host,epoch,policy),'statistics_replica_policy_changed',409)
            base=directory(self.store.root/self._scope_dir(include_catch=include_catch,include_taiko=include_taiko));hostbase=directory(base/host)
            pointer=hostbase/'current.json'
            old=manifest(read_json(pointer)) if pointer.exists() else None
            if old is not None:
                require(old['scheme']==(TAIKO_SCHEME if include_taiko else SCHEME),
                        'incompatible_statistics_replica')
            if old and (old['server_epoch'],old['policy_id'],old['watermark'],old['generated_at'])==self.stamp:
                return old
            pages=[];entries=[]
            for start in range(0,len(self.projection),PAGE_ROWS):
                rows=[]
                for row in self.projection[start:start+PAGE_ROWS]:
                    item={**row['player'],'requester_host_totals':row['own'].get(host,dict.fromkeys(TOTALS,0))}
                    rows.append({'ruleset_id':row['ruleset_id'],'player':item})
                body={'ok':True,'format':1,'index':len(pages),'rows':rows};raw=canonical(body)
                require(len(raw)<=MAX_PAGE,'statistics_replica_capacity_required',503)
                pages.append(body);entries.append({'index':body['index'],'rows':len(rows),'bytes':len(raw),'sha256':fingerprint(body)})
            value={'ok':True,'format':1,'scheme':TAIKO_SCHEME if include_taiko else SCHEME,'server_epoch':epoch,'host_id':host,'policy_id':policy,
                   'watermark':self.stamp[2],'generated_at':self.stamp[3],'rows':len(self.projection),'pages':entries}
            value['content_id']=fingerprint(value);manifest(value)
            stage=Path(tempfile.mkdtemp(prefix='.build-',dir=hostbase));target=hostbase/value['content_id']
            try:
                for body in pages:atomic_json(stage/f"page-{body['index']}.json",body)
                atomic_json(stage/'manifest.json',value)
                if target.exists():require(manifest(read_json(_private(target,directory=True)/'manifest.json'))==value,'invalid_statistics_replica')
                else:os.rename(stage,target)
                atomic_json(pointer,value)
                self._prune(hostbase,{value['content_id'],old['content_id'] if old else None})
            finally:
                if stage.exists():shutil.rmtree(stage)
            return value

    @staticmethod
    def _prune(base,keep):
        for candidate in base.iterdir():
            if candidate.name in keep:continue
            try:
                digest(candidate.name);_private(candidate,directory=True)
                value=manifest(read_json(candidate/'manifest.json'));require(value['content_id']==candidate.name,'invalid_statistics_replica')
                require({p.name for p in candidate.iterdir()}=={'manifest.json',*[f"page-{r['index']}.json" for r in value['pages']]},'invalid_statistics_replica')
                for p in candidate.iterdir():_private(p)
            except (OSError,ValueError,NetworkError):continue
            shutil.rmtree(candidate)

    def read_page(self,token,content_id,index,*,include_catch=False,include_taiko=False):
        include_catch=include_catch or include_taiko
        digest(content_id);integer(index,0,MAX_PAGES-1)
        host,epoch,policy,_=self.authority(token)
        base=_private(self.store.root/self._scope_dir(include_catch=include_catch,include_taiko=include_taiko)/host/content_id,directory=True)
        value=manifest(read_json(base/'manifest.json'))
        require(value['scheme']==(TAIKO_SCHEME if include_taiko else SCHEME),
                'incompatible_statistics_replica')
        require(value['host_id']==host and value['server_epoch']==epoch and value['policy_id']==policy,'statistics_replica_policy_changed',409)
        require(index<len(value['pages']),'statistics_replica_not_found',404)
        return page(read_json(_private(base/f'page-{index}.json'),maximum=MAX_PAGE),value['pages'][index])


class Replica:
    def __init__(self,root,*,clock=time.time):
        self.root=Path(root);self.clock=clock;self.path=self.root/'state/activity-cache.sqlite3'

    @staticmethod
    def schema(db):
        db.execute('CREATE TABLE IF NOT EXISTS replica_meta(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,checked REAL NOT NULL,next_check REAL NOT NULL,phase TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS replica_players(user_id INTEGER NOT NULL,ruleset_id INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(user_id,ruleset_id))')
        db.execute('CREATE TABLE IF NOT EXISTS replica_names(normalized TEXT NOT NULL,ruleset_id INTEGER NOT NULL,user_id INTEGER NOT NULL,PRIMARY KEY(normalized,ruleset_id,user_id))')

    def observation(self,db):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='replica_meta'").fetchone() is None:return None
        row=db.execute('SELECT * FROM replica_meta WHERE id=1').fetchone()
        if row is None:return None
        info=json.loads(row['payload']);now=self.clock()
        if row['phase']=='legacy':return {'phase':'legacy','next_check':row['next_check']}
        value=manifest(info['manifest']);marker,_=load(self.root)
        require(all(info[k]==marker[k] for k in ('installation_id','generation')),'foreign_statistics_replica')
        from ..reporting import profile
        if not profile(self.root)['consume_statistics']:return {'phase':'unavailable','manifest':value,'checked':row['checked'],'next_check':row['next_check']}
        # Agent publishes a nonsecret binding; command reads no credential contents.
        binding=read_json(self.root/'state/activity-replica-binding.json')
        fields(binding,('installation_id','generation','host_id','server_epoch'))
        require(all(binding[k]==marker[k] for k in ('installation_id','generation'))
                and all(value[k]==binding[k] for k in ('host_id','server_epoch')),'foreign_statistics_replica')
        require((self.root/'secrets/network.json').exists(),'network_enrollment_required')
        ready=row['phase']=='ready' and 0<=now-row['checked']<=POLICY_LEASE and -300<=now-value['generated_at']<=MAX_AGE
        return {'phase':'ready' if ready else 'unavailable','manifest':value,'checked':row['checked'],'next_check':row['next_check']}

    @contextmanager
    def metadata(self):
        """Command reads never reserve the background agent's SQLite writer.

        Keep a short read transaction, private-path checks and current privacy
        authority. No immutable flag here: the agent updates this small pointer
        database. Only the separate completed player replica is immutable.
        """
        # Match the existing cache directory contract (not group/world
        # writable); legacy caches may have a searchable 0755 state directory.
        for parent in (self.path.parent, *self.path.parent.parents):
            require(not parent.is_symlink(), 'unsafe_activity_storage')
        info = self.path.parent.stat()
        require(self.path.parent.is_dir() and info.st_uid == os.geteuid()
                and not info.st_mode & 0o022, 'unsafe_activity_storage_parent')
        if not os.path.lexists(self.path):
            yield None
            return
        _private(self.path)
        require(self.path.stat().st_size < 512*1024**2, 'activity_capacity_required', 503)
        for suffix in ('-wal', '-shm', '-journal'):
            side = Path(str(self.path)+suffix)
            if os.path.lexists(side):
                _private(side)
        db = sqlite3.connect('file:'+quote(str(self.path))+'?mode=ro', uri=True, timeout=.05)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=50')
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield db
        finally:
            db.close()

    @contextmanager
    def database(self,state):
        # This is a closed, atomically published database, never a live WAL DB.
        path=_private(self.root/'state/activity-replica-downloads'/state['manifest']['content_id']/'replica.sqlite3')
        require(not any(Path(str(path)+suffix).exists() for suffix in ('-wal','-journal')),'invalid_statistics_replica')
        db=sqlite3.connect('file:'+quote(str(path))+'?mode=ro&immutable=1',uri=True)
        try:yield db
        finally:db.close()

    def stats(self,uid,mode):
        with self.metadata() as db:state=self.observation(db) if db is not None else None
        if state is None or state['phase']=='legacy':return False,None
        if state['phase']!='ready':return True,None
        if mode==1 and state['manifest']['scheme']!=TAIKO_SCHEME:return False,None
        with self.database(state) as db:
            row=db.execute('SELECT payload FROM replica_players WHERE user_id=? AND ruleset_id=?',(uid,mode)).fetchone()
        value=json.loads(row[0]) if row else None
        if value:value.update(network_statistics=True,network_statistics_fetched_at=state['manifest']['generated_at'])
        return True,value

    def lookup(self,name,mode):
        with self.metadata() as db:state=self.observation(db) if db is not None else None
        if state is None or state['phase']=='legacy':return False,None
        if state['phase']!='ready':return True,None
        if mode==1 and state['manifest']['scheme']!=TAIKO_SCHEME:return False,None
        with self.database(state) as db:
            rows=db.execute('SELECT user_id FROM replica_names WHERE normalized=? AND ruleset_id=? ORDER BY user_id',(name.casefold(),mode)).fetchall()
            if len(rows)!=1:return True,None
            row=db.execute('SELECT payload FROM replica_players WHERE user_id=? AND ruleset_id=?',(rows[0][0],mode)).fetchone()
        value=json.loads(row[0]);return True,(value['user_id'],value['username'])

    def status(self):
        with self.metadata() as db:
            state=self.observation(db) if db is not None else None
            if state is None:return {'phase':'cold'}
            result={'phase':state['phase']}
            if 'manifest' in state:
                result.update(rows=state['manifest']['rows'],generated_at=state['manifest']['generated_at'],policy_checked_at=state['checked'])
            agent=self.root/'state/activity-replica-agent.json'
            if agent.exists():
                receipt=read_json(agent)
                fields(receipt,('format','installation_id','generation','at','next_attempt','phase','error'))
                marker,_=load(self.root)
                require(receipt['format']==1 and all(receipt[k]==marker[k] for k in ('installation_id','generation')),'foreign_statistics_replica')
                require(receipt['phase'] in ('ready','legacy','retrying') and receipt['error'] in (None,'replica-unavailable'),'invalid_statistics_replica')
                number(receipt['at']);number(receipt['next_attempt'])
                result.update(sync_phase=receipt['phase'],sync_error=receipt['error'],next_attempt=receipt['next_attempt'])
            return result

    def tick(self,network,*,stop=None):
        """Durable isolated backoff; a broken replica must not hold uploads."""
        path=self.root/'state/activity-replica-agent.json';now=self.clock()
        marker,_=load(self.root)
        if path.exists():
            state=read_json(path)
            fields(state,('format','installation_id','generation','at','next_attempt','phase','error'))
            require(state['format']==1 and all(state[k]==marker[k] for k in ('installation_id','generation')),'foreign_statistics_replica')
            number(state['at']);number(state['next_attempt'])
            require(state['at']<=state['next_attempt']<=state['at']+INTERVAL,'invalid_statistics_replica')
            require(state['phase'] in ('ready','legacy','retrying') and state['error'] in (None,'replica-unavailable'),'invalid_statistics_replica')
            if state['at']<=now<state['next_attempt']:return state
        phase='ready';error=None
        try:
            value=self.sync(network,stop=stop)
            if value['phase']=='legacy':phase='legacy'
        except Exception:
            phase='retrying';error='replica-unavailable'
        state={'format':1,**{k:marker[k] for k in ('installation_id','generation')},
               'at':now,'next_attempt':now+(INTERVAL if phase=='legacy' else POLICY_INTERVAL),'phase':phase,'error':error}
        atomic_json(path,state);return state

    def sync(self,network,*,stop=None,force=False):
        marker,_=load(self.root);profile=network.profile();credential=network.credential()
        require(credential is not None,'network_enrollment_required')
        token=credential['credential'];transport=network.transport(profile);now=self.clock()
        # Bulk aggregation/compression has a longer deadline than heartbeat.
        from ..network.transport import Transport
        if type(transport) is Transport:transport.timeout=30
        binding={**{k:marker[k] for k in ('installation_id','generation')},**{k:credential[k] for k in ('host_id','server_epoch')}}
        atomic_json(self.root/'state/activity-replica-binding.json',binding)
        with connection(self.path) as db:
            self.schema(db);old=db.execute('SELECT * FROM replica_meta WHERE id=1').fetchone()
            if old and not force and old['checked']<=now<old['next_check']:return {'ok':True,'phase':'idle','transferred':0}
        rooms=load(self.root)[1]['rooms']
        from ..managed_host.projection import read as read_managed_projection
        managed=read_managed_projection(self.root)
        projected=[row['settings'] for row in managed['rooms'].values()] if managed else []
        rooms=[*rooms,*projected]
        include_taiko=any(r.get('ruleset_id')==1 or str(r.get('preset','')).startswith('taiko-') for r in rooms)
        include_catch=include_taiko or any(r.get('ruleset_id')==2 or r.get('preset')=='catch-fixed' for r in rooms)
        route='/v3/activity/replica' if include_taiko else '/v2/activity/replica' if include_catch else '/v1/activity/replica'
        try:
            try:reply=transport.request('GET',route+'/current',token=token)
            except NetworkError as exc:
                if exc.status!=404 or route.startswith('/v1/') or include_taiko:raise
                route='/v1/activity/replica'
                reply=transport.request('GET',route+'/current',token=token)
        except NetworkError as exc:
            if exc.status==404 and exc.code=='route_not_found':
                if include_taiko:
                    # Keep the previous Standard/Catch generation intact. A
                    # pre-Taiko server has not verified Taiko totals.
                    raise NetworkError('statistics_taiko_replica_unavailable',503) from None
                with connection(self.path) as db:
                    db.execute('INSERT OR REPLACE INTO replica_meta VALUES(1,?,?,?,?)',('{}',now,now+INTERVAL,'legacy'))
                    db.execute('DELETE FROM replica_players');db.execute('DELETE FROM replica_names')
                return {'ok':True,'phase':'legacy','transferred':0}
            if exc.status in (401,403,409):
                with connection(self.path) as db:db.execute("UPDATE replica_meta SET phase='unavailable',next_check=0 WHERE id=1")
            raise
        value=manifest(reply)
        require((value['scheme']==TAIKO_SCHEME) if include_taiko else (value['scheme']==SCHEME),
                'incompatible_statistics_replica')
        require(value['server_epoch']==credential['server_epoch'] and value['host_id']==credential['host_id'],'foreign_statistics_replica')
        require(-300<=now-value['generated_at']<=MAX_AGE,'statistics_replica_expired')
        payload={**{k:marker[k] for k in ('installation_id','generation')},'manifest':value}
        with connection(self.path) as db:
            if old and old['phase']!='legacy':
                previous=manifest(json.loads(old['payload'])['manifest'])
                require(value['server_epoch']==previous['server_epoch'] and value['watermark']>=previous['watermark'] and value['generated_at']>=previous['generated_at'],'statistics_replica_rollback')
                if old['phase']=='ready' and previous==value:
                    db.execute('UPDATE replica_meta SET checked=?,next_check=? WHERE id=1',(now,now+POLICY_INTERVAL))
                    return {'ok':True,'phase':'unchanged','transferred':0}
                # New policy invalidates all old data BEFORE any further page I/O.
                if value['policy_id']!=previous['policy_id']:
                    db.execute("UPDATE replica_meta SET phase='unavailable',next_check=0 WHERE id=1")
        if old is None or old['phase']=='legacy':
            # Once the hub advertises the new protocol, unknown legacy cache
            # visibility cannot remain authoritative during the first download.
            with connection(self.path) as db:
                db.execute('INSERT OR REPLACE INTO replica_meta VALUES(1,?,?,?,?)',
                           (canonical(payload).decode(),now,0,'unavailable'))
        base=directory(self.root/'state/activity-replica-downloads');target=directory(base/value['content_id'])
        download=target/'download.json'
        if download.exists():require(read_json(_private(download))==payload,'foreign_statistics_replica')
        else:
            require(not list(target.iterdir()),'foreign_statistics_replica');atomic_json(download,payload)
        transferred=0
        for entry in value['pages']:
            if stop is not None and stop.is_set():return {'ok':True,'phase':'interrupted','transferred':transferred}
            path=target/f"page-{entry['index']}.json"
            if path.exists():
                try:page(read_json(_private(path),maximum=MAX_PAGE),entry);continue
                except (OSError,ValueError,NetworkError):pass
            require(shutil.disk_usage(base).free>16*1024**2+entry['bytes'],'statistics_replica_disk_reserve',503)
            body=transport.request('GET',f"{route}/page?content_id={value['content_id']}&index={entry['index']}",token=token)
            page(body,entry);atomic_json(path,body);transferred+=entry['bytes']
        # Confirm current authority again after the last page; no stale-policy window.
        confirmed=manifest(transport.request('GET',route+'/current',token=token))
        require(confirmed==value,'statistics_replica_policy_changed',409)
        now=self.clock();previous_key=(-1,0)
        # Build outside the command cache: no large writer transaction blocks .stats.
        building=target/'replica-building.sqlite3'
        with connection(building) as db:
            self.schema(db)
            db.execute('DELETE FROM replica_players');db.execute('DELETE FROM replica_names')
            for entry in value['pages']:
                body=page(read_json(_private(target/f"page-{entry['index']}.json"),maximum=MAX_PAGE),entry)
                for row in body['rows']:
                    item=row['player'];mode=row['ruleset_id'];key=(mode,item['user_id'])
                    require(key>previous_key,'invalid_statistics_replica');previous_key=key
                    db.execute('INSERT INTO replica_players VALUES(?,?,?)',(item['user_id'],mode,canonical(item).decode()))
                    db.execute('INSERT INTO replica_names VALUES(?,?,?)',(item['username'].casefold(),mode,item['user_id']))
            require(db.execute('SELECT COUNT(*) FROM replica_players').fetchone()[0]==value['rows'],'invalid_statistics_replica')
            require(db.execute('PRAGMA quick_check').fetchall()[0][0]=='ok','invalid_statistics_replica')
        with building.open('rb') as stream:os.fsync(stream.fileno())
        os.replace(building,target/'replica.sqlite3')
        fd=os.open(target,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        confirmed=manifest(transport.request('GET',route+'/current',token=token))
        require(confirmed==value,'statistics_replica_policy_changed',409)
        now=self.clock()
        with connection(self.path) as db:
            db.execute('INSERT OR REPLACE INTO replica_meta VALUES(1,?,?,?,?)',(canonical(payload).decode(),now,now+POLICY_INTERVAL,'ready'))
        # Only our validated per-installation staged files may be removed.
        for child in base.iterdir():
            if child==target:
                # Raw pages are disposable after the closed SQLite projection commits.
                for entry in value['pages']:_private(child/f"page-{entry['index']}.json").unlink()
                continue
            try:
                digest(child.name);_private(child,directory=True);owned=read_json(_private(child/'download.json'))
                require(all(owned[k]==marker[k] for k in ('installation_id','generation')),'foreign_statistics_replica')
                meta=manifest(owned['manifest']);require(child.name==meta['content_id'],'invalid_statistics_replica')
                require({p.name for p in child.iterdir()}<={'download.json','replica.sqlite3','replica-building.sqlite3','replica-building.sqlite3-journal',*[f"page-{e['index']}.json" for e in meta['pages']]},'invalid_statistics_replica')
                for p in child.iterdir():_private(p)
            except (OSError,ValueError,NetworkError,KeyError):continue
            shutil.rmtree(child)
        return {'ok':True,'phase':'activated','transferred':transferred,'rows':value['rows']}
