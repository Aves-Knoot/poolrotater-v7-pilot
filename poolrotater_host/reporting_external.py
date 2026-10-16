"""Bounded fixed-unit journal and early worker stderr collection.

Only the independent root maintenance process reads the system journal. All
application-state writes occur as the dedicated UID, after source sanitization.
There are no provider calls, bot locks, room actions or shell commands here.
"""
import json
import logging
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

from .layout import atomic_json, contained, file_lease, load, read_json
from .diagnostic_logs import LocalLogs, Source
from .diagnostic_logs.common import transaction
from .network.contracts import require

_ALIAS=re.compile(r'room[0-9]+\Z')
_GENERATION=re.compile(r'[a-f0-9]{32}\Z')
_CURSOR=re.compile(r'[A-Za-z0-9;=:_-]{1,1024}\Z')
_CHAT=re.compile(r'(?im)(?:^CHAT\b|\b(?:INFO|DEBUG|WARNING|ERROR|CRITICAL)\s+CHAT\b|command from|public command suppressed|AI request queued|(?:received|sent|sending) (?:chat|message))')
MAX_ENTRIES=512
MAX_BYTES=4*1024**2
JOURNAL_TIMEOUT=4.0


def classify(text):
    return 'command' if re.search(r'(?i)command from|public command suppressed',text) else 'chat' if _CHAT.search(text) else 'operational'


def register_worker(root,alias,generation,version):
    """Witness consent before launch, so early stderr is collected from byte0."""
    require(bool(_ALIAS.fullmatch(alias)) and bool(_GENERATION.fullmatch(generation)), 'invalid_worker_log_binding')
    from .reporting import profile
    root=Path(root);marker,_=load(root);choice=profile(root)
    folder=contained(root,'state/reporting-worker-sources');folder.mkdir(mode=0o700,exist_ok=True)
    atomic_json(folder/(alias+'-'+generation+'.json'),{'format':1,
        **{k:marker[k] for k in ('installation_id','generation')},'alias':alias,
        'worker_generation':generation,'version':version,'at':time.time(),
        'capture_enabled_at_launch':choice['operational_logs']})


def read_journal(units,*,cursor,since):
    """Read a bounded forward page; stop/reap only our pidfd-owned journalctl."""
    require(bool(units) and len(units)<=4 and all(re.fullmatch(r'poolrotater-v7-(?:pilot|network|updates)-[a-z][a-z0-9-]{0,15}\.service',u) for u in units), 'invalid_journal_units')
    require(cursor is None or bool(_CURSOR.fullmatch(cursor)), 'invalid_journal_cursor')
    from .process_target import OwnedChild
    args=['/usr/bin/journalctl','--quiet','--no-pager','--output=json','--all','--no-tail',
        '--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP,_SYSTEMD_UNIT,_PID,UNIT']
    args += ['--unit='+unit for unit in units]
    args += ['--after-cursor='+cursor] if cursor else ['--since=@'+str(float(since))]
    child=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,cwd='/tmp',env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},start_new_session=True)
    owner=OwnedChild(child.pid);entries=[];last=cursor;buffer=b'';total=0;bounded=False
    selector=selectors.DefaultSelector();selector.register(child.stdout,selectors.EVENT_READ)
    deadline=time.monotonic()+JOURNAL_TIMEOUT
    try:
        finished=False
        while not finished and len(entries)<MAX_ENTRIES:
            if total>=MAX_BYTES:
                require(last!=cursor,'journal_page_too_large')
                bounded=True;break
            remaining=deadline-time.monotonic()
            require(remaining>0,'journal_read_timeout')
            ready=selector.select(min(.2,remaining))
            if not ready:continue
            raw=os.read(child.stdout.fileno(),min(65536,MAX_BYTES-total))
            if not raw:
                require(not buffer,'journal_reply_truncated');finished=True;break
            total+=len(raw)
            buffer+=raw
            while b'\n' in buffer and len(entries)<MAX_ENTRIES:
                line,buffer=buffer.split(b'\n',1)
                if not line:continue
                row=json.loads(line)
                require(type(row)is dict and type(row.get('__CURSOR'))is str
                    and bool(_CURSOR.fullmatch(row['__CURSOR'])),'invalid_journal_reply')
                last=row['__CURSOR']
                # journalctl -u also includes PID1's messages about that unit.
                selected=row.get('_SYSTEMD_UNIT') in units or (str(row.get('_PID'))=='1' and row.get('UNIT') in units)
                if not selected:continue
                message=row.get('MESSAGE','')
                if type(message)is list and all(type(v)is int and 0<=v<=255 for v in message):
                    message=bytes(message).decode('utf-8',errors='replace')
                require(type(message)is str,'invalid_journal_message')
                at=int(row['__REALTIME_TIMESTAMP'])/1000000
                unit=row.get('_SYSTEMD_UNIT') if row.get('_SYSTEMD_UNIT') in units else row['UNIT']
                entries.append({'at':at,'room':None,'kind':classify(message),'cursor':last,
                    'text':'['+unit+'] '+message})
            if len(entries)>=MAX_ENTRIES:bounded=True;break
        if not bounded:require(child.wait(timeout=1)==0,'journal_read_unavailable')
        return entries,last
    finally:
        selector.close();child.stdout.close()
        try:
            if child.poll() is None:
                owner.send(signal.SIGTERM)
                try:child.wait(timeout=.5)
                except subprocess.TimeoutExpired:owner.kill();child.wait(timeout=1)
        finally:owner.close()


