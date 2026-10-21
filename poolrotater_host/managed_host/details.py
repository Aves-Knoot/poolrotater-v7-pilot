"""Paged, authenticated staff-only desired/effective configuration snapshots.

Large greeting dictionaries stay out of the frequent capability heartbeat. Pages
use the existing bounded transport; none of this data enters the public directory.
"""
import base64
import hashlib
import json
import math
from ..diagnostic_logs.common import private_database,transaction
from ..network.contracts import fields,identifier,integer,require,verifier,canonical,unique
from .contracts import alias as check_alias
from .settings import validate,public_settings

PATH='/v1/hosts/self/managed-settings'
CHUNK=24*1024
MAX_DOCUMENT=32*1024**2
MAX_PAGES=math.ceil(MAX_DOCUMENT/CHUNK)


def snapshot(state):
    state.refresh_binding()
    with transaction(state.path) as db:
        rooms={}
        for row in db.execute('SELECT alias,revision,applied_revision,config,effective FROM rooms ORDER BY alias'):
            rooms[row['alias']]={'revision':row['revision'],'applied_revision':row['applied_revision'],
                'desired':public_settings(json.loads(row['config'])),'effective':public_settings(json.loads(row['effective']))}
    return {'protocol_version':1,**state.binding,'rooms':rooms}


def checked(value):
    fields(value,('protocol_version','host_id','installation_id','generation','rooms'))
    integer(value['protocol_version'],1,1)
    for key in ('host_id','installation_id','generation'):identifier(value[key])
    require(type(value['rooms']) is dict and 1<=len(value['rooms'])<=4,'invalid_managed_settings')
    for name,room in value['rooms'].items():
        check_alias(name);fields(room,('revision','applied_revision','desired','effective'))
        for key in ('revision','applied_revision'):integer(room[key],0,2**53-1)
        require(room['applied_revision']<=room['revision'],'invalid_managed_settings')
        for key in ('desired','effective'):
            settings=room[key]
            require(type(settings) is dict and type(settings.get('password_configured')) is bool
                    and 'password' not in settings,'invalid_managed_settings')
            try:validate({k:v for k,v in settings.items() if k!='password_configured'},allow_redacted=True)
            except (ValueError,TypeError):require(False,'invalid_managed_settings')
    return value


def encode(value):
    checked(value);raw=canonical(value)
    require(0<len(raw)<=MAX_DOCUMENT,'managed_settings_too_large')
    return hashlib.sha256(raw).hexdigest(),[raw[n:n+CHUNK] for n in range(0,len(raw),CHUNK)]


def page(value,index,pages,pin):
    return {'protocol_version':1,**{k:value[k] for k in ('installation_id','generation')},
            'content_id':pin,'pages':len(pages),'index':index,
            'chunk':base64.b64encode(pages[index]).decode('ascii')}


