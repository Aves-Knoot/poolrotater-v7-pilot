"""Private, bounded Discord routes and command ledger, separate from hub schema."""
from contextlib import contextmanager
import json
import os
import sqlite3
import stat
import uuid
from ..network.contracts import canonical, identifier, require, verifier
from .model import command, exchange


class Repository:
    def __init__(self,store):
        self.store=store;self.path=store.root/'discord-relay.sqlite3'
        if not os.path.lexists(self.path):
            try:
                fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
            except FileExistsError:pass
        with self.database() as db:
            db.execute('CREATE TABLE IF NOT EXISTS slots(host TEXT NOT NULL,alias TEXT NOT NULL,slot INTEGER UNIQUE NOT NULL,channel TEXT UNIQUE,PRIMARY KEY(host,alias))')
            db.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,host TEXT NOT NULL,payload TEXT NOT NULL,phase TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,notified INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS replies(id TEXT PRIMARY KEY,request TEXT NOT NULL,channel TEXT NOT NULL,content TEXT NOT NULL,sent INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS capabilities(host TEXT PRIMARY KEY,received REAL NOT NULL)')

    @contextmanager
    def database(self):
        for suffix in ('','-wal','-shm','-journal'):
            p=self.path.with_name(self.path.name+suffix)
            if os.path.lexists(p):
                s=p.lstat();require(stat.S_ISREG(s.st_mode) and s.st_uid==os.geteuid() and s.st_nlink==1
                    and not s.st_mode&0o077 and s.st_size<=32*1024**2,'hub_storage_unavailable',503)
        db=sqlite3.connect(self.path,timeout=2,isolation_level=None);db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA max_page_count=8192');db.execute('BEGIN IMMEDIATE')
            yield db;db.execute('COMMIT')
        except BaseException:db.rollback();raise
        finally:db.close()

    def assign(self,rooms,channels,*,owner_host):
        """Input channels already verified in the configured Discord guild.

        No channel is reused when a host disappears, nor when another host has
        the same operator label. Unassigned rooms wait for added capacity.
        """
        require(type(channels) is dict and all(type(k)is int and 5<=k<=9999
            and type(v)is int and v>0 for k,v in channels.items())
            and len(set(channels.values()))==len(channels),'invalid_request')
        ordered=sorted({(r['host_id'],r['alias']) for r in rooms if r['host_id']!=owner_host})
        with self.database() as db:
            used={r['slot'] for r in db.execute('SELECT slot FROM slots')}
            for host,alias in ordered:
                identifier(host)
                row=db.execute('SELECT * FROM slots WHERE host=? AND alias=?',(host,alias)).fetchone()
                if row is None:
                    free=next((n for n in sorted(channels) if n not in used),None)
                    if free is None:continue
                    db.execute('INSERT INTO slots VALUES(?,?,?,?)',(host,alias,free,str(channels[free])));used.add(free)
                elif row['channel'] is None and row['slot'] in channels:
                    db.execute('UPDATE slots SET channel=? WHERE host=? AND alias=?',(str(channels[row['slot']]),host,alias))
            return [dict(r) for r in db.execute('SELECT * FROM slots ORDER BY slot')]

    def queue(self,host,alias,actor,*,channel_id,content):
        now=self.store.clock()
        rooms=[];offset=0
        while True:
            page=self.store.directory(offset=offset,limit=100,protocol_version=2);rooms.extend(page['rooms']);offset=page['next_offset']
            if offset is None:break
        room=next((r for r in rooms if r['host_id']==host and r['alias']==alias),None)
        require(room is not None and room['freshness']=='fresh' and room['age_seconds']<120
                and room['state'] not in ('unavailable','draining'),'invalid_request')
        with self.store.transaction() as db:
            binding=db.execute('SELECT * FROM hosts WHERE id=? AND revoked=0',(host,)).fetchone()
            require(binding is not None,'host_revoked',403)
            value={'id':str(uuid.uuid4()),'host_id':host,**{k:binding[k] for k in ('installation_id','generation')},
                'alias':alias,'room_id':room['osu_room_id'],'channel_id':channel_id,**actor,'content':content,'expires_at':now+45}
            command(value)
            from ..diagnostic_logs import scrub
            value['content']=scrub(value['content'])
            command(value)
            with self.database() as ledger:
                route=ledger.execute('SELECT * FROM slots WHERE host=? AND alias=?',(host,alias)).fetchone()
                require(route is not None and route['slot']>=5 and route['channel']==str(channel_id),'invalid_request')
                cap=ledger.execute('SELECT received FROM capabilities WHERE host=?',(host,)).fetchone()
                require(cap is not None and now-cap['received']<60,'discord_host_not_connected',409)
                require(ledger.execute("SELECT count(*) FROM requests WHERE host=? AND created>?",(host,now-60)).fetchone()[0]<20,'hub_rate_limited',429)
                require(ledger.execute('SELECT count(*) FROM requests').fetchone()[0]<5000,'hub_capacity_required',503)
                ledger.execute('INSERT INTO requests(id,host,payload,phase,created,expires) VALUES(?,?,?,?,?,?)',(value['id'],host,canonical(value).decode(),'queued',now,value['expires_at']))
        return value['id']

    def accept(self,token,value):
        exchange(value);now=self.store.clock();require(abs(now-value['sent_at'])<=60,'invalid_request')
        with self.store.transaction() as authority:
            host,_=self.store._authenticate(authority,verifier(token),'heartbeat')
            require(all(value[k]==host[k] for k in ('installation_id','generation')),'host_identity_mismatch',403)
            with self.database() as db:
                db.execute('INSERT OR REPLACE INTO capabilities VALUES(?,?)',(host['id'],now))
                for receipt in value['receipts']:
                    row=db.execute('SELECT * FROM requests WHERE id=? AND host=?',(receipt['id'],host['id'])).fetchone()
                    require(row is not None,'invalid_request')
                    require(row['phase'] in ('delivered',receipt['result']),'invalid_request')
                    db.execute('UPDATE requests SET phase=? WHERE id=?',(receipt['result'],receipt['id']))
                for raw_reply in value['replies']:
                    from ..diagnostic_logs import scrub
                    reply={**raw_reply,'content':scrub(raw_reply['content'],secrets=(token,))}
                    row=db.execute('SELECT * FROM requests WHERE id=? AND host=?',(reply['request_id'],host['id'])).fetchone()
                    require(row is not None and now-row['created']<=300,'invalid_request')
                    original=command(json.loads(row['payload']))
                    existing=db.execute('SELECT * FROM replies WHERE id=?',(reply['id'],)).fetchone()
                    require(existing is None or (existing['request']==reply['request_id'] and existing['content']==reply['content']),'invalid_request')
                    require(existing is not None or db.execute('SELECT count(*) FROM replies WHERE request=?',(row['id'],)).fetchone()[0]<32,'hub_rate_limited',429)
                    require(existing is not None or db.execute('SELECT count(*) FROM replies').fetchone()[0]<4000,'hub_capacity_required',503)
                    db.execute('INSERT OR IGNORE INTO replies VALUES(?,?,?,?,0,?)',(reply['id'],row['id'],str(original['channel_id']),reply['content'],now))
                pending=db.execute("SELECT * FROM requests WHERE host=? AND phase='queued' AND expires>? ORDER BY created,id LIMIT 8",(host['id'],now)).fetchall()
                # Once delivered, never redeliver mutations after response loss.
                db.executemany("UPDATE requests SET phase='delivered' WHERE id=?",[(r['id'],) for r in pending])
                db.execute('DELETE FROM replies WHERE created<?',(now-3600,))
                db.execute('DELETE FROM requests WHERE created<?',(now-3600,))
                return {'ok':True,'requests':[json.loads(r['payload']) for r in pending]}

    def take_replies(self):
        with self.database() as db:
            rows=db.execute('SELECT * FROM replies WHERE sent=0 ORDER BY created,id LIMIT 32').fetchall()
            # Chat output too is at-most-once; never retry uncertain Discord sends.
            db.executemany('UPDATE replies SET sent=1 WHERE id=?',[(r['id'],) for r in rows])
            return [dict(r) for r in rows]

    def take_notices(self):
        now=self.store.clock()
        with self.database() as db:
            rows=db.execute("SELECT * FROM requests WHERE notified=0 AND (phase IN ('refused','uncertain') OR (phase IN ('queued','delivered') AND expires<?)) LIMIT 32",(now,)).fetchall()
            db.executemany('UPDATE requests SET notified=1 WHERE id=?',[(r['id'],) for r in rows])
            return [{'channel':command(json.loads(r['payload']))['channel_id'],
                     'content':'Command was not confirmed by the current room; it will not be retried automatically. Check room status before trying again.'} for r in rows]