def collect_workers(root,local,*,limit=16):
    root=Path(root);marker,_=load(root)
    folder=contained(root,'state/reporting-worker-sources')
    if not folder.exists():return {'sources':0,'bytes':0}
    paths=sorted(folder.glob('room*-*.json'))
    with transaction(local.path) as db:
        cursors={r['source']:r['offset'] for r in db.execute('SELECT source,offset FROM cursors')}
        saved=db.execute("SELECT value FROM settings WHERE key='stderr_scan_after'").fetchone()
        after=json.loads(saved[0]) if saved else ''
    require(type(after)is str and len(after)<=128,'invalid_worker_scan_cursor')
    paths=[p for p in paths if p.name>after]+[p for p in paths if p.name<=after]
    collected=0;consumed=0;errors=0;last=after
    support=local.support()
    for path in paths[:128]:
        last=path.name
        try:
            row=read_json(path,maximum=4096)
            require(type(row)is dict and set(row)=={'format','installation_id','generation','alias',
                'worker_generation','version','at','capture_enabled_at_launch'},'invalid_worker_log_witness')
            alias=row['alias'];generation=row['worker_generation']
            require(row['format']==1 and row['installation_id']==marker['installation_id']
                and row['generation']==marker['generation'] and bool(_ALIAS.fullmatch(alias))
                and bool(_GENERATION.fullmatch(generation)) and path.name==alias+'-'+generation+'.json'
                and type(row['capture_enabled_at_launch'])is bool,'invalid_worker_log_witness')
            name='stderr-'+alias+'-'+generation
            raw=contained(root,'logs/workers/'+alias+'-'+generation+'.log')
            if not raw.exists():continue
            if name in cursors and cursors[name]>=raw.stat().st_size:continue
            # A re-enable must not backfill an explicitly disabled interval.
            start_at_end=not row['capture_enabled_at_launch'] or row['at']<support['capture_since']
            source=Source(name,raw,room=alias,version=row['version'],start_at_end=start_at_end)
            reader=LocalLogs(root/'state',installation_id=marker['installation_id'],generation=marker['generation'],
                secret_provider=local.secret_provider,sources=(source,),full_stream=True,quota_bytes=local.quota)
            result=reader.collect(source);collected+=1;consumed+=result['read']
        except Exception:errors+=1
        if collected>=limit:break
    with transaction(local.path) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES('stderr_scan_after',?)",(json.dumps(last),))
    return {'sources':collected,'bytes':consumed,'unavailable':bool(errors)}


def collect(instance):
    """Called by root update ticks even when update installation is paused."""
    from .pilot_installation import administrative_binding
    from .ratings.operations import _identity
    from .reporting import logs,profile,CachedSecrets
    require(os.geteuid()==0,'journal_collector_requires_administrator')
    target,_,record=administrative_binding(instance);root=target['root'];uid=record['service_uid']
    with file_lease(target['prefix']/'diagnostics-collection.lock'):
        with _identity(uid):
            choice=profile(root);local=logs(root)
            local.configure(operational=choice['operational_logs'],public_chat=choice['public_chat'])
            if not choice['operational_logs']:return {'phase':'disabled'}
            local.secret_provider=CachedSecrets(root)
            cursor=local.external_cursor();since=local.support()['capture_since']
            resume_since=max(since,local.external_since()-1)
            try:workers=collect_workers(root,local)
            except Exception:workers={'sources':0,'bytes':0,'unavailable':True}
        units=(target['unit'],'poolrotater-v7-network-'+instance+'.service','poolrotater-v7-updates-'+instance+'.service')
        resumed=False
        try:entries,last=read_journal(units,cursor=cursor,since=resume_since)
        except Exception as error:
            from .network.contracts import NetworkError
            if cursor is None or not isinstance(error,NetworkError) or error.code!='journal_read_unavailable':raise
            # Journals may be vacuumed or volatile across reboots. Re-read a
            # short overlap from the last committed time, deduplicating by the
            # per-entry cursors retained atomically with the previous batch.
            entries,last=read_journal(units,cursor=None,since=resume_since);resumed=True
        with _identity(uid):
            current=profile(root)
            if not current['operational_logs']:return {'phase':'disabled'}
            local.configure(operational=True,public_chat=current['public_chat'])
            if last is not None or resumed:local.append_journal(entries,previous=cursor,cursor=last)
            if resumed:local.append('journal cursor unavailable; resumed from last committed timestamp; older source coverage may be unavailable',source='collector')
            phase='partial' if workers.get('unavailable') or resumed else 'collected'
            atomic_json(root/'state/reporting-external.json',{'format':1,'phase':phase,
                'at':time.time(),'journal_records':len(entries),'worker_sources':workers['sources'],
                'worker_bytes':workers['bytes'],'provider_dispatch':False})
        return {'phase':phase,'journal_records':len(entries),'worker_sources':workers['sources']}


def safely_collect(instance):
    try:return collect(instance)
    except Exception:
        # Fixed code only: journal messages and malformed source files can carry
        # private credentials. A collection failure never stops rooms or updates.
        try:
            from .pilot_installation import administrative_binding
            from .ratings.operations import _identity
            from .reporting import profile,logs
            target,_,record=administrative_binding(instance)
            with _identity(record['service_uid']):
                root=target['root']
                if profile(root)['operational_logs']:
                    logs(root).append('external log collection unavailable; inspect maintenance service',source='collector')
                    atomic_json(root/'state/reporting-external.json',{'format':1,'phase':'retrying',
                        'at':time.time(),'journal_records':0,'worker_sources':0,'worker_bytes':0,'provider_dispatch':False})
        except Exception:pass
        return {'phase':'retrying','error':'external_log_collection_unavailable'}
