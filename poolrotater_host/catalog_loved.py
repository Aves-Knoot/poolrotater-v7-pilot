"""Secret-free, signed-distribution projection for native Standard Loved selection.

Reuse the existing Loved MapPool reader. No player scores, identities, credentials,
research tables, arbitrary source SQL or source SQLite pages are distributed.
"""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .config import ConfigError
from .catalog_export import (BASE, columns, create_table, source, validate_value,
                             _identity, _pack, canonical, sha256, EXCLUDED, MAX_ROWS)

READER='poolrotater-loved-selection-v1'
COLUMNS=BASE+columns('beatmapset_id:INTEGER mode:TEXT convert:INTEGER download_disabled:INTEGER playcount:INTEGER ar:REAL')
CAPABILITIES={'styles':['variety','loved'],'mods':['NM'],'companions':[],
              'unavailable':{'DT':'no qualified Loved DT companion','converts':'native Standard only',
                             'farm':'neutral weights; Loved selection'}}
ANALYSIS={'selection':'native-standard-loved-v1','status':'loved','ruleset':'osu','converts':False}


def check(row):
    if len(row)!=len(COLUMNS):raise ConfigError('Loved column count mismatch')
    for (name,kind),value in zip(COLUMNS,row):validate_value(name,kind,value)
    item=dict(zip((n for n,_ in COLUMNS),row))
    if (item['status']!='loved' or item['mode']!='osu' or item['convert']!=0
            or item['download_disabled'] not in (0,1) or item['beatmapset_id']<=0):
        raise ConfigError('Loved catalog contains wrong status/ruleset/conversion')
    return item


def export(catalog,destination):
    destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink():raise ConfigError('export destination already exists')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='.loved-export-',dir=destination.parent))
    try:
        with source(catalog) as (db,origin):
            obj=db.execute("SELECT type,sql FROM sqlite_master WHERE name='beatmaps'").fetchone()
            if not obj or obj[0]!='table' or 'VIRTUAL TABLE' in obj[1].upper():
                raise ConfigError('Loved source table is missing or unsupported')
            actual={r[1] for r in db.execute('PRAGMA table_info(beatmaps)')}
            if not {n for n,_ in COLUMNS}<=actual:raise ConfigError('Loved source selection columns are missing')
            count=0
            with sqlite3.connect(temporary/'catalog.db') as out:
                out.execute('PRAGMA journal_mode=DELETE');out.execute('PRAGMA application_id=1347572547');out.execute('PRAGMA user_version=1')
                create_table(out,'beatmaps',COLUMNS)
                # The collector can retain disabled/zero-star difficulties. Keep
                # every qualified native Loved row; unknown/invalid identities
                # never become substitute ranked maps or a partial farm pool.
                query='SELECT '+','.join(n for n,_ in COLUMNS)+" FROM beatmaps WHERE status='loved' AND mode='osu' AND convert=0 AND sr>0 AND sr<=100 ORDER BY beatmap_id"
                for row in db.execute(query):
                    item=check(row)
                    if item['beatmap_id'] in EXCLUDED:continue
                    count+=1
                    if count>MAX_ROWS:raise ConfigError('Loved row budget exceeded')
                    out.execute('INSERT INTO beatmaps VALUES ('+','.join('?' for _ in COLUMNS)+')',row)
                if not count:raise ConfigError('Loved source has no qualified native maps')
                out.execute('CREATE INDEX selection_window ON beatmaps(sr,hit_length,passcount)')
            files=[_identity(temporary/'catalog.db','catalog',{'beatmaps':count})]
            manifest={'format':1,'export_schema':1,'reader':READER,'mode':'loved','ruleset':0,'keys':None,
                      'files':files,'sources':{'catalog':origin},'analysis':dict(ANALYSIS),
                      'capabilities':{**CAPABILITIES,'unavailable':dict(CAPABILITIES['unavailable'])},
                      'exclusions':{'revision':1,'beatmap_ids':list(EXCLUDED)},'archive':_pack(temporary,files)}
            manifest['content_id']=hashlib.sha256(canonical(manifest)).hexdigest()
            validate_manifest(manifest)
            (temporary/'manifest.json').write_bytes(canonical(manifest))
            inspect(temporary,manifest)
        os.rename(temporary,destination)
        return manifest
    finally:
        if temporary.exists():shutil.rmtree(temporary)


def validate_manifest(value):
    from .catalog_export import _closed,_hash,_size
    _closed(value,{'format','export_schema','reader','mode','ruleset','keys','files','sources','analysis','capabilities','exclusions','archive','content_id'})
    if (type(value['format'])is not int or value['format']!=1 or type(value['export_schema'])is not int or value['export_schema']!=1
            or value['reader']!=READER or value['mode']!='loved' or type(value['ruleset'])is not int or value['ruleset']!=0
            or value['keys']is not None or value['analysis']!=ANALYSIS or value['capabilities']!=CAPABILITIES
            or value['exclusions']!={'revision':1,'beatmap_ids':list(EXCLUDED)}):
        raise ConfigError('unsupported native Standard Loved catalog contract')
    if type(value['files'])is not list or len(value['files'])!=1:raise ConfigError('Loved has no qualified companions')
    item=value['files'][0];_closed(item,{'name','role','sha256','bytes','tables'})
    if item['name']!='catalog.db' or item['role']!='catalog':raise ConfigError('invalid Loved file identity')
    _closed(item['tables'],{'beatmaps'});_size(item['tables']['beatmaps'],maximum=MAX_ROWS)
    _hash(item['sha256']);_size(item['bytes'])
    _closed(value['sources'],{'catalog'});_closed(value['sources']['catalog'],{'sha256','bytes'})
    _hash(value['sources']['catalog']['sha256']);_size(value['sources']['catalog']['bytes'])
    arc=value['archive'];_closed(arc,{'name','sha256','bytes','expanded_bytes','codec'})
    _hash(arc['sha256']);_size(arc['bytes']);_size(arc['expanded_bytes'])
    if arc['name']!='bundle.tar.gz' or arc['codec']!='tar+gzip-v1' or arc['expanded_bytes']!=item['bytes']:
        raise ConfigError('invalid Loved archive contract')
    _hash(value['content_id'])
    if value['content_id']!=hashlib.sha256(canonical({k:v for k,v in value.items() if k!='content_id'})).hexdigest():
        raise ConfigError('Loved content identity mismatch')
    return value


def inspect(folder,manifest):
    validate_manifest(manifest);count=0
    with source(Path(folder)/'catalog.db') as (db,_):
        objects=db.execute("SELECT type,name FROM sqlite_master WHERE type IN ('table','view','trigger')").fetchall()
        if objects!=[('table','beatmaps')]:raise ConfigError('Loved shipping table allowlist mismatch')
        info=db.execute('PRAGMA table_info(beatmaps)').fetchall()
        if tuple((r[1],r[2])for r in info)!=COLUMNS or any(r[5]!=(1 if r[1]=='beatmap_id' else 0)for r in info):
            raise ConfigError('Loved shipping columns mismatch')
        for row in db.execute('SELECT '+','.join(n for n,_ in COLUMNS)+' FROM beatmaps'):
            item=check(row)
            if item['beatmap_id']in EXCLUDED:raise ConfigError('excluded map in Loved export')
            count+=1
    if count!=manifest['files'][0]['tables']['beatmaps']:raise ConfigError('Loved row count mismatch')
    return manifest
