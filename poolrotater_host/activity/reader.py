"""Bounded local personal-stat cache/request queue. Player commands never do HTTP."""
import json
from pathlib import Path
import time
from ..network.contracts import require, integer, text, fields, number, identifier, canonical
from .storage import connection
from .query import contract


def validate_player(item):
    require(type(item) is dict and type(item.get('hidden')) is bool,'invalid_activity_query_reply');integer(item.get('user_id'))
    if item['hidden']:fields(item,('user_id','hidden'))
    else:
        fields(item,('user_id','username','hidden','total_gain','achievement_count','best_gain','total_lobby_seconds','visit_count','map_plays','first_gain_at','last_gain_at','first_join_at','last_join_at','last_leave_at','coverage','host_ids','requester_host_totals'))
        text(item['username'],80)
        for key in ('total_gain','best_gain','total_lobby_seconds'):number(item[key])
        for key in ('achievement_count','visit_count','map_plays'):integer(item[key],0)
        for key in ('first_gain_at','last_gain_at','first_join_at','last_join_at','last_leave_at'):
            if item[key] is not None:number(item[key])
        require(type(item['coverage']) is dict and type(item['host_ids']) is list and len(item['host_ids'])<=100,'invalid_activity_query_reply')
        for host_id in item['host_ids']:identifier(host_id)
        own=item['requester_host_totals']
        fields(own,('total_gain','achievement_count','best_gain','total_lobby_seconds','visit_count','map_plays'))
        for key in ('total_gain','best_gain','total_lobby_seconds'):number(own[key])
        for key in ('achievement_count','visit_count','map_plays'):integer(own[key],0)
    return item


