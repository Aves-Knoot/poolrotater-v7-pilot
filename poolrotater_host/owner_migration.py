"""Stopped owner adoption primitives for explicitly supplied OFFLINE copies.

This is not a deployment command. It never discovers an owner installation,
reads OAuth/env files, stops services or contacts a provider. Its caller must
establish stopped writers and review a complete private snapshot enumeration.
All publication is exclusive and incomplete imports fence native startup.
"""
from __future__ import annotations

import copy
import ast
from contextlib import nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from urllib.parse import quote

from .config import ConfigError, contained, fields
from .layout import atomic_json, load, owner_lease, read_json
from . import owner_integrations as integrations, owner_settings as gameplay
from .ratings import migration

RECORD = 'state/owner-migration.json'
MANIFEST = 'snapshot.json'
CONFIG = 'source-config.json'
PHASES = ('prepared', 'files_imported', 'ledger_migrated', 'committed')
MAX_STATE_FILES=100000
MAX_STATE_BYTES=4*1024**3
PP_CACHE_SUFFIX=r'(?:osu/[1-9][0-9]{0,9}\.osu|(?:results|results_mania)/[1-9][0-9]{0,9}__[A-Z0-9]+__[0-9p-]+\.json)'
LANGUAGE_OFFERS='state/player-language-v1/offers.json'
ADDITIONS = frozenset({'ratings_schema_prepared','ratings_reader_mode','ratings_reader_root',
                      'ratings_reader_epoch','standard_catalog_binding','standard_dt_db'})
OBSERVER_KEYS = ('discord_control_health_state_file', 'discord_public_status_state_file')
OBSERVER_PATHS = frozenset(integrations.PATHS[key] for key in OBSERVER_KEYS)


def _offline_path(path, *, directory=False):
    path=Path(path).absolute()
    # Until a separately reviewed stopped production snapshot command exists,
    # these primitives cannot be pointed at running deployment roots.
    for prefix in ('/opt/poolrotator','/opt/poolrotator-runtime','/opt/poolrotator-releases'):
        if path==Path(prefix) or path.is_relative_to(prefix):
            raise ConfigError('owner migration requires explicitly isolated copies')
    migration._private(path,directory=directory)
    return path


def _identity(path):
    path=_offline_path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
        after=os.fstat(stream.fileno())
    if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
        raise ConfigError('owner snapshot input changed during read')
    return {'sha256':digest,'bytes':after.st_size}


def _observer_bytes(root, relative):
    """Preserve only declared native observer JSON; its writer may use0640."""
    root = _offline_path(root, directory=True)
    if relative not in OBSERVER_PATHS:
        raise ConfigError('owner rollback observer path not allowlisted')
    record = integrations.load(root, *load(root))
    key = next(key for key in OBSERVER_KEYS if integrations.PATHS[key] == relative)
    if record is None or not any(row['paths'][key] for row in record['rooms'].values()):
        raise ConfigError('owner rollback observer path not declared')
    path = contained(root, relative)
    parents = (root, path.parent)
    parent_identities = []
    for parent in parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ConfigError('owner rollback observer private ancestry required')
        parent_identities.append((info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid))
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    def check(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_gid != os.getegid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) not in (0o600, 0o640)
                or info.st_size > 1024*1024):
            raise ConfigError('owner rollback observer metadata invalid')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno()); check(before)
        raw = stream.read(1024*1024+1)
        after = os.fstat(stream.fileno()); check(after)
        current = path.lstat(); check(current)
        if (len(raw) != before.st_size or identity(before) != identity(after)
                or identity(before) != identity(current)):
            raise ConfigError('owner rollback observer changed during read')
        if parent_identities != [(info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid)
                                 for parent in parents for info in (parent.lstat(),)]:
            raise ConfigError('owner rollback observer ancestry changed during read')
    from .layout import unique_fields
    def invalid_number(_): raise ConfigError('owner rollback observer nonfinite JSON')
    def finite_float(text):
        value = float(text)
        if not math.isfinite(value): invalid_number(text)
        return value
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_fields,
                           parse_constant=invalid_number, parse_float=finite_float)
    except (ValueError, UnicodeError) as exc:
        raise ConfigError('owner rollback observer JSON invalid') from exc
    if not isinstance(value, dict): raise ConfigError('owner rollback observer JSON object required')
    return raw