class Repository:
    def __init__(self,store):
        self.store=store;self.path=private_database(store.root,'managed-settings.sqlite3')
        with transaction(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS pages(host TEXT NOT NULL,content TEXT NOT NULL,idx INTEGER NOT NULL,total INTEGER NOT NULL,chunk BLOB NOT NULL,PRIMARY KEY(host,content,idx))')
            db.execute('CREATE TABLE IF NOT EXISTS snapshots(host TEXT PRIMARY KEY,content TEXT NOT NULL,document TEXT NOT NULL,received REAL NOT NULL)')
            db.execute('PRAGMA max_page_count=65536')
    def accept(self,token,value):
        fields(value,('protocol_version','installation_id','generation','content_id','pages','index','chunk'))
        integer(value['protocol_version'],1,1)
        for key in ('installation_id','generation'):identifier(value[key])
        pin=value['content_id']
        require(type(pin) is str and len(pin)==64 and all(c in '0123456789abcdef' for c in pin),'invalid_managed_settings')
        integer(value['pages'],1,MAX_PAGES);integer(value['index'],0,value['pages']-1)
        require(type(value['chunk']) is str and 0<len(value['chunk'])<=CHUNK*4//3,'invalid_managed_settings')
        try:raw=base64.b64decode(value['chunk'],validate=True)
        except ValueError:require(False,'invalid_managed_settings')
        require(0<len(raw)<=CHUNK and (value['index']==value['pages']-1 or len(raw)==CHUNK),'invalid_managed_settings')
        with self.store.transaction() as authority:
            host,_=self.store._authenticate(authority,verifier(token),'heartbeat')
            require(all(value[k]==host[k] for k in ('installation_id','generation')),'host_identity_mismatch',403)
            with transaction(self.path) as db:
                current=db.execute('SELECT content FROM snapshots WHERE host=?',(host['id'],)).fetchone()
                if current and current['content']==pin:
                    return {'ok':True,'content_id':pin,'next_index':value['pages'],'complete':True}
                old=db.execute('SELECT * FROM pages WHERE host=? AND content=? AND idx=?',(host['id'],pin,value['index'])).fetchone()
                if old:require(old['total']==value['pages'] and old['chunk']==raw,'managed_settings_page_conflict',409)
                else:
                    # One current upload per authenticated host, no cross-host
                    # eviction or writes to the public room/directory tables.
                    db.execute('DELETE FROM pages WHERE host=? AND content<>?',(host['id'],pin))
                    used=db.execute('SELECT COALESCE(SUM(length(chunk)),0) FROM pages').fetchone()[0]
                    used+=db.execute('SELECT COALESCE(SUM(length(CAST(document AS BLOB))),0) FROM snapshots').fetchone()[0]
                    require(used+len(raw)<=256*1024**2,'managed_settings_capacity_required',503)
                    db.execute('INSERT INTO pages VALUES(?,?,?,?,?)',(host['id'],pin,value['index'],value['pages'],raw))
                rows=db.execute('SELECT idx,total,chunk FROM pages WHERE host=? AND content=? ORDER BY idx',(host['id'],pin)).fetchall()
                require(all(row['total']==value['pages'] for row in rows),'managed_settings_page_conflict',409)
                indexes={row['idx'] for row in rows};next_index=next((n for n in range(value['pages']) if n not in indexes),value['pages'])
                if next_index==value['pages']:
                    combined=b''.join(row['chunk'] for row in rows)
                    require(len(combined)<=MAX_DOCUMENT and hashlib.sha256(combined).hexdigest()==pin,'invalid_managed_settings')
                    # This is a locally reassembled, hash-checked document,
                    # not a larger wire-body allowance for other API routes.
                    try:
                        parsed=json.loads(combined,object_pairs_hook=unique,
                            parse_constant=lambda _:require(False,'invalid_managed_settings'))
                    except (ValueError,UnicodeError,RecursionError):require(False,'invalid_managed_settings')
                    document=checked(parsed)
                    require(document['host_id']==host['id'] and all(document[k]==host[k] for k in ('installation_id','generation')),'host_identity_mismatch',403)
                    previous=db.execute('SELECT document FROM snapshots WHERE host=?',(host['id'],)).fetchone()
                    if previous:
                        before=json.loads(previous['document'])
                        if all(before[k]==document[k] for k in ('installation_id','generation')):
                            require(set(before['rooms'])==set(document['rooms']),'managed_settings_revision_conflict',409)
                            for alias,room in document['rooms'].items():
                                old=before['rooms'][alias]
                                require(room['revision']>=old['revision'] and room['applied_revision']>=old['applied_revision'],
                                        'managed_settings_stale',409)
                                if (room['revision'],room['applied_revision'])==(old['revision'],old['applied_revision']):
                                    require(room==old,'managed_settings_revision_conflict',409)
                    db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?)',
                               (host['id'],pin,canonical(document).decode(),self.store.clock()))
                    db.execute('DELETE FROM pages WHERE host=?',(host['id'],))
                return {'ok':True,'content_id':pin,'next_index':next_index,'complete':next_index==value['pages']}
    def inspect(self,host_id,capabilities):
        identifier(host_id)
        with transaction(self.path) as db:
            row=db.execute('SELECT document,received FROM snapshots WHERE host=?',(host_id,)).fetchone()
        if row is None or capabilities is None:return {'phase':'syncing','document':None}
        document=checked(json.loads(row['document']))
        if any(document[k]!=capabilities[k] for k in ('host_id','installation_id','generation')):
            return {'phase':'binding-changed','document':None}
        revisions={r['alias']:(r['revision'],r['applied_revision']) for r in capabilities['rooms']}
        current=set(revisions)==set(document['rooms']) and all(revisions[a]==(r['revision'],r['applied_revision']) for a,r in document['rooms'].items())
        return {'phase':'current' if current else 'syncing','document':document,'received_at':row['received']}
