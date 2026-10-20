"""Independent host relay. Network outages never reach the room tick/provider loop."""
from contextlib import contextmanager
import json
import logging
import os
import queue
import sqlite3
import stat
import threading
import time
import uuid
from ..network.client import Client
from ..network.contracts import fields, require
from ..layout import atomic_json, read_json
from .model import PATH, command

log=logging.getLogger('discord_relay')


class HostRelay:
    def __init__(self,supervisor,*,client=None,clock=time.time):
        self.owner=supervisor;self.root=supervisor.runtime;self.clock=clock
        self.client=client or Client(self.root)
        self.path=self.root/'state/discord-relay.sqlite3'
        if not os.path.lexists(self.path):
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600);os.close(fd)
        with self.database() as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,alias TEXT NOT NULL,room INTEGER NOT NULL,channel INTEGER NOT NULL,phase TEXT NOT NULL,expires REAL NOT NULL,ack INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,request TEXT NOT NULL,content TEXT NOT NULL)')
        self.thread=None

    @contextmanager
    def database(self):
        for suffix in ('','-wal','-shm','-journal'):
            p=self.path.with_name(self.path.name+suffix)
            if os.path.lexists(p):
                s=p.lstat();require(stat.S_ISREG(s.st_mode) and s.st_uid==os.geteuid() and s.st_nlink==1
                    and not s.st_mode&0o077 and s.st_size<=8*1024**2,'unsafe_relay_state')
        db=sqlite3.connect(self.path,timeout=.25,isolation_level=None);db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA max_page_count=2048');db.execute('BEGIN IMMEDIATE');yield db;db.execute('COMMIT')
        except BaseException:db.rollback();raise
        finally:db.close()

    def enabled(self):
        p=self.root/'config/discord-relay.json'
        if not os.path.lexists(p):return True
        value=read_json(p);fields(value,('format','enabled'))
        require(type(value['format'])is int and value['format']==1 and type(value['enabled'])is bool,'invalid_relay_policy')
        return value['enabled']

    def channels(self,alias):
        try:
            with self.database() as db:
                return {r[0] for r in db.execute('SELECT channel FROM requests WHERE alias=? AND expires>? AND phase IN (?,?)',(alias,self.clock()-120,'dispatched','pending'))}
        except Exception:
            return set()

    def reply(self,alias,channel,content):
        """Called by controller RPC, bounded local storage only; no HTTP or Discord."""
        try:
            if not self.enabled():return False
            with self.database() as db:
                row=db.execute("SELECT * FROM requests WHERE alias=? AND channel=? AND phase IN ('pending','dispatched') AND expires>? ORDER BY expires DESC LIMIT 1",(alias,channel,self.clock()-120)).fetchone()
                if row is None:return False
                if db.execute('SELECT count(*) FROM outbox').fetchone()[0]>=128:return False
                text=str(content)[:1800]
                # Never ship multiline control text or raw credentials/errors.
                from ..diagnostic_logs import scrub as redact
                text=redact(text)
                if not text:return False
                db.execute('INSERT INTO outbox VALUES(?,?,?)',(str(uuid.uuid4()),row['id'],text))
            return True
        except Exception:
            log.warning('remote Discord response refused; local delivery unavailable')
            return False

    def dispatch(self,value,credential):
        command(value);now=self.clock()
        require(all(value[k]==credential[k] for k in ('host_id','installation_id','generation')),'host_identity_mismatch')
        with self.database() as db:
            old=db.execute('SELECT phase FROM requests WHERE id=?',(value['id'],)).fetchone()
            if old:return old[0] if old[0]!='pending' else 'uncertain'
            # Intent precedes any queue mutation. A crash never replays commands.
            db.execute('INSERT INTO requests(id,alias,room,channel,phase,expires) VALUES(?,?,?,?,?,?)',(value['id'],value['alias'],value['room_id'],value['channel_id'],'pending',value['expires_at']))
        result='refused'
        try:
            if self.enabled() and now<value['expires_at']<=now+60 and not self.owner.stopping.is_set():
                alias=value['alias'];worker=self.owner.workers.get(alias)
                with self.owner.discord._view_lock:
                    view=self.owner.discord.view_for(alias)
                    if (worker is not None and worker.fresh() and worker.status.get('ready')is True
                        and worker.status.get('running')is True and not worker.status.get('paused')
                        and worker.status.get('room_id')==value['room_id']
                        and self.owner.rollout_alias!=alias and not view._stop.is_set()
                        and worker.broker.known and not worker.broker.uncertain
                        and value['room_id'] in worker.broker.hub.owned):
                        from bot.discord_control import DiscordCommand
                        view.enqueue_command(DiscordCommand(value['discord_user_id'],value['username'],value['role'],value['content'],value['channel_id']))
                        result='dispatched'
        except queue.Full:result='refused'
        except Exception:result='uncertain'
        with self.database() as db:db.execute('UPDATE requests SET phase=? WHERE id=?',(result,value['id']))
        return result

    def step(self):
        if not self.enabled():return 'disabled'
        status=self.client.status()
        if not status.get('enrolled') or status.get('pending_operation') or status.get('account_binding_matches')is False:return 'not-enrolled'
        profile=self.client.profile();self.client.check_account(profile['account_id']);credential=self.client.credential()
        require(credential is not None and credential['credential_expires_at']>self.clock(),'credential_expired')
        with self.database() as db:
            db.execute('DELETE FROM outbox WHERE request IN (SELECT id FROM requests WHERE expires<?)',(self.clock()-120,))
            db.execute('DELETE FROM requests WHERE expires<?',(self.clock()-300,))
            receipts=[{'id':r['id'],'result':'uncertain' if r['phase']=='pending' else r['phase']} for r in db.execute("SELECT * FROM requests WHERE ack=0 ORDER BY expires,id LIMIT 16")]
            replies=[{'id':r['id'],'request_id':r['request'],'content':r['content']} for r in db.execute('SELECT * FROM outbox ORDER BY id LIMIT 16')]
        value={'protocol_version':1,**self.client.marker,'sent_at':self.clock(),'receipts':receipts,'replies':replies};value.pop('format',None)
        response=self.client.transport(profile).request('POST',PATH,body=value,token=credential['credential'])
        fields(response,('ok','requests'));require(type(response['requests'])is list and len(response['requests'])<=8,'invalid_request')
        # Validate the whole batch before admitting any item.
        for row in response['requests']:
            command(row);require(all(row[k]==credential[k] for k in ('host_id','installation_id','generation')),'host_identity_mismatch')
        with self.database() as db:
            # Keep dispatched routing until expiry while marking acknowledgments separately.
            for r in receipts:
                db.execute('UPDATE requests SET ack=1 WHERE id=?',(r['id'],))
            db.executemany('DELETE FROM outbox WHERE id=?',[(r['id'],) for r in replies])
            db.execute('DELETE FROM requests WHERE expires<?',(self.clock()-300,))
        for row in response['requests']:self.dispatch(row,credential)
        return 'connected'

    def run(self):
        delay=5
        while not self.owner.stopping.is_set():
            error=None
            try:phase=self.step();delay=5 if phase=='connected' else 60
            except Exception as exc:
                phase='unavailable';error='relay_unavailable';delay=min(60,max(10,delay*2))
            try:atomic_json(self.root/'state/discord-relay.json',{'format':1,'phase':phase,'error':error,'observed_at':self.clock()})
            except Exception:pass
            self.owner.stopping.wait(delay)

    def start(self):
        self.thread=threading.Thread(target=self.run,name='discord-relay',daemon=True);self.thread.start()

    def stop(self):
        if self.thread:self.thread.join(timeout=7)