def _publish(source,destination,expected):
    """Exclusive durable copy. A crash after publication resumes by identity."""
    source=_offline_path(source);destination=Path(destination)
    _offline_path(destination.parent,directory=True)
    if os.path.lexists(destination):
        if _identity(destination)!=expected:raise ConfigError('owner import existing file differs')
        return
    temporary=None
    try:
        fd,name=tempfile.mkstemp(prefix='.owner-copy-',dir=destination.parent)
        temporary=Path(name);os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as target:
            fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,'rb') as origin:
                while block:=origin.read(1024*1024):target.write(block)
            target.flush();os.fsync(target.fileno())
        if _identity(temporary)!=expected:raise ConfigError('owner snapshot input changed during copy')
        os.link(temporary,destination)
        fd=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def _state_target(value):
    if type(value) is not str:raise ConfigError('owner snapshot state target invalid')
    # Only native private policies, preferences, integration state and PP cache.
    # Credentials, ownership journals, process locks and OAuth are never copied.
    fixed=set(integrations.PATHS.values())-{'config/ai-context-{alias}.md','state/controller-map-pp'}
    valid=(value in fixed or value==LANGUAGE_OFFERS or re.fullmatch(r'config/ai-context-[a-z][a-z0-9_-]{0,23}\.md',value)
           or re.fullmatch(r'state/player-language-v1/[1-9][0-9]{0,18}\.json',value)
           or re.fullmatch(r'state/controller-map-pp/'+PP_CACHE_SUFFIX,value))
    if not valid:raise ConfigError('owner snapshot state target not allowlisted')
    return value


def _state_check(target,path):
    if target==LANGUAGE_OFFERS:
        value=read_json(path,maximum=2048)
        fields(value,('format','at'),'owner language offer snapshot')
        if (type(value['format']) is not int or value['format']!=1
                or type(value['at']) not in (int,float) or not math.isfinite(value['at'])
                or value['at']<0):
            raise ConfigError('owner language offer snapshot invalid')
    elif target.startswith('state/player-language-v1/'):
        from bot.languages import LANGUAGES
        value=read_json(path,maximum=2048)
        fields(value,('format','language','offered'),'owner language snapshot')
        if type(value['format']) is not int or value['format']!=1 or value['language'] not in (None,*LANGUAGES) or type(value['offered']) is not bool:
            raise ConfigError('owner language snapshot invalid')
    elif target=='state/moderation/bans.json':
        value=read_json(path,maximum=8*1024*1024)
        if not isinstance(value,dict) or type(value.get('schema')) is not int or value['schema']!=1 or not isinstance(value.get('bans'),list) or not isinstance(value.get('audit'),list):
            raise ConfigError('owner bans snapshot invalid')
        seen=set()
        for row in value['bans']:
            if not isinstance(row,dict) or type(row.get('user_id')) is not int or row['user_id']<=0 or row['user_id'] in seen:
                raise ConfigError('owner bans snapshot invalid')
            seen.add(row['user_id']);expiry=row.get('expires_at')
            if expiry is not None and (type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry<=0):
                raise ConfigError('owner bans snapshot invalid')
    elif target=='state/moderation/beatmap-exclusions.json':
        value=read_json(path,maximum=1024*1024)
        if (not isinstance(value,dict) or type(value.get('schema')) is not int or value['schema']!=1
                or not isinstance(value.get('exclusions'),list) or any(not isinstance(item,dict)
                    or type(item.get('beatmap_id')) is not int or item['beatmap_id']<=0 for item in value['exclusions'])):
            raise ConfigError('owner exclusions snapshot invalid')
    elif target.startswith('config/ai-context-'):
        if path.stat().st_size>1024*1024:raise ConfigError('owner context snapshot too large')


def _config(value):
    fields(value,('defaults','rooms','universal'),'owner source configuration')
    if not isinstance(value['rooms'],dict):raise ConfigError('owner source configuration object invalid')
    from bot.mode import MANIA_KEYS
    objects=[value['defaults'],value['universal'],*value['rooms'].values()]
    for obj in objects:
        if not isinstance(obj,dict):raise ConfigError('owner source configuration object invalid')
        for key,item in obj.items():
            if key=='mania_settings':
                if not isinstance(item,dict) or set(item)-MANIA_KEYS:raise ConfigError('owner source nested mania configuration invalid')
            elif integrations.classify(key) in ('unclassified','credential_value_refused'):
                raise ConfigError('owner source configuration has unclassified or credential fields')
    effective=gameplay.effective_legacy(value['defaults'],value['rooms'],value['universal'])
    preparation=integrations.prepare_adoption(value['defaults'],value['rooms'],value['universal'],
        {'installation_id':'snapshot-only','generation':'snapshot-only'})
    if not preparation['summary']['ok']:raise ConfigError('owner source configuration has unclassified or credential fields')
    return effective