class Reader:
    def __init__(self, root, *, clock=time.time, max_age=120):
        self.root=Path(root);self.clock=clock;self.max_age=max_age
        self.path=self.root/'state/activity-cache.sqlite3'
        with connection(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cache(user_id INTEGER NOT NULL,ruleset_id INTEGER NOT NULL,payload TEXT NOT NULL,fetched REAL NOT NULL,PRIMARY KEY(user_id,ruleset_id))')
            db.execute('CREATE TABLE IF NOT EXISTS queries(query_key TEXT PRIMARY KEY,ruleset_id INTEGER NOT NULL,user_id INTEGER,username TEXT,queued REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS privacy(user_id INTEGER PRIMARY KEY,hidden INTEGER NOT NULL,observed REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS names(username TEXT NOT NULL,ruleset_id INTEGER NOT NULL,user_id INTEGER NOT NULL,PRIMARY KEY(username,ruleset_id))')
            db.execute('CREATE TABLE IF NOT EXISTS lookups(username TEXT NOT NULL,ruleset_id INTEGER NOT NULL,fetched REAL NOT NULL,PRIMARY KEY(username,ruleset_id))')

    def request(self, *, user_id=None, username=None, ruleset_id=0):
        require(type(ruleset_id) is int and ruleset_id in (0,3),'invalid_activity_ruleset')
        require((user_id is None)!=(username is None),'invalid_activity_lookup')
        if user_id is not None:integer(user_id);key='id:'+str(user_id)
        else:text(username,80);key='name:'+username.casefold()
        with connection(self.path) as db:
            if username is not None:
                miss=db.execute('SELECT fetched FROM lookups WHERE username=? AND ruleset_id=?',(username.casefold(),ruleset_id)).fetchone()
                if miss and 0<=self.clock()-miss[0]<=self.max_age:return True
            exists=db.execute('SELECT 1 FROM queries WHERE query_key=?',(str(ruleset_id)+':'+key,)).fetchone()
            if exists is None and db.execute('SELECT COUNT(*) FROM queries').fetchone()[0]>=200:return False
            db.execute('INSERT OR IGNORE INTO queries VALUES(?,?,?,?,?)',(str(ruleset_id)+':'+key,ruleset_id,user_id,username,self.clock()))
        return True

    def user_stats(self,user_id,*,ruleset_id=0):
        integer(user_id)
        from .replica import Replica
        handled,value=Replica(self.root,clock=self.clock).stats(user_id,ruleset_id)
        if handled:return value
        with connection(self.path) as db:
            row=db.execute('SELECT payload,fetched FROM cache WHERE user_id=? AND ruleset_id=?',(user_id,ruleset_id)).fetchone()
            privacy=db.execute('SELECT hidden FROM privacy WHERE user_id=?',(user_id,)).fetchone()
        self.request(user_id=user_id,ruleset_id=ruleset_id)
        if privacy and privacy[0]:return {'user_id':user_id,'hidden':True}
        if row is None:return None
        value=json.loads(row[0])
        if value.get('hidden'):return value
        if not 0<=self.clock()-row['fetched']<=self.max_age:return None
        return {**value,'network_statistics':True,'network_statistics_fetched_at':row['fetched']}

    def lookup(self,username,*,ruleset_id=0):
        text(username,80)
        from .replica import Replica
        handled,value=Replica(self.root,clock=self.clock).lookup(username,ruleset_id)
        if handled:return value
        self.request(username=username,ruleset_id=ruleset_id)
        with connection(self.path) as db:
            row=db.execute('SELECT user_id FROM names WHERE username=? AND ruleset_id=?',(username.casefold(),ruleset_id)).fetchone()
        if row is None:return None
        value=self.user_stats(row[0],ruleset_id=ruleset_id)
        return (row[0],value['username']) if value and not value.get('hidden') else None

    def lookup_status(self,username,*,ruleset_id=0):
        """A validated empty reply is not an indefinitely pending request.

        Hidden and absent names deliberately have the same public result. The
        short negative cache expires so a later history import can be found.
        """
        text(username,80)
        from .replica import Replica
        state=Replica(self.root,clock=self.clock).status()
        if state['phase']=='ready':return 'not_found'
        if state['phase'] not in ('cold','legacy'):return 'pending'
        require(type(ruleset_id) is int and ruleset_id in (0,3),'invalid_activity_ruleset')
        with connection(self.path) as db:
            row=db.execute('SELECT fetched FROM lookups WHERE username=? AND ruleset_id=?',(username.casefold(),ruleset_id)).fetchone()
        return 'not_found' if row and 0<=self.clock()-row[0]<=self.max_age else 'pending'

    def hidden(self,user_id,*,ruleset_id=0):
        value=self.user_stats(user_id,ruleset_id=ruleset_id)
        return bool(value.get('hidden')) if value is not None else None

    def refresh(self,network):
        """Agent-only network operation. Queue survives outages until validated reply."""
        profile=network.profile();credential=network.credential()
        require(credential is not None,'network_enrollment_required')
        with connection(self.path) as db:
            rows=db.execute('SELECT * FROM queries ORDER BY queued LIMIT 30').fetchall()
        if not rows:return {'ok':True,'queries':0,'provider_dispatch':False}
        mode=rows[0]['ruleset_id'];selected=[];ids=[];names=[]
        for row in rows:
            if row['ruleset_id']!=mode:continue
            if row['user_id'] is not None and len(ids)<20:ids.append(row['user_id']);selected.append(row)
            elif row['username'] is not None and len(names)<10:names.append(row['username']);selected.append(row)
        request={'protocol_version':1,'user_ids':ids,'usernames':names,'ruleset_id':mode};contract(request)
        reply=network.transport(profile).request('POST','/v1/activity/query',token=credential['credential'],body=request)
        fields(reply,('ok','protocol_version','ruleset_id','generated_at','players','coverage'))
        require(reply['ok'] is True and reply['protocol_version']==1 and reply['ruleset_id']==mode,'invalid_activity_query_reply')
        number(reply['generated_at']);require(type(reply['players']) is list and len(reply['players'])<=30,'invalid_activity_query_reply')
        validated=[]
        for item in reply['players']:
            validate_player(item)
            validated.append(item)
        with connection(self.path) as db:
            returned_names={item['username'].casefold() for item in validated if not item['hidden']}
            for name in names:
                normalized=name.casefold()
                if normalized in returned_names:
                    db.execute('DELETE FROM lookups WHERE username=? AND ruleset_id=?',(normalized,mode))
                else:
                    db.execute('DELETE FROM names WHERE username=? AND ruleset_id=?',(normalized,mode))
                    db.execute('INSERT OR REPLACE INTO lookups VALUES(?,?,?)',(normalized,mode,self.clock()))
            for item in validated:
                db.execute('INSERT OR REPLACE INTO privacy VALUES(?,?,?)',(item['user_id'],int(item['hidden']),self.clock()))
                if item['hidden']:
                    db.execute('DELETE FROM cache WHERE user_id=?',(item['user_id'],))
                    db.execute('DELETE FROM names WHERE user_id=?',(item['user_id'],))
                else:
                    db.execute("DELETE FROM cache WHERE user_id=? AND json_extract(payload,'$.hidden')=1",(item['user_id'],))
                db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)',(item['user_id'],mode,canonical(item).decode(),self.clock()))
                if not item['hidden']:
                    db.execute('DELETE FROM lookups WHERE username=? AND ruleset_id=?',(item['username'].casefold(),mode))
                    db.execute('DELETE FROM names WHERE user_id=? AND ruleset_id=?',(item['user_id'],mode))
                    db.execute('INSERT OR REPLACE INTO names VALUES(?,?,?)',(item['username'].casefold(),mode,item['user_id']))
            for row in selected:db.execute('DELETE FROM queries WHERE query_key=?',(row['query_key'],))
            # Bounded cache can be rebuilt; hidden markers remain conservative.
            db.execute('DELETE FROM cache WHERE rowid IN (SELECT rowid FROM cache ORDER BY fetched DESC LIMIT -1 OFFSET 5000)')
            db.execute('DELETE FROM names WHERE NOT EXISTS(SELECT 1 FROM cache c WHERE c.user_id=names.user_id AND c.ruleset_id=names.ruleset_id)')
            db.execute('DELETE FROM lookups WHERE rowid IN (SELECT rowid FROM lookups ORDER BY fetched DESC LIMIT -1 OFFSET 5000)')
        return {'ok':True,'queries':len(selected),'players':len(validated),'provider_dispatch':False}

    def status(self):
        from .replica import Replica
        state=Replica(self.root,clock=self.clock).status()
        with connection(self.path) as db:
            return {'ok':True,'pending_queries':db.execute('SELECT COUNT(*) FROM queries').fetchone()[0],
                    'cached_players':db.execute('SELECT COUNT(*) FROM cache').fetchone()[0],'replica':state,'provider_dispatch':False}


