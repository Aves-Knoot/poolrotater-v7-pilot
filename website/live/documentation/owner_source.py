"""Staged operational-reader binding. Legacy defaults remain unchanged.

Explicit owner selection is a fixed namespace, never arbitrary filesystem roots.
This helper reads only declared public/config projection fields; it has no bot
imports, provider calls, credential access, service control or source writes.
Deploying it and altering units require the separately approved cutover manifest.
"""
from dataclasses import dataclass
import json
import math
import os
import hashlib
import datetime as dt
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

SELECTOR='POOLROTATER_OWNER_OPERATIONS_INSTANCE'
OWNER_ROOT=Path('/var/lib/poolrotater-v7-pilot/owner')
OWNER_PREFIX=Path('/opt/poolrotater-v7-pilot/owner')


@dataclass(frozen=True)
class Source:
    owner: bool
    root: Path
    service: str
    prefix: Path|None=None

    @property
    def ledger(self):return self.root/('state/controller-ratings.db' if self.owner else 'state/ratings.db')
    @property
    def api(self):return self.root/('state/provider-usage.db' if self.owner else 'state/api_rate.db')
    @property
    def discord_health(self):return self.root/('state/controller-discord-health.json' if self.owner else 'state/discord_control_health.json')
    @property
    def shared_log(self):return self.root/'logs/multiroom.log'
    def room_log(self,alias):
        if not re.fullmatch('room[1-4]',alias):raise ValueError('invalid room alias')
        return self.root/'logs'/((('room-' if self.owner else '')+alias)+'.log')
    def config(self,alias):
        if alias!='universal' and not re.fullmatch('room[1-4]',alias):raise ValueError('invalid config alias')
        return self.root/('config/rooms' if self.owner else 'instances')/(alias+'.json')
    def catalog(self,filename):
        if not self.owner:return self.root/'data'/filename
        choices={'farm.db':('room1','farm_db'),'mania.db':('room4','mania_db'),
                 'mania_7k.db':('room3','mania_db'),'loved.db':('room1','loved_db')}
        if filename not in choices:raise ValueError('unknown catalog role')
        alias,key=choices[filename]
        value=bounded_json(self.config(alias)).get(key)
        if not isinstance(value,str) or not value:raise ValueError('catalog role unavailable')
        candidate=Path(value)
        store=self.prefix/'catalog-stores'
        try:parts=candidate.relative_to(store).parts
        except ValueError:raise ValueError('catalog outside protected namespace') from None
        if (len(parts)!=4 or not re.fullmatch('[a-f0-9-]{36}',parts[0]) or parts[1]!='versions'
                or not re.fullmatch('[a-f0-9]{64}',parts[2])
                or parts[3] not in ('catalog.db','farm.db','dt.db','converts.db')):
            raise ValueError('invalid catalog identity')
        for ancestor in (store,*candidate.parents):
            if ancestor.is_relative_to(store) and ancestor.is_symlink():raise ValueError('catalog symlink refused')
        info=candidate.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o222 or info.st_nlink!=1:
            raise ValueError('catalog is not protected immutable public data')
        return candidate


def selected(environment=None):
    environment=os.environ if environment is None else environment
    instance=environment.get(SELECTOR)
    if instance is None:return Source(False,Path(environment.get('POOLROTATOR_ROOT','/opt/poolrotator-runtime')),'poolrotator.service')
    if instance!='owner':raise ValueError('operational instance must be explicitly owner')
    # Mixing a legacy root override into owner activation is an operator error.
    if 'POOLROTATOR_ROOT' in environment:raise ValueError('legacy root override conflicts with owner source')
    return Source(True,OWNER_ROOT,'poolrotater-v7-pilot-owner.service',OWNER_PREFIX)


def wiki_commit_argv(source, script, pending):
    """Root verifies owner authority; native wiki revisions keep their old UID."""
    command=['/usr/bin/php',str(script),str(pending)]
    if not source.owner:return command
    if (os.geteuid()!=0 or Path(script)!=Path('/srv/poolrotator-wiki/documentation/commit.php')
            or Path(pending)!=Path('/var/lib/poolrotator-wiki/documentation/pending.json')):
        raise ValueError('owner documentation publication requires its fixed privileged reader')
    import pwd
    account=pwd.getpwnam('poolrotator-wiki')
    if account.pw_uid<=0:raise ValueError('native wiki writer identity unavailable')
    return ['/usr/sbin/runuser','-u','poolrotator-wiki','--',*command]