def _required_states(effective):
    required=set()
    for alias,cfg in effective.items():
        for key in ('moderation_bans_file','beatmap_exclusions_file','ai_context_file'):
            if cfg.get(key):required.add(integrations.PATHS[key].format(alias=alias))
    return required


def create_snapshot(destination,*,source_config,ledger,state_files,state_complete=False):
    """Seal reviewed offline copies; no source path is inferred from config.

    state_files maps allowed native target paths to explicit private COPY paths.
    state_complete is the caller's reviewed enumeration proof, including all
    player language files and caches. Source config paths are never followed.
    """
    if state_complete is not True:raise ConfigError('owner complete state enumeration required')
    effective=_config(source_config)
    if not isinstance(state_files,dict):raise ConfigError('owner snapshot state objects invalid')
    targets={_state_target(k) for k in state_files}
    if len(targets)>MAX_STATE_FILES:raise ConfigError('owner snapshot state enumeration exceeds bound')
    total=0
    for target,source in state_files.items():
        source=_offline_path(source);total+=source.stat().st_size
        if total>MAX_STATE_BYTES:raise ConfigError('owner snapshot state bytes exceed bound')
    if not _required_states(effective)<=targets:raise ConfigError('owner declared private state missing from snapshot')
    ledger=_offline_path(ledger)
    for suffix in ('-wal','-shm','-journal'):
        sidecar=Path(str(ledger)+suffix)
        if os.path.lexists(sidecar):_offline_path(sidecar)
    destination=Path(destination).absolute();_offline_path(destination.parent,directory=True)
    if os.path.lexists(destination):raise ConfigError('owner snapshot destination already exists')
    destination.mkdir(mode=0o700)
    try:
        atomic_json(destination/CONFIG,source_config)
        files={CONFIG:_identity(destination/CONFIG)}
        migration.snapshot_ledger(ledger,destination/'ledger.sqlite')
        _legacy_schema_check(destination/'ledger.sqlite')
        files['ledger.sqlite']=_identity(destination/'ledger.sqlite')
        states={}
        for index,target in enumerate(sorted(targets)):
            source=_offline_path(state_files[target]);_state_check(target,source)
            if source.stat().st_size>64*1024**2:raise ConfigError('owner snapshot state file too large')
            name=f'state-{index:06d}.bin';identity=_identity(source)
            _publish(source,destination/name,identity);files[name]=identity;states[target]=name
        manifest={'format':1,'files':files,'states':states,'state_complete':True}
        atomic_json(destination/MANIFEST,manifest)
        return {'ok':True,'state_files':len(states),'rooms':len(effective),'credentials_copied':False,
                'provider_dispatch':False,'production_mutated':False}
    except BaseException:
        # This newly-created directory is ours; never recurse into arbitrary
        # caller directories or remove a previously published snapshot.
        for item in destination.iterdir():
            if item.is_file() and not item.is_symlink():item.unlink()
        destination.rmdir();raise


def _bundle(root):
    root=_offline_path(root,directory=True)
    manifest=read_json(root/MANIFEST,maximum=8*1024**2)
    fields(manifest,('format','files','states','state_complete'),'owner snapshot manifest')
    if type(manifest['format']) is not int or manifest['format']!=1 or manifest['state_complete'] is not True:
        raise ConfigError('owner snapshot manifest invalid')
    files,states=manifest['files'],manifest['states']
    if not isinstance(files,dict) or not isinstance(states,dict) or not {CONFIG,'ledger.sqlite'}<=set(files):
        raise ConfigError('owner snapshot manifest invalid')
    if len(states)>MAX_STATE_FILES:raise ConfigError('owner snapshot state enumeration exceeds bound')
    if set(files)!={CONFIG,'ledger.sqlite',*states.values()} or len(set(states.values()))!=len(states):
        raise ConfigError('owner snapshot file enumeration invalid')
    for target,name in states.items():
        _state_target(target)
        if type(name) is not str or re.fullmatch(r'state-[0-9]{6}\.bin',name) is None:
            raise ConfigError('owner snapshot state name invalid')
    for name,identity in files.items():
        fields(identity,('sha256','bytes'),'owner snapshot file identity')
        if type(identity['sha256']) is not str or re.fullmatch('[a-f0-9]{64}',identity['sha256']) is None or type(identity['bytes']) is not int or identity['bytes']<0:
            raise ConfigError('owner snapshot file identity invalid')
        if _identity(contained(root,name))!=identity:raise ConfigError('owner snapshot integrity mismatch')
    if sum(files[name]['bytes'] for name in states.values())>MAX_STATE_BYTES:
        raise ConfigError('owner snapshot state bytes exceed bound')
    if {p.name for p in root.iterdir()}!=set(files)|{MANIFEST}:raise ConfigError('owner snapshot has unreviewed files')
    _legacy_schema_check(root/'ledger.sqlite')
    cfg=read_json(root/CONFIG,maximum=4*1024**2);effective=_config(cfg)
    if not _required_states(effective)<=set(states):raise ConfigError('owner snapshot declared state missing')
    for target,name in states.items():_state_check(target,root/name)
    return manifest,cfg,effective


