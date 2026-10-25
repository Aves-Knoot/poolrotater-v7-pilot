"""Build the protected public display index from verified compact catalogs.

Inputs are an explicit JSON document with this shape::

    {"format":1,"sources":[
      {"mode":"standard","table":"beatmaps","path":"/absolute/catalog.db","sha256":"..."},
      {"mode":"mania_4k","table":"maps","path":"/absolute/catalog.db","sha256":"..."},
      {"mode":"mania_7k","table":"maps","path":"/absolute/catalog.db","sha256":"..."},
      {"mode":"catch","table":"catch_maps","path":"/absolute/catalog.db","sha256":"..."},
      {"mode":"taiko","table":"taiko_maps","path":"/absolute/catalog.db","sha256":"..."},
      {"mode":"loved","table":"beatmaps","path":"/absolute/catalog.db","sha256":"..."}
    ]}

Every source is hash-pinned, settled, bounded, and opened immutable/read-only.
Only the fixed compact selection table and its public display columns are read.
Loved is projected into the standard output mode; duplicate standard/Loved
rows are accepted only when every output field is identical.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

MAX_DESCRIPTOR_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024**2
MAX_ROWS_PER_SOURCE = 1_000_000
MAX_OUTPUT_ROWS = 1_000_000
MAX_SOURCES = 16
MAX_TEXT = 180
MAX_TOTAL_SECONDS = 86_400
MAX_BEATMAPSET_ID = 10**7
SHA256 = re.compile(r'[0-9a-f]{64}\Z')

# Extra columns reflect the actual compact catalog export schemas. The builder
# never selects them unless they are one of the fixed display fields below.
TABLES = {
    'standard': ('beatmaps',
        {'beatmap_id','artist','title','version','sr','hit_length'},
        {'beatmap_id','artist','title','version','sr','hit_length','beatmapset_id',
         'total_length','passcount','status','ranked_date','ar'}),
    'loved': ('beatmaps',
        {'beatmap_id','artist','title','version','sr','hit_length','beatmapset_id'},
        {'beatmap_id','artist','title','version','sr','hit_length','beatmapset_id',
         'total_length','passcount','status','ranked_date','ar','mode','convert',
         'download_disabled','playcount'}),
    'mania_4k': ('maps',
        {'beatmap_id','artist','title','version','sr','hit_length','mode_int','key_count'},
        {'beatmap_id','artist','title','version','sr','hit_length','beatmapset_id',
         'total_length','passcount','status','ranked_date','mode_int','key_count','ar'}),
    'mania_7k': ('maps',
        {'beatmap_id','artist','title','version','sr','hit_length','mode_int','key_count'},
        {'beatmap_id','artist','title','version','sr','hit_length','beatmapset_id',
         'total_length','passcount','status','ranked_date','mode_int','key_count','ar'}),
    'catch': ('catch_maps', {'beatmap_id','variant_id','payload'},
        {'beatmap_id','variant_id','payload'}),
    'taiko': ('taiko_maps',
        {'beatmap_id','beatmapset_id','artist','title','version','sr','hit_length'},
        {'beatmap_id','beatmapset_id','artist','title','version','sr','hit_length',
         'status','original_mode','is_convert','source_md5','source_sha256',
         'taiko_nm_variant','max_pp','calculator_id','difficulty_version',
         'source_commit','background_url','bpm_min','bpm_max','hit_count',
         'big_hits','centre_hits','rim_hits','peak_hits_per_second',
         'mean_hits_per_second','colour_change_fraction','maximum_mono_streak'}),
}
OUTPUT_MODES = {'standard','mania_4k','mania_7k','catch','taiko'}
OUTPUT_COLUMNS = ('mode','beatmap_id','beatmapset_id','title','artist','version',
                  'total_length','hit_length','sr')
EXCLUDED_MAPS = frozenset({1549496})


def _strict_json(raw: bytes):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,
        parse_constant=lambda _value:(_ for _ in ()).throw(ValueError('nonfinite JSON number')))


def _descriptor(path):
    path=Path(path).absolute()
    info=path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>MAX_DESCRIPTOR_BYTES:
        raise ValueError('invalid display index descriptor file')
    value=_strict_json(path.read_bytes())
    if (type(value)is not dict or set(value)!={'format','sources'}
            or type(value.get('format'))is not int or value['format']!=1):
        raise ValueError('invalid display index descriptor')
    sources=value.get('sources')
    if type(sources)is not list or not 1<=len(sources)<=MAX_SOURCES:
        raise ValueError('invalid display index source list')
    result=[];seen=set()
    for item in sources:
        if type(item)is not dict or set(item)!={'mode','table','path','sha256'}:
            raise ValueError('invalid display index source descriptor')
        mode=item.get('mode')
        if mode not in TABLES or type(item.get('table'))is not str or item['table']!=TABLES[mode][0]:
            raise ValueError('display index source table is not allowed for its mode')
        source=Path(item.get('path',''))
        if not source.is_absolute() or type(item.get('path'))is not str:
            raise ValueError('display index source path must be absolute')
        if type(item.get('sha256'))is not str or not SHA256.fullmatch(item['sha256']):
            raise ValueError('invalid display index source digest')
        key=(mode,str(source))
        if key in seen:raise ValueError('duplicate display index source')
        seen.add(key);result.append({**item,'path':source})
    if {item['mode'] for item in result}!=set(TABLES):
        raise ValueError('display index requires all six qualified source modes')
    return result


def _fingerprint(path):
    info=path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>MAX_SOURCE_BYTES:
        raise ValueError('display index source is not a bounded regular file')
    if any(Path(str(path)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
        raise ValueError('display index source has unsettled SQLite sidecars')
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _sha256(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def _source_db(source):
    path=source['path'];before=_fingerprint(path)
    if _sha256(path)!=source['sha256']:
        raise ValueError('display index source hash mismatch')
    db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True,timeout=2)
    db.row_factory=sqlite3.Row
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,1024*1024)
    db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,64*1024)
    db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN,64)
    db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
    db.execute('PRAGMA busy_timeout=1000')
    object_row=db.execute('SELECT type,sql FROM sqlite_master WHERE name=?',(source['table'],)).fetchone()
    if object_row is None or object_row['type']!='table' or 'VIRTUAL TABLE' in (object_row['sql'] or '').upper():
        db.close();raise ValueError('display index source table is missing or unsupported')
    names={row['name'] for row in db.execute('PRAGMA table_info('+source['table']+')')}
    required,allowed=TABLES[source['mode']][1:]
    if not required<=names or names-allowed:
        db.close();raise ValueError('display index source table columns are not allowed')
    return db,before


def _text(value):
    if type(value)is not str or not value or len(value)>MAX_TEXT or any(ord(ch)<32 or ord(ch)==127 for ch in value):
        return None
    return value


def _integer(value,maximum):
    return value if type(value)is int and 0<value<=maximum else None


def _number(value,minimum,maximum):
    if type(value) not in (int,float) or not math.isfinite(value) or not minimum<=value<=maximum:return None
    return float(value)


def _row(mode,values):
    beatmap_id=values.get('beatmap_id')
    if type(beatmap_id)is not int or beatmap_id<=0:
        raise ValueError('display index source contains invalid beatmap ID')
    return (mode,beatmap_id,_integer(values.get('beatmapset_id'),MAX_BEATMAPSET_ID),
        _text(values.get('title')),_text(values.get('artist')),_text(values.get('version')),
        _number(values.get('total_length'),.001,MAX_TOTAL_SECONDS),
        _number(values.get('hit_length'),.001,MAX_TOTAL_SECONDS),
        _number(values.get('sr'),0,100))


def _catch_values(source, row):
    try:
        payload=_strict_json(row['payload'].encode('utf-8'))
    except Exception as error:
        raise ValueError('invalid compact Catch payload') from error
    if (len(row['payload'].encode('utf-8'))>32*1024 or type(payload)is not dict
            or payload.get('schema')!='catch-native-selection-1'):
        raise ValueError('invalid compact Catch payload')
    selection=payload['selection'];presentation=payload['presentation'];durations=payload['durations']
    difficulty=payload.get('difficulty')
    if (type(selection)is not dict or type(presentation)is not dict or type(durations)is not dict
            or type(difficulty)is not dict
            or type(selection.get('map_id'))is not int or selection['map_id']<=0
            or type(selection.get('set_id'))is not int or selection['set_id']<=0
            or type(selection.get('variant_id'))is not str or not SHA256.fullmatch(selection['variant_id'])
            or row['beatmap_id']!=selection['map_id'] or row['variant_id']!=selection['variant_id']):
        raise ValueError('Catch source row identity mismatch')
    total=durations['provider_total_seconds']
    if total is None:total=selection['provider_duration']
    hit=durations['provider_hit_seconds']
    if hit is None:hit=durations['mandatory_scoring_seconds']
    return {'beatmap_id':selection['map_id'],'beatmapset_id':selection['set_id'],
        'title':presentation.get('title_unicode') or selection['title'],
        'artist':presentation.get('artist_unicode') or selection['artist'],
        'version':selection['version'],'total_length':total,'hit_length':hit,
        'sr':difficulty.get('provider_stars') if difficulty.get('provider_stars') is not None else selection.get('stars')}


def _source_rows(source):
    db,before=_source_db(source);mode=source['mode'];table=source['table'];count=0
    try:
        if mode=='catch':
            query='SELECT beatmap_id,variant_id,payload FROM catch_maps ORDER BY beatmap_id'
            for row in db.execute(query):
                count+=1
                if count>MAX_ROWS_PER_SOURCE:raise ValueError('display index source row limit exceeded')
                yield _row('catch',_catch_values(source,row))
        else:
            selected=[name for name in ('beatmap_id','beatmapset_id','title','artist','version',
                'total_length','hit_length','sr','mode_int','key_count') if name in TABLES[mode][2]]
            info={row['name'] for row in db.execute('PRAGMA table_info('+table+')')}
            query_columns=[name for name in selected if name in info]
            sql='SELECT '+','.join(query_columns)+' FROM '+table+' ORDER BY beatmap_id'
            output_mode='standard' if mode=='loved' else mode
            for row in db.execute(sql):
                count+=1
                if count>MAX_ROWS_PER_SOURCE:raise ValueError('display index source row limit exceeded')
                values=dict(row)
                if mode in ('mania_4k','mania_7k'):
                    expected_keys=4 if mode=='mania_4k' else 7
                    if values.get('mode_int')!=3 or values.get('key_count')!=expected_keys:
                        raise ValueError('mania display source row has a wrong ruleset or key count')
                if values['beatmap_id'] in EXCLUDED_MAPS:continue
                yield _row(output_mode,values)
    finally:
        db.close()
    if _fingerprint(source['path'])!=before or _sha256(source['path'])!=source['sha256']:
        raise ValueError('display index source changed while reading')
    if any(Path(str(source['path'])+suffix).exists() for suffix in ('-wal','-shm','-journal')):
        raise ValueError('display index source acquired SQLite sidecars')
    return count


def build(inputs, output):
    """Build and atomically seal an index; return a bounded integrity report."""
    sources=_descriptor(inputs);output=Path(output).absolute();parent=output.parent
    if not parent.is_dir() or parent.is_symlink():raise ValueError('display index output directory must exist and be real')
    if output.is_symlink() or output.exists() and not output.is_file():
        raise ValueError('display index output target is not a regular file')
    fd,temp_name=tempfile.mkstemp(prefix='.display-index-',suffix='.sqlite3',dir=parent)
    os.close(fd);temporary=Path(temp_name);counts={mode:0 for mode in sorted(OUTPUT_MODES)}
    sources_report=[];duplicates=0
    try:
        db=sqlite3.connect(temporary,timeout=5)
        try:
            db.execute('PRAGMA journal_mode=DELETE');db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE display_maps (mode TEXT NOT NULL, beatmap_id INTEGER NOT NULL, '
                'beatmapset_id INTEGER, title TEXT, artist TEXT, version TEXT, total_length REAL, '
                'hit_length REAL, sr REAL, PRIMARY KEY(mode,beatmap_id)) WITHOUT ROWID')
            for source in sources:
                before=_fingerprint(source['path'])
                if _sha256(source['path'])!=source['sha256']:
                    raise ValueError('display index source hash mismatch')
                input_count=0;inserted=0;source_duplicates=0
                for value in _source_rows(source):
                    input_count+=1
                    if value[1] in EXCLUDED_MAPS:continue
                    old=db.execute('SELECT '+','.join(OUTPUT_COLUMNS)+' FROM display_maps WHERE mode=? AND beatmap_id=?',
                                   (value[0],value[1])).fetchone()
                    if old is not None:
                        if tuple(old)!=value:
                            raise ValueError('conflicting display metadata for duplicate mode/map')
                        duplicates+=1;source_duplicates+=1;continue
                    if sum(counts.values())>=MAX_OUTPUT_ROWS:
                        raise ValueError('display index output row limit exceeded')
                    db.execute('INSERT INTO display_maps VALUES(?,?,?,?,?,?,?,?,?)',value)
                    counts[value[0]]+=1;inserted+=1
                if _fingerprint(source['path'])!=before or _sha256(source['path'])!=source['sha256']:
                    raise ValueError('display index source changed while building')
                sources_report.append({'mode':source['mode'],'table':source['table'],
                    'sha256':source['sha256'],'rows_read':input_count,
                    'rows_inserted':inserted,'identical_duplicates':source_duplicates})
            if any(counts[mode]==0 for mode in OUTPUT_MODES):
                raise ValueError('display index source set produced an empty mode')
            db.commit()
            if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
                raise ValueError('display index integrity check failed')
        finally:db.close()
        out_size=temporary.stat().st_size
        if not 0<out_size<=64*1024**2:raise ValueError('display index output size limit exceeded')
        with temporary.open('rb') as stream:os.fsync(stream.fileno())
        out_hash=_sha256(temporary)
        os.chmod(temporary,0o444)
        os.replace(temporary,output)
        directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return {'format':1,'output':str(output),'sha256':out_hash,'bytes':out_size,
            'rows_by_mode':counts,'total_rows':sum(counts.values()),
            'identical_duplicates_ignored':duplicates,'sources':sources_report}
    finally:
        for suffix in ('','-wal','-shm','-journal'):
            sidecar=Path(str(temporary)+suffix)
            if sidecar.exists():sidecar.unlink()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',required=True,type=Path,help='root-supplied hash-pinned source descriptor JSON')
    parser.add_argument('--output',required=True,type=Path,help='explicit display index destination')
    args=parser.parse_args(argv)
    try:report=build(args.inputs,args.output)
    except (OSError,ValueError,sqlite3.Error,KeyError,TypeError) as error:
        parser.error(str(error))
    print(json.dumps(report,sort_keys=True,separators=(',',':')))
    return 0


if __name__=='__main__':raise SystemExit(main())