def for_settings(settings):
    """Use the existing protected installation root and an explicit bound choice."""
    import os
    import sqlite3
    from ..network.contracts import NetworkError
    from ..layout import load, contained, read_json
    root=settings.get('ratings_reader_root')
    if not root:
        return None
    root=Path(root);path=contained(root,'config/reporting.json')
    if not os.path.lexists(path):return None
    marker,_=load(root);value=read_json(path)
    fields(value,('format','installation_id','generation','share_statistics','consume_statistics','operational_logs','public_chat'))
    require(type(value['format']) is int and value['format']==1
            and all(value[k]==marker[k] for k in ('installation_id','generation'))
            and all(type(value[k]) is bool for k in ('share_statistics','consume_statistics','operational_logs','public_chat')),
            'invalid_reporting_profile')
    if not value['consume_statistics']:return None
    try:return Reader(root)
    except (OSError,sqlite3.Error,NetworkError):
        return DeferredReader(root)


class DeferredReader:
    """Unavailable optional cache stays fail-closed and retries only local storage."""
    def __init__(self,root):self.root=root
    def _call(self,method,*args,**kwargs):
        import sqlite3
        from ..network.contracts import NetworkError
        try:return getattr(Reader(self.root),method)(*args,**kwargs)
        except (OSError,sqlite3.Error,NetworkError):return None
    def user_stats(self,*args,**kwargs):return self._call('user_stats',*args,**kwargs)
    def lookup(self,*args,**kwargs):return self._call('lookup',*args,**kwargs)
    def lookup_status(self,*args,**kwargs):return self._call('lookup_status',*args,**kwargs)
    def hidden(self,*args,**kwargs):return self._call('hidden',*args,**kwargs)