def record(root):
    marker,_=load(root);path=contained(root,RECORD)
    if not os.path.lexists(path):return None
    value=read_json(path,maximum=8*1024**2)
    fields(value,('format','installation_id','generation','phase','snapshot_sha256','outputs','audit','seed'),'owner migration receipt')
    if (type(value['format']) is not int or value['format']!=1 or any(value[k]!=marker[k] for k in ('installation_id','generation'))
            or value['phase'] not in PHASES or type(value['snapshot_sha256']) is not str
            or re.fullmatch('[a-f0-9]{64}',value['snapshot_sha256']) is None or not isinstance(value['outputs'],dict)):
        raise ConfigError('owner migration receipt invalid')
    allowed={gameplay.RECORD,integrations.RECORD,migration.LEDGER,'backups/owner-source-config.json'}
    for path,identity in value['outputs'].items():
        if path not in allowed:_state_target(path)
        fields(identity,('sha256','bytes'),'owner migration output identity')
        if type(identity['sha256']) is not str or re.fullmatch('[a-f0-9]{64}',identity['sha256']) is None or type(identity['bytes']) is not int or identity['bytes']<0:
            raise ConfigError('owner migration output identity invalid')
    if not allowed<=set(value['outputs']):raise ConfigError('owner migration receipt outputs missing')
    if value['audit'] is not None:
        audit=value['audit'];fields(audit,('ok','rooms','checked_keys','mismatches','credential_values_returned'),'owner parity receipt')
        if (type(audit['ok']) is not bool or type(audit['rooms']) is not int or not 1<=audit['rooms']<=4
                or type(audit['checked_keys']) is not int or audit['checked_keys']<0
                or audit['credential_values_returned'] is not False or not isinstance(audit['mismatches'],list)):
            raise ConfigError('owner parity receipt invalid')
        aliases={r['alias'] for r in load(root)[1]['rooms']}
        for item in audit['mismatches']:
            fields(item,('alias','key','reason'),'owner parity mismatch receipt')
            if (item['alias'] not in aliases or type(item['key']) is not str
                    or re.fullmatch('[a-z][a-z0-9_]{0,95}',item['key']) is None
                    or item['reason'] not in ('unclassified','credential_value_refused','missing_effective_key',
                        'effective_value_or_type_changed','required_conversion_companion_missing')):
                raise ConfigError('owner parity receipt invalid')
        if audit['ok']!=(not bool(audit['mismatches'])):raise ConfigError('owner parity receipt invalid')
    if value['seed'] is not None:
        seed=value['seed'];fields(seed,('ok','seeded','already_seeded','post_boundary','excluded',
            'historical_seed','uploaded','provider_dispatch'),'owner seed receipt')
        if (seed['ok'] is not True or seed['historical_seed'] is not True or seed['uploaded'] is not False
                or seed['provider_dispatch'] is not False or any(type(seed[k]) is not int or seed[k]<0
                    for k in ('seeded','already_seeded','post_boundary','excluded'))):
            raise ConfigError('owner seed receipt invalid')
    if value['phase']=='committed' and (not isinstance(value['audit'],dict) or value['audit'].get('ok') is not True or not isinstance(value['seed'],dict)):
        raise ConfigError('owner migration receipt commit invalid')
    return value


def admission(root):
    value=record(root)
    if value is not None and value['phase']!='committed':raise ConfigError('owner migration recovery required')


def _audit(root,effective,mappings):
    from run_bot import DEFAULT_CONFIG
    from bot.mode import merge_room_config
    from .runtime_config import render
    target={r['alias']:merge_room_config(DEFAULT_CONFIG,r['settings'],{}) for r in render(root)['rooms']}
    if set(target)!=set(effective) or set(mappings)-set(effective):raise ConfigError('owner migration parity topology invalid')
    checked=0;failures=[]
    for alias,source in effective.items():
        result=integrations.parity(source,target[alias],mappings=mappings.get(alias,{}),additions=ADDITIONS)
        checked+=result['checked_keys']
        failures.extend({'alias':alias,**item} for item in result['mismatches'])
        for key in ('mania_convert_db','mania_convert_validation_db'):
            if source.get(key) and not target[alias].get(key):failures.append({'alias':alias,'key':key,'reason':'required_conversion_companion_missing'})
    return {'ok':not failures,'rooms':len(effective),'checked_keys':checked,'mismatches':failures,
            'credential_values_returned':False}