def bounded_json(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>256*1024:raise ValueError('projection exceeds bound')
        body=stream.read(256*1024+1)
        after=os.fstat(stream.fileno())
    if len(body)>256*1024 or (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
        raise ValueError('projection changed during read')
    value=json.loads(body)
    if not isinstance(value,dict):raise ValueError('projection object required')
    return value


def code_evidence(source,proof):
    """Verify fresh installed code proof before treating files as source authority."""
    required={'format','kind','ok','version','execution','runtime_sha256','environment_id',
        'source_files_sha256','source_files','protected_runtime_checked','receipt_scope',
        'operational_acceptance_verified','provider_dispatch','live_ready'}
    if (not source.owner or type(proof) is not dict or set(proof)!=required
            or type(proof['format']) is not int or proof['format']!=1 or proof['kind']!='protected-v7-code-evidence'
            or proof['ok'] is not True or proof['protected_runtime_checked'] is not True
            or proof['receipt_scope']!='registered_code_and_environment_only'
            or proof['operational_acceptance_verified'] is not False
            or proof['provider_dispatch'] is not False or proof['live_ready'] is not False
            or proof['execution']!='pilot-live'
            or type(proof['version']) is not str or not re.fullmatch(r'v7\.\d+\.\d+',proof['version'])):
        raise ValueError('registered code proof refused')
    for key in ('runtime_sha256','environment_id','source_files_sha256'):
        if not isinstance(proof[key],str) or not re.fullmatch('[0-9a-f]{64}',proof[key]):raise ValueError('invalid code identity')
    files=proof['source_files']
    if type(files) is not dict or not 1<=len(files)<=3000:raise ValueError('invalid public source inventory')
    for name,digest in files.items():
        if (not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_./-]+',name)
                or Path(name).is_absolute() or '..' in Path(name).parts
                or any(word in name.lower() for word in ('secret','oauth.json','token.json','.env'))
                or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest)):
            raise ValueError('invalid public source entry')
    encoded=(json.dumps(files,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
    if hashlib.sha256(encoded).hexdigest()!=proof['source_files_sha256']:raise ValueError('code inventory identity mismatch')
    release=source.prefix/'releases'/(proof['version']+'-'+proof['runtime_sha256'][:16])
    for parent in (release,release.parent,source.prefix):
        info=parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:raise ValueError('mutable code namespace')
    for name,expected in files.items():
        path=release/name
        if path.resolve()!=path.absolute():raise ValueError('code path symlink refused')
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or info.st_size>8*1024**2:
            raise ValueError('mutable code source')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('code source changed')
    return release


def _binding_json(path,uid,*,private=False):
    """Read one binding once; a service pointer is not root code authority."""
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=uid
                or before.st_nlink!=1 or before.st_size>256*1024
                or stat.S_IMODE(before.st_mode)&(0o077 if private else 0o022)):
            raise ValueError('runtime binding owner/type/permissions refused')
        raw=stream.read(256*1024+1);after=os.fstat(stream.fileno())
    if (len(raw)>256*1024 or
            (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=
            (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)):
        raise ValueError('runtime binding changed during read')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('duplicate runtime binding field')
            value[key]=item
        return value
    value=json.loads(raw,object_pairs_hook=unique,
        parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid runtime binding number')))
    if not isinstance(value,dict):raise ValueError('runtime binding object required')
    return value


def registered_release(source):
    """Resolve the native UID-owned pointer through its root-owned registration."""
    if (not source.owner or source.prefix is None
            or not source.root.is_absolute() or not source.prefix.is_absolute()
            or '..' in source.root.parts or '..' in source.prefix.parts
            or source.root.resolve()!=source.root.absolute()
            or source.root.name!=source.prefix.name
            or not re.fullmatch('[a-z][a-z0-9-]{0,15}',source.root.name)):
        raise ValueError('registered operational namespace mismatch')
    owner=source.root.lstat()
    if not stat.S_ISDIR(owner.st_mode) or owner.st_uid<=0 or owner.st_mode&0o077:
        raise ValueError('private operational namespace refused')
    state=(source.root/'state').lstat()
    if not stat.S_ISDIR(state.st_mode) or state.st_uid!=owner.st_uid or state.st_mode&0o077:
        raise ValueError('private operational state refused')
    marker=_binding_json(source.root/'.poolrotater-host.json',owner.st_uid,private=True)
    if set(marker)!= {'format','installation_id','generation'} or type(marker['format']) is not int or marker['format']!=1:
        raise ValueError('runtime installation identity refused')
    for key in ('installation_id','generation'):
        if type(marker[key]) is not str or str(uuid.UUID(marker[key]))!=marker[key]:
            raise ValueError('runtime installation UUID refused')
    pointer=_binding_json(source.root/'state/runtime-installation.json',owner.st_uid,private=True)
    expected={**marker,'prefix':str(source.prefix)}
    if pointer!=expected or type(pointer.get('format')) is not int:
        raise ValueError('runtime pointer binding refused')
    for parent in (source.prefix,*source.prefix.parents):
        info=parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022):
            raise ValueError('runtime registration namespace is not protected')
    directory=source.prefix/'installations';info=directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
        raise ValueError('runtime registration directory is not protected')
    record_path=directory/(marker['installation_id']+'.json')
    record=_binding_json(record_path,0)
    fields={'format','installation_id','generation','prefix','execution','live_ready',
            'service_uid','host_root','release','environment','bundle_sha256',
            'environment_id','release_snapshot'}
    if (set(record)!=fields or any(record[k]!=v for k,v in expected.items())
            or type(record['format']) is not int or record['execution']!='pilot-live'
            or record['live_ready'] is not False or type(record['service_uid']) is not int
            or record['service_uid']!=owner.st_uid or record['host_root']!=str(source.root)):
        raise ValueError('protected runtime registration binding refused')
    for key in ('bundle_sha256','environment_id'):
        if type(record[key]) is not str or not re.fullmatch('[a-f0-9]{64}',record[key]):
            raise ValueError('protected runtime identity refused')
    release=Path(record.get('release',''))
    if (release.parent!=source.prefix/'releases' or
            not re.fullmatch(r'v7\.\d+\.\d+-'+record['bundle_sha256'][:16],release.name)
            or Path(record['environment'])!=source.prefix/'environments'/record['environment_id']):
        raise ValueError('registered source outside owner namespace')
    for parent in (release,release.parent,release/'poolrotater_host'):
        info=parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('registered code namespace is mutable')
    return record,release


def fresh_code_proof(source):
    """Fixed protected admin read-only action; no arbitrary executable or secrets."""
    record,release=registered_release(source)
    entry=release/'poolrotater_host/pilot_admin_entry.py'
    info=entry.lstat()
    if (entry.resolve()!=entry.absolute() or not stat.S_ISREG(info.st_mode)
            or info.st_uid!=0 or info.st_mode&0o022 or info.st_nlink!=1
            or info.st_size>131072):
        raise ValueError('registered entry is mutable')
    # Production selected() admits owner only; an installed donor can bind its
    # own fixed protected Source for qualification without impersonating owner.
    if source.root.name!=source.prefix.name or not re.fullmatch('[a-z][a-z0-9-]{0,15}',source.root.name):
        raise ValueError('registered operational namespace mismatch')
    result=subprocess.run(['/usr/bin/python3.12','-I','-S',str(entry),'--instance',source.root.name,'code-proof'],
        capture_output=True,text=True,timeout=30,check=False,
        env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
    if result.returncode or len(result.stdout)>2*1024**2:raise ValueError('registered code proof unavailable')
    proof=json.loads(result.stdout)
    if (type(proof) is not dict or proof.get('runtime_sha256')!=record['bundle_sha256']
            or proof.get('environment_id')!=record['environment_id']
            or registered_release(source)!=(record,release)):
        raise ValueError('registered code proof binding changed')
    if code_evidence(source,proof)!=release:raise ValueError('registered source changed')
    return proof,release


def worker_evidence(source,proof,now=None):
    now=time.time() if now is None else now
    feed=bounded_json(source.root/'state/rolling/public-status.json')
    rows=feed.get('rooms')
    record,_=registered_release(source)
    config=_binding_json(source.root/'config/host.json',record['service_uid'],private=True)
    configured=config.get('rooms')
    if (not isinstance(configured,list) or not 1<=len(configured)<=50
            or any(not isinstance(r,dict) or not isinstance(r.get('alias'),str) for r in configured)
            or len({r['alias'] for r in configured})!=len(configured)):
        raise ValueError('owner configured room evidence invalid')
    aliases={r['alias'] for r in configured}
    if (type(feed.get('generated_at')) not in (int,float)
            or not -5<=now-feed['generated_at']<=20
            or feed.get('supervisor_version')!=proof['version']
            or type(rows) is not dict or set(rows)!=aliases):
        raise ValueError('owner worker source evidence stale or incomplete')
    for row in rows.values():
        if (type(row) is not dict or row.get('version')!=proof['version']
                or row.get('healthy') is not True or type(row.get('observed_at')) not in (int,float)
                or not -5<=now-row['observed_at']<=20):raise ValueError('owner worker source is not qualified')
    return {alias:row['version'] for alias,row in rows.items()}


def documentation_prepare(module,source):
    """Reuse old semantic validator/publisher while changing source proof family."""
    observed=dict(schema=1,observed_at=int(time.time()),active_version=None,confirmed=False,
        ready=False,reason='registered_v7_source_or_manifest_unavailable',
        source_kind='owner_v7_registered',operational_acceptance_verified=False)
    try:
        proof,release=fresh_code_proof(source);versions=worker_evidence(source,proof)
        observed.update(active_version=proof['version'],confirmed=True,room_versions=versions,
            runtime_sha256=proof['runtime_sha256'],source_files_sha256=proof['source_files_sha256'])
        path=release/'docs/wiki-manifest.json'
        if not path.exists():path=module.MANIFESTS/(proof['version']+'.json')
        if path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode&0o022:
            raise ValueError('v7 source manifest must be protected reviewed data')
        manifest=module.validate(module.readjson(path),release)
        again,again_release=fresh_code_proof(source)
        if again!=proof or again_release!=release:raise ValueError('registered source changed during manifest read')
        worker_evidence(source,proof)
        manifest_digest=module.digest(json.dumps(manifest,sort_keys=True).encode())
        candidate=dict(schema=1,version=proof['version'],source_kind='owner_v7_registered',
            event_id=module.digest((proof['runtime_sha256']+manifest_digest).encode()),
            runtime_sha256=proof['runtime_sha256'],source_files_sha256=proof['source_files_sha256'],
            manifest_digest=manifest_digest,manifest=manifest)
        module.atomic(module.FILES/'candidate.json',candidate)
        observed.update(ready=True,reason='registered_code_and_current_workers_verified',
            manifest_digest=manifest_digest,reviewed_on=manifest['reviewed_on'])
    except (OSError,ValueError,KeyError,TypeError,AttributeError,SyntaxError,subprocess.SubprocessError):
        (module.FILES/'candidate.json').unlink(missing_ok=True)
    module.atomic(module.FILES/'release.json',observed)
    print(json.dumps({key:observed[key] for key in ('active_version','confirmed','ready','reason')}))


def documentation_release(source,candidate):
    proof,release=fresh_code_proof(source);worker_evidence(source,proof)
    if (candidate.get('source_kind')!='owner_v7_registered' or candidate.get('version')!=proof['version']
            or candidate.get('runtime_sha256')!=proof['runtime_sha256']
            or candidate.get('source_files_sha256')!=proof['source_files_sha256']):
        raise ValueError('registered docs source changed before publication')
    return release


def release_version(source,release):
    if not source.owner:return release.name
    if release.parent!=source.prefix/'releases' or not re.fullmatch(r'v7\.\d+\.\d+-[0-9a-f]{16}',release.name):
        raise ValueError('invalid registered release version')
    return release.name.rsplit('-',1)[0]


def provider_metrics(connection, source, since):
    """Actual window + durable authority totals, with explicit observation gaps.

    Do not expose source_ledger hashes, arbitrary SQL, accounts or credentials.
    The connection is supplied by the existing owner-identity read-only helper.
    """
    path=source.root/'state/provider-usage-health.json'
    try:health=bounded_json(path)
    except FileNotFoundError:
        # Healthy telemetry need not create a failure marker. A dangling link
        # or missing parent is not that normal absence and must still refuse.
        if path.is_symlink() or not path.parent.is_dir():raise
        health={'format':1,'failures':0,'last_failure_at':None}
    if (set(health)!= {'format','failures','last_failure_at'}
            or type(health.get('format')) is not int or health['format']!=1
            or type(health.get('failures')) is not int
            or not 0<=health['failures']<2**63):raise ValueError('invalid telemetry health')
    stamp=health['last_failure_at']
    if stamp is not None and (type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<0):
        raise ValueError('invalid telemetry failure timestamp')
    rows=connection.execute('SELECT started_at,counters,gaps FROM usage_epochs ORDER BY started_at LIMIT 65').fetchall()
    if len(rows)>64:raise ValueError('epoch observation budget exceeded')
    keys={'attempted','dispatched','replies','rate_limited','uncertain','deferred'}
    totals={key:0 for key in sorted(keys)}
    gaps=health['failures']>0
    for started,counters,gap in rows:
        if (type(started) not in (int,float) or not math.isfinite(started) or started<0
                or type(gap) is not int or gap<0 or len(counters)>2048):raise ValueError('invalid telemetry epoch')
        counts=json.loads(counters)
        if set(counts)!=keys or any(type(n) is not int or not 0<=n<2**63 for n in counts.values()):
            raise ValueError('invalid authority counter projection')
        for key in totals:totals[key]+=counts[key]
        gaps=gaps or gap>0
    discarded=float(connection.execute("SELECT value FROM usage_meta WHERE key='discarded_through'").fetchone()[0])
    if not math.isfinite(discarded):raise ValueError('invalid discarded observation boundary')
    attempts,limited=connection.execute('SELECT COUNT(*),COALESCE(SUM(status_code=429),0) FROM api_request_events WHERE requested_at>=?',(since,)).fetchone()
    unfinished=connection.execute('SELECT COUNT(*) FROM api_request_events WHERE requested_at>=? AND status_code IS NULL',(since,)).fetchone()[0]
    return {'requests':attempts,'http_429':limited,
        'source':'provider-usage.db; actual provider HTTP; separate v7 epoch',
        'complete':bool(rows) and not gaps and not unfinished and rows[0][0]<=since and discarded<since,
        'unfinished_or_uncertain_exchanges':unfinished,
        'observation_started_at':rows[0][0] if rows else None,'legacy_epoch_included':False,
        'telemetry_gap':gaps,'overall_provider_counters':totals}


def usage_snapshot(connection,source,days=7,*,now=None):
    """Read durable v7 lifetime/daily rollups; preserve v6 archive separately."""
    if type(days) is not int or not 1<=days<=366:raise ValueError('v7 daily observation range is 1..366 days')
    now=time.time() if now is None else now
    window=provider_metrics(connection,source,now-14400)
    metrics=('http_started','http_replied','http_429','http_uncertain','delayed_reservations',
        'scheduled_seconds','max_scheduled_seconds','observed_elapsed_seconds')
    query=','.join(('MAX' if name=='max_scheduled_seconds' else 'SUM')+'('+name+')' for name in metrics)
    total=connection.execute('SELECT '+query+' FROM usage_epochs').fetchone()
    overall={name:(value or 0) for name,value in zip(metrics,total)}
    days_rows=connection.execute('SELECT day,'+query+' FROM usage_daily GROUP BY day ORDER BY day DESC LIMIT ?',(days,)).fetchall()
    daily=[]
    for row in days_rows:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',row[0]):raise ValueError('invalid telemetry UTCday')
        stats={name:(value or 0) for name,value in zip(metrics,row[1:])}
        daily.append({'day':row[0],**stats,'attempts':stats['http_started'],
            'scheduled_wait_seconds':stats['scheduled_seconds'],
            'max_scheduled_wait_seconds':stats['max_scheduled_seconds']})
    overall.update(attempts=overall['http_started'],scheduled_wait_seconds=overall['scheduled_seconds'],
        max_scheduled_wait_seconds=overall['max_scheduled_seconds'])
    return {'scope':'owner v7 observed provider HTTP including referee negotiate/upgrade; excludes websocket frames, collectors and Discord',
        'updated_at':dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),
        'http_history_started_at':window['observation_started_at'],
        'legacy_epoch_included':False,'legacy_history_retained_separately':True,
        'local_delay_definition':'actual scheduled admission waits; scheduled and observed elapsed time reported separately',
        'possible_coverage_gaps':int(window['telemetry_gap']),
        'unfinished_or_uncertain_exchanges':window['unfinished_or_uncertain_exchanges'],
        'retention_seconds':21600,'daily_retention_days':366,
        'overall':overall,'daily_utc':daily,
        'overall_provider_counters':window['overall_provider_counters']}


def usage_watch(module,source,source_path,db,days):
    """Observation-only watcher: telemetry is durably written by provider events."""
    if Path(source_path)!=source.api or Path(db)!=source.api:
        raise ValueError('v7 watcher requires fixed observed provider store; legacy destinations must be reviewed')
    stopped=False
    def stop(*args):
        nonlocal stopped
        stopped=True
    module.signal.signal(module.signal.SIGTERM,stop);module.signal.signal(module.signal.SIGINT,stop)
    while not stopped:
        try:
            with module.readonly_database(source.api,timeout=1) as connection:
                print(json.dumps(usage_snapshot(connection,source,days),sort_keys=True),flush=True)
        except (OSError,ValueError,TypeError,module.sqlite3.Error):
            print(json.dumps({'telemetry_error':'provider_observation_unavailable','legacy_epoch_included':False}),flush=True)
        # Keep signal responsiveness and bounded reporter polling, no new writer.
        for _ in range(60):
            if stopped:break
            time.sleep(1)
