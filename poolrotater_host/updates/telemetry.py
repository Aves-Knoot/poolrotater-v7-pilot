"""Closed minimal authenticated update receipts, separate from room/player data."""
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager

from ..network.contracts import (require,fields,integer,number,canonical,verifier,identifier)

PHASES=('idle','available','downloaded','staging','dispatching','waiting_empty','installed',
        'held','retrying','paused','controller_consent_required')
ERRORS=(None,'update_installation_requires_review','update_deployment_receipt_missing',
        'update_other_deployment_pending','update_check_or_download_failed','update_host_not_running','update_release_requires_manual_retry')


def report(value):
    fields(value,('protocol_version','sequence','sent_at','channel','phase','paused','installed_version',
                  'available_version','available_kind','download_bytes','download_total','waiting_rooms','error'))
    require(type(value['protocol_version'])is int and value['protocol_version']==1,'unsupported_protocol')
    integer(value['sequence']);number(value['sent_at'])
    require(value['channel']in('pilot','stable')and value['phase']in PHASES,'invalid_request')
    require(type(value['paused'])is bool and value['available_kind']in(None,'worker','controller')
            and value['error']in ERRORS,'invalid_request')
    import re
    for name in ('installed_version','available_version'):
        require(value[name]is None or type(value[name])is str and re.fullmatch(r'v[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}',value[name]),'invalid_request')
    integer(value['download_total'],0,128*1024**2);integer(value['download_bytes'],0,value['download_total'])
    integer(value['waiting_rooms'],0,50)
    return value


def projection(summary,*,sequence,now):
    download=summary.get('download')or{}
    return report({'protocol_version':1,'sequence':sequence,'sent_at':now,
        'channel':summary['channel'],'phase':summary['phase'],'paused':summary['paused'],
        'installed_version':summary['installed_version'],'available_version':summary['available_version'],
        'available_kind':summary['available_kind'],'download_bytes':download.get('bytes',0),
        'download_total':download.get('total',0),'waiting_rooms':len(summary['waiting_rooms']),
        'error':summary['error']})


class Reports:
    def __init__(self,store):
        self.store=store;self.path=store.root/'updates.sqlite3'
        if not os.path.lexists(self.path):
            fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
        self._check()
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reports(host_id TEXT PRIMARY KEY,sequence INTEGER NOT NULL,payload_hash TEXT NOT NULL,payload TEXT NOT NULL,received REAL NOT NULL)')

    def _check(self):
        for suffix in ('','-journal','-wal','-shm'):
            path=self.path.with_name(self.path.name+suffix)
            if not os.path.lexists(path):continue
            info=path.lstat()
            require(stat.S_ISREG(info.st_mode)and info.st_uid==os.geteuid()and info.st_nlink==1
                    and not info.st_mode&0o077 and info.st_size<=16*1024**2,'hub_storage_unavailable',503)

    @contextmanager
    def _db(self):
        self._check();db=sqlite3.connect(self.path,timeout=2)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA busy_timeout=2000');db.execute('PRAGMA max_page_count=4096')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback();raise
        finally:db.close()

    def accept(self,token,value):
        report(value);digest=hashlib.sha256(canonical(value)).hexdigest();now=self.store.clock()
        require(abs(value['sent_at']-now)<=300,'invalid_request')
        with self.store.transaction()as authority:
            host,_=self.store._authenticate(authority,verifier(token),'heartbeat')
            with self._db()as db:
                db.execute('DELETE FROM reports WHERE received<?',(now-7*86400,))
                previous=db.execute('SELECT * FROM reports WHERE host_id=?',(host['id'],)).fetchone()
                if previous is not None:
                    require(value['sequence']>=previous['sequence'],'stale_sequence',409)
                    if value['sequence']==previous['sequence']:
                        require(digest==previous['payload_hash'],'sequence_conflict',409)
                        return {'ok':True,'sequence':value['sequence'],'received_at':previous['received'],'replayed':True}
                    require(now-previous['received']>=5,'heartbeat_rate_limited',429)
                db.execute('INSERT OR REPLACE INTO reports VALUES(?,?,?,?,?)',
                    (host['id'],value['sequence'],digest,canonical(value).decode(),now))
        return {'ok':True,'sequence':value['sequence'],'received_at':now,'replayed':False}

    def local(self,host_id):
        identifier(host_id)
        with self._db()as db:row=db.execute('SELECT * FROM reports WHERE host_id=?',(host_id,)).fetchone()
        return {'ok':True,'report':json.loads(row['payload'])if row else None,
                'received_at':row['received']if row else None,
                'stale':row is None or self.store.clock()-row['received']>3600}