def import_snapshot(root,snapshot,*,mappings=None,stopped=False,owner_lease_held=False):
    """Resume one bound import into an unused offline target under its lease.

    mappings contains private per-alias reviewed (source,target) pairs only for
    managed path/identity changes. It cannot waive changed gameplay/integrations.
    No OAuth is copied and no unit is started. Existing native preferences are
    refused, rather than overwritten. A failure retains all recovery inputs.
    owner_lease_held is a trusted installed-wrapper assertion, never a CLI or
    configuration field: use only inside _stopped/_quiescent's held lease.
    """
    if stopped is not True:raise ConfigError('owner migration stopped writer proof required')
    if type(owner_lease_held) is not bool:raise ConfigError('owner migration lease proof invalid')
    root=_offline_path(root,directory=True);snapshot=_offline_path(snapshot,directory=True)
    if root==snapshot or root.is_relative_to(snapshot) or snapshot.is_relative_to(root):raise ConfigError('owner snapshot must be outside target installation')
    manifest,cfg,effective=_bundle(snapshot);marker,config=load(root)
    prepared=integrations.prepare_adoption(cfg['defaults'],cfg['rooms'],cfg['universal'],marker)
    overlay=gameplay.make_record(marker,config,prepared['gameplay_projection'])
    integration_record=integrations.validate_record(prepared['integration_record'],marker,config)
    snapshot_digest=_identity(snapshot/MANIFEST)['sha256'];mappings={} if mappings is None else mappings
    with nullcontext() if owner_lease_held else owner_lease(root):
        value=record(root)
        if value is not None and value['snapshot_sha256']!=snapshot_digest:raise ConfigError('owner migration snapshot changed')
        if value is None:
            outputs={migration.LEDGER:manifest['files']['ledger.sqlite'],'backups/owner-source-config.json':manifest['files'][CONFIG]}
            for target,name in manifest['states'].items():outputs[target]=manifest['files'][name]
            # Stable JSON encoding is identical to layout.atomic_json.
            for path,obj in ((gameplay.RECORD,overlay),(integrations.RECORD,integration_record)):
                data=(json.dumps(obj,indent=2,allow_nan=False)+'\n').encode()
                outputs[path]={'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}
            if os.path.lexists(root/'state/runtime-config.json') or os.path.lexists(root/'config/rooms'):
                raise ConfigError('owner migration existing native preferences refused')
            if any(os.path.lexists(contained(root,path)) for path in outputs) or os.path.lexists(root/migration.RECORD) or os.path.lexists(root/migration.BACKUP):
                raise ConfigError('owner migration target already contains state')
            value={'format':1,**{k:marker[k] for k in ('installation_id','generation')},'phase':'prepared',
                   'snapshot_sha256':snapshot_digest,'outputs':outputs,'audit':None,'seed':None}
            atomic_json(root/RECORD,value)
        if value['phase']=='committed':return status(root)
        if value['phase']=='prepared':
            pairs={migration.LEDGER:'ledger.sqlite','backups/owner-source-config.json':CONFIG,**manifest['states']}
            for target,name in pairs.items():
                destination=contained(root,target);destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                _publish(snapshot/name,destination,value['outputs'][target])
            for target,obj in ((gameplay.RECORD,overlay),(integrations.RECORD,integration_record)):
                destination=contained(root,target)
                if os.path.lexists(destination):
                    if _identity(destination)!=value['outputs'][target]:raise ConfigError('owner migration overlay changed')
                else:atomic_json(destination,obj)
            value['phase']='files_imported';atomic_json(root/RECORD,value)
        # Non-ledger state must remain exactly the supplied snapshot on resume.
        for path,expected in value['outputs'].items():
            if path!=migration.LEDGER and _identity(contained(root,path))!=expected:raise ConfigError('owner migration imported state changed')
        if value['phase']=='files_imported':
            if migration.migration_record(root) is None and _identity(root/migration.LEDGER)!=value['outputs'][migration.LEDGER]:
                raise ConfigError('owner migration imported ledger changed')
            migration.migrate_ledger(root)
            from .ratings.seed import seed_ledger
            value['seed']=seed_ledger(root)
            value['phase']='ledger_migrated';atomic_json(root/RECORD,value)
        audit=_audit(root,effective,mappings);value['audit']=audit;atomic_json(root/RECORD,value)
        if not audit['ok']:raise ConfigError('owner migration exhaustive parity requires review')
        value['phase']='committed';atomic_json(root/RECORD,value)
        return status(root)


def status(root):
    value=record(root)
    return {'ok':True,'migration_phase':'absent' if value is None else value['phase'],
            'recovery_required':value is not None and value['phase']!='committed',
            'state_files':0 if value is None else len(value['outputs'])-4,
            'parity':None if value is None else copy.deepcopy(value['audit']),
            'seed':None if value is None else copy.deepcopy(value['seed']),
            'credentials_copied':False,'provider_dispatch':False,'production_mutated':False}


def rollback_ledger(source,destination,*,stopped=False):
    """Exclusive v6-compatible ledger export, including NEW local v7 votes.

    Source is an isolated consistent copy (not a migrated live path). Export
    selects ALL rows of ALL native RatingStore base tables/columns, retaining
    languages/policies in the separate owner snapshot. Unknown native columns
    refuse export. v7 outbox stays archived in the original, never replayed.
    """
    if stopped is not True:raise ConfigError('owner rollback stopped writer proof required')
    source=_offline_path(source);destination=Path(destination).absolute()
    _offline_path(destination.parent,directory=True)
    if os.path.lexists(destination):raise ConfigError('owner rollback destination already exists')
    for suffix in ('-wal','-shm','-journal'):
        if os.path.lexists(Path(str(source)+suffix)):raise ConfigError('owner rollback requires consistent standalone snapshot')
    origin=sqlite3.connect('file:'+quote(str(source))+'?mode=ro&immutable=1',uri=True)
    fd,name=tempfile.mkstemp(prefix='.owner-rollback-',dir=destination.parent);os.fchmod(fd,0o600);os.close(fd)
    temporary=Path(name);target=sqlite3.connect(temporary)
    counts={}
    try:
        if origin.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ConfigError('owner rollback source integrity failed')
        schema=_native_schema()
        source_tables={r[0] for r in origin.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        for table,(sql,columns,mandatory,indexes) in schema.items():
            if mandatory or table in source_tables:
                target.execute(sql)
                for index in indexes:target.execute(index)
        tables=[r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        if source_tables-set(tables)-{'rating_sync_meta','rating_revision_cache','rating_outbox'}:
            raise ConfigError('owner rollback unclassified tables require review')
        for table in tables:
            expected=[r[1:] for r in target.execute(f'PRAGMA table_info("{table}")')]
            if not _native_columns_match(origin,table,expected):
                raise ConfigError('owner rollback native schema incompatible')
            columns=[r[0] for r in expected]
            names=','.join('"'+column+'"' for column in columns)
            insert=f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns)})'
            rows=origin.execute(f'SELECT {names} FROM "{table}"');count=0
            while batch:=rows.fetchmany(1000):target.executemany(insert,batch);count+=len(batch)
            counts[table]=count
        if origin.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            for table,sequence in origin.execute('SELECT name,seq FROM sqlite_sequence'):
                if table in tables:
                    target.execute('DELETE FROM sqlite_sequence WHERE name=?',(table,))
                    target.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)',(table,sequence))
        target.commit()
        if target.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ConfigError('owner rollback export integrity failed')
        target.close();target=None;origin.close();origin=None
        _publish(temporary,destination,_identity(temporary))
        return {'ok':True,'native_table_counts':counts,'post_migration_votes_preserved':True,
                'original_preserved':True,'provider_dispatch':False,'production_mutated':False}
    finally:
        if target is not None:target.close()
        if origin is not None:origin.close()
        temporary.unlink(missing_ok=True)


