"""Native Catch production export, separate from the immutable C6 research kit."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .config import ConfigError
from bot.catch_catalog import production
from bot.catch_catalog.c6_schema import canonical, decode, validate

READER = 'poolrotater-catch-selection-v1'
CAPABILITIES = {'styles': ['variety'], 'mods': ['NM'], 'companions': [],
                'unavailable': {'loved': 'not enabled in native Catch production',
                                'converts': 'native Catch only',
                                'farm': 'neutral weights; unqualified',
                                'automatic_difficulty': 'fixed difficulty only'}}
ANALYSIS = {'schema': production.SCHEMA,
            'official_commit': 'b11d0d87824f33984587438074f3791ce06b159b',
            'official_difficulty_version': 20251020,
            'permission': 'owner-attested-ppy-production-and-distribution'}


def permission(path, source_manifest):
    """Explicit local permission record; never inferred from research flags."""
    raw=Path(path).read_bytes()
    if len(raw)>16384: raise ConfigError('Catch permission record exceeds bound')
    value=decode(raw)
    expected={'format','authority','recorded_at','scope','production_use','distribution','source_manifest_sha256'}
    if (type(value) is not dict or set(value)!=expected or type(value['format']) is not int or value['format']!=1
            or value['production_use'] is not True or value['distribution'] is not True
            or value['source_manifest_sha256']!=source_manifest
            or value['authority']!='owner-attested-peppy-permission'
            or value['scope']!='PoolRotater native Catch production and catalog distribution'
            or type(value['recorded_at']) is not str or len(value['recorded_at'])>40):
        raise ConfigError('explicit Catch production/distribution permission required')
    return hashlib.sha256(canonical(value)).hexdigest(),len(canonical(value))


def export(research, destination, *, permission_record):
    from .catalog_export import _pack, _identity, canonical as manifest_bytes, sha256, EXCLUDED
    from bot.catch_catalog.c6_reader import Catalog
    research=Path(research); destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink():raise ConfigError('export destination already exists')
    manifest_path=research/'manifest.json'; source_id=sha256(manifest_path)
    permit,permit_bytes=permission(permission_record,source_id)
    catalog=Catalog(research,purpose='research') # original guards are unchanged
    destination.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.catch-export-',dir=destination.parent))
    try:
        count=0;digest=hashlib.sha256();statuses=Counter()
        with sqlite3.connect(stage/'catalog.db') as db:
            db.execute('PRAGMA page_size=4096');db.execute('PRAGMA journal_mode=DELETE')
            db.execute('PRAGMA application_id=1347572547');db.execute('PRAGMA user_version=2')
            db.execute('CREATE TABLE catch_maps (beatmap_id INTEGER PRIMARY KEY,variant_id TEXT,payload TEXT)')
            db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)')
            for research_row in catalog.rows():
                row=production.project(research_row)
                if row['selection']['map_id'] in EXCLUDED:continue
                raw=canonical(row)
                if len(raw)>production.MAX_PAYLOAD:raise ConfigError('Catch row exceeds projection bound')
                db.execute('INSERT INTO catch_maps VALUES(?,?,?)',
                           (row['selection']['map_id'],row['selection']['variant_id'],raw.decode()))
                digest.update(raw+b'\n');count+=1;statuses[row['selection']['status']]+=1
            meta={'schema':production.SCHEMA,'permission_id':permit,'source_manifest_sha256':source_id,
                  'rows':str(count),'rows_sha256':digest.hexdigest()}
            db.executemany('INSERT INTO metadata VALUES(?,?)',sorted(meta.items()))
        if not count:raise ConfigError('empty Catch production catalog')
        files=[_identity(stage/'catalog.db','catalog',{'catch_maps':count})]
        manifest={'format':1,'export_schema':2,'reader':READER,'mode':'catch','ruleset':2,'keys':None,
                  'files':files,'sources':{'research_manifest':{'sha256':source_id,'bytes':manifest_path.stat().st_size},
                  'permission':{'sha256':permit,'bytes':permit_bytes}},'analysis':dict(ANALYSIS),
                  'capabilities':json.loads(json.dumps(CAPABILITIES)),
                  'exclusions':{'revision':1,'beatmap_ids':list(EXCLUDED)},'archive':_pack(stage,files)}
        manifest['content_id']=hashlib.sha256(manifest_bytes(manifest)).hexdigest()
        validate_manifest(manifest)
        (stage/'manifest.json').write_bytes(manifest_bytes(manifest))
        inspect(stage,manifest)
        if sha256(manifest_path)!=source_id or permission(permission_record,source_id)!=(permit,permit_bytes):
            raise ConfigError('Catch permission/source changed during export')
        os.rename(stage,destination)
        return manifest
    finally:
        if stage.exists():shutil.rmtree(stage)


def validate_manifest(value):
    from .catalog_export import _closed,_hash,_size,canonical,EXCLUDED,MAX_ROWS
    _closed(value,{'format','export_schema','reader','mode','ruleset','keys','files','sources','analysis',
                   'capabilities','exclusions','archive','content_id'})
    if (type(value['format']) is not int or value['format']!=1 or type(value['export_schema']) is not int
            or value['export_schema']!=2 or value['reader']!=READER or value['mode']!='catch'
            or type(value['ruleset']) is not int or value['ruleset']!=2 or value['keys'] is not None
            or value['analysis']!=ANALYSIS or value['capabilities']!=CAPABILITIES
            or value['exclusions']!={'revision':1,'beatmap_ids':list(EXCLUDED)}):
        raise ConfigError('unsupported native Catch production contract')
    if type(value['files']) is not list or len(value['files'])!=1:raise ConfigError('Catch has no companions')
    item=value['files'][0];_closed(item,{'name','role','sha256','bytes','tables'})
    if item['name']!='catalog.db' or item['role']!='catalog':raise ConfigError('invalid Catch catalog file')
    _hash(item['sha256']);_size(item['bytes']);_closed(item['tables'],{'catch_maps'})
    count=item['tables']['catch_maps']
    if type(count) is not int or not 0<count<=production.MAX_ROWS:raise ConfigError('invalid Catch row count')
    _closed(value['sources'],{'research_manifest','permission'})
    for entry in value['sources'].values():
        _closed(entry,{'sha256','bytes'});_hash(entry['sha256']);_size(entry['bytes'])
    arc=value['archive'];_closed(arc,{'name','sha256','bytes','expanded_bytes','codec'})
    _hash(arc['sha256']);_size(arc['bytes']);_size(arc['expanded_bytes'])
    if arc['name']!='bundle.tar.gz' or arc['codec']!='tar+gzip-v1' or arc['expanded_bytes']!=item['bytes']:
        raise ConfigError('invalid Catch archive contract')
    _hash(value['content_id'])
    if hashlib.sha256(canonical({k:v for k,v in value.items() if k!='content_id'})).hexdigest()!=value['content_id']:
        raise ConfigError('Catch content identity mismatch')
    return value


def inspect(folder, manifest):
    from .catalog_export import EXCLUDED
    folder=Path(folder).absolute();count=0
    for row in production.rows(folder/'catalog.db'):
        if (row['selection']['map_id'] in EXCLUDED or row['identity']['official_commit']!=ANALYSIS['official_commit']
                or row['identity']['official_difficulty_version']!=ANALYSIS['official_difficulty_version']):
            raise ConfigError('excluded map or unqualified Catch calculator')
        count+=1
    with sqlite3.connect((folder/'catalog.db').as_uri()+'?mode=ro',uri=True) as db:
        meta=dict(db.execute('SELECT key,value FROM metadata'))
    if (count!=manifest['files'][0]['tables']['catch_maps']
            or meta['permission_id']!=manifest['sources']['permission']['sha256']
            or meta['source_manifest_sha256']!=manifest['sources']['research_manifest']['sha256']):
        raise ConfigError('Catch manifest/projection binding mismatch')
    return manifest