def _native_schema():
    """Code-owned literal native schemas only; no source SQLite DDL is adopted."""
    from bot.ratings import SCHEMA
    import bot.ppstats as stats
    import bot.player_preferences as preferences
    memory=sqlite3.connect(':memory:')
    try:
        memory.executescript(SCHEMA)
        base={r[0] for r in memory.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        for module in (stats,preferences):
            tree=ast.parse(Path(module.__file__).read_text())
            statements=[node.value for node in ast.walk(tree) if isinstance(node,ast.Constant)
                and type(node.value) is str and node.value.lstrip().startswith(('CREATE TABLE IF NOT EXISTS ','CREATE INDEX IF NOT EXISTS '))]
            for sql in statements:
                memory.execute(sql)
                if module is stats:
                    mania=sql
                    for name in ('autohost_pp_achievements','autohost_lobby_time','ix_autohost_pp_user',
                                 'ix_autohost_pp_created','ix_autohost_lobby_username'):
                        mania=re.sub(r'\b'+name+r'\b',name+'_mania',mania)
                    memory.execute(mania)
        return {table:(sql,[r[1:] for r in memory.execute(f'PRAGMA table_info("{table}")')],table in base,
            [r[0] for r in memory.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",(table,))])
            for table,sql in memory.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    finally:memory.close()


def _native_columns_match(connection,table,expected):
    # Native ALTER TABLE history may change column order. Keep exact named
    # affinity/nullability/default/PK metadata and reject hidden columns.
    actual=[r[1:] for r in connection.execute(f'PRAGMA table_xinfo("{table}")')]
    named={row[0]:tuple(row) for row in expected}
    return (len(actual)==len(named) and all(row[-1]==0 for row in actual)
            and {row[0]:tuple(row[:-1]) for row in actual}==named)


def _legacy_schema_check(path):
    """Closed native source schema, after consistent private backup only."""
    if path.stat().st_size>256*1024**2:raise ConfigError('owner legacy ledger exceeds bounded import')
    schema=_native_schema()
    connection=sqlite3.connect('file:'+quote(str(path))+'?mode=ro&immutable=1',uri=True)
    try:
        tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables-set(schema) or {name for name,row in schema.items() if row[2]}-tables:
            raise ConfigError('owner legacy ledger schema requires review')
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1").fetchone():
            raise ConfigError('owner legacy ledger executable schema requires review')
        for table in tables:
            # Historical native ALTER TABLE appends fields in a different order
            # from a fresh CREATE. Every import/rollback copy addresses columns
            # by name; names, affinity, nullability, defaults and PK positions
            # must still match the exact code-owned schema. Hidden/generated
            # columns are not a supported native history extension.
            if not _native_columns_match(connection,table,schema[table][1]):
                raise ConfigError('owner legacy ledger columns require review')
    finally:connection.close()


def rollback_state(root,destination,*,stopped=False,owner_lease_held=False):
    """Archive the CURRENT isolated owner state and a compatible v6 ledger.

    Fixed host-local policies/preferences/caches are enumerated while stopped.
    The entire v7 ledger remains archived for outbox reconciliation. Exact old
    source configuration and current native room edits are both retained; no
    old backup is restored over new votes or preferences, and no service starts.
    """
    if stopped is not True or type(owner_lease_held) is not bool:
        raise ConfigError('owner rollback stopped writer proof required')
    root=_offline_path(root,directory=True);destination=Path(destination).absolute()
    _offline_path(destination.parent,directory=True)
    if os.path.lexists(destination):raise ConfigError('owner rollback destination already exists')
    with nullcontext() if owner_lease_held else owner_lease(root):
        admission(root);value=record(root)
        if value is None:raise ConfigError('owner rollback requires committed adoption')
        _,config=load(root);integration_record=integrations.load(root,*load(root))
        files={'.poolrotater-host.json','config/host.json',gameplay.RECORD,integrations.RECORD,
               'backups/owner-source-config.json'}
        optional={f'config/rooms/{room["alias"]}.json' for room in config['rooms']}
        optional.update({'config/rooms/universal.json','state/runtime-config.json'})
        files.update(path for path in optional if os.path.lexists(contained(root,path)))
        for alias,row in integration_record['rooms'].items():
            for key,enabled in row['paths'].items():
                if not enabled:continue
                path=integrations.PATHS[key].format(alias=alias)
                if key=='map_pp_cache_dir':continue
                source=contained(root,path)
                if os.path.lexists(source):files.add(path)
                elif key in ('ai_context_file','moderation_bans_file','beatmap_exclusions_file'):
                    raise ConfigError('owner rollback preserved state missing')
        # Languages and both explicit/private and supervisor version PP caches.
        for folder in ('state/player-language-v1','state/controller-map-pp','state/map_pp'):
            directory=contained(root,folder)
            if not os.path.lexists(directory):continue
            _offline_path(directory,directory=True)
            for source in directory.rglob('*'):
                if source.is_symlink():raise ConfigError('owner rollback state symlink refused')
                if source.is_dir():_offline_path(source,directory=True);continue
                relative=str(source.relative_to(root))
                if folder=='state/player-language-v1' and source.name=='.lock':continue
                if folder=='state/player-language-v1':_state_target(relative)
                elif not re.fullmatch(r'state/(?:controller-map-pp|map_pp)(?:/[A-Za-z0-9_.-]{1,100}){0,2}/'+PP_CACHE_SUFFIX,relative):
                    raise ConfigError('owner rollback cache enumeration requires review')
                files.add(relative)
        if len(files)>MAX_STATE_FILES:raise ConfigError('owner rollback state enumeration exceeds bound')
        total=0
        for path in files:
            source=contained(root,path)
            if source.stat().st_size>64*1024**2:raise ConfigError('owner rollback state file too large')
            total+=source.stat().st_size
        if total>MAX_STATE_BYTES:raise ConfigError('owner rollback state bytes exceed bound')
        destination.mkdir(mode=0o700)
        try:
            migration.snapshot_ledger(root/migration.LEDGER,destination/'current-v7.sqlite')
            ledger_result=rollback_ledger(destination/'current-v7.sqlite',destination/'legacy-v6.sqlite',stopped=True)
            identity={name:_identity(destination/name) for name in ('current-v7.sqlite','legacy-v6.sqlite')}
            archive={}
            for index,path in enumerate(sorted(files)):
                source=contained(root,path);name=f'private-{index:06d}.bin'
                if path==integrations.PATHS['moderation_bans_file'] or path in OBSERVER_PATHS:
                    # Preserve exact declared native0640 writer bytes into a
                    # private600 archive; generic private inputs remain strict.
                    raw=(integrations._ban_bytes(root) if path==integrations.PATHS['moderation_bans_file']
                         else _observer_bytes(root,path))
                    fd=os.open(destination/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
                    identity[name]=_identity(destination/name)
                else:
                    if path.startswith('state/player-language-v1/'):_state_check(path,source)
                    expected=_identity(source);_publish(source,destination/name,expected);identity[name]=expected
                archive[path]=name
            atomic_json(destination/'rollback.json',{'format':1,'kind':'stopped-owner-rollback',
                'files':identity,'states':archive,'native_table_counts':ledger_result['native_table_counts']})
            verify_rollback(destination)
            return {'ok':True,'state_files':len(archive),'native_table_counts':ledger_result['native_table_counts'],
                'post_migration_votes_preserved':True,'post_migration_preferences_preserved':True,
                'v7_outbox_archived':True,'source_configuration_archived':True,'credentials_copied':False,
                'services_started':False,'provider_dispatch':False,'production_mutated':False}
        except BaseException:
            # Preserve the full partial rollback archive for review. Publication
            # is a final manifest; an absent manifest never means a usable kit.
            raise


def verify_rollback(directory):
    """Read-only closed archive verification; reports counts only, never IDs."""
    directory=_offline_path(directory,directory=True)
    manifest=read_json(directory/'rollback.json',maximum=8*1024**2)
    fields(manifest,('format','kind','files','states','native_table_counts'),'owner rollback manifest')
    if type(manifest['format']) is not int or manifest['format']!=1 or manifest['kind']!='stopped-owner-rollback':
        raise ConfigError('owner rollback manifest invalid')
    files,states,counts=manifest['files'],manifest['states'],manifest['native_table_counts']
    if not isinstance(files,dict) or not isinstance(states,dict) or not isinstance(counts,dict):
        raise ConfigError('owner rollback manifest invalid')
    if (set(files)!={'current-v7.sqlite','legacy-v6.sqlite',*states.values()}
            or len(set(states.values()))!=len(states)
            or {p.name for p in directory.iterdir()}!=set(files)|{'rollback.json'}):
        raise ConfigError('owner rollback archive enumeration invalid')
    fixed={'.poolrotater-host.json','config/host.json',gameplay.RECORD,integrations.RECORD,
           'backups/owner-source-config.json','config/rooms/universal.json','state/runtime-config.json'}
    for path,name in states.items():
        if path not in fixed and not re.fullmatch(r'config/rooms/[a-z][a-z0-9_-]{0,23}\.json',path):
            if path.startswith('state/map_pp/'):
                if re.fullmatch(r'state/map_pp(?:/[A-Za-z0-9_.-]{1,100}){0,2}/'+PP_CACHE_SUFFIX,path) is None:
                    raise ConfigError('owner rollback cache path invalid')
            else:_state_target(path)
        if type(name) is not str or re.fullmatch('private-[0-9]{6}\\.bin',name) is None:
            raise ConfigError('owner rollback archive name invalid')
    for name,expected in files.items():
        fields(expected,('sha256','bytes'),'owner rollback identity')
        if _identity(contained(directory,name))!=expected:raise ConfigError('owner rollback archive integrity mismatch')
    _legacy_schema_check(directory/'legacy-v6.sqlite')
    conn=sqlite3.connect('file:'+quote(str(directory/'legacy-v6.sqlite'))+'?mode=ro&immutable=1',uri=True)
    try:
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if set(counts)!=tables:raise ConfigError('owner rollback count receipt invalid')
        for table in tables:
            if type(counts[table]) is not int or counts[table]!=conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]:
                raise ConfigError('owner rollback count receipt invalid')
    finally:conn.close()
    return {'ok':True,'state_files':len(states),'native_table_counts':copy.deepcopy(counts),
            'v7_outbox_archived':True,'credentials_copied':False,'provider_dispatch':False}
