"""Minimal, deterministic local catalog packages. M2-A has no network or trust authority.

Inputs are operator-supplied isolated copies. This exporter never copies source
SQLite pages/schema or arbitrary columns. Authenticity is a separate M2-B step.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import tempfile

from .config import ConfigError

FORMAT = 1
READER = 'poolrotater-selection-v1'
MAX_FILE = 512 * 1024**2
MAX_ROWS = 500_000
MAX_TEXT = 4096
MODES = {'standard': (0, None), 'mania4': (3, 4), 'mania7': (3, 7), 'catch': (2, None), 'loved': (0, None), 'taiko': (1, None)}
CORE_MODES = ('standard', 'mania4', 'mania7')

def complete_modes(value):
    return type(value) is dict and set(CORE_MODES) <= set(value) <= set(MODES)

EXCLUDED = (1549496,)
HASH = re.compile(r'[0-9a-f]{64}\Z')
# Entries are fixed (column name, SQLite type) pairs; never source-provided SQL.
def columns(text):
    return tuple(tuple(entry.split(':')) for entry in text.split())

BASE = columns('beatmap_id:INTEGER artist:TEXT title:TEXT version:TEXT sr:REAL hit_length:INTEGER passcount:INTEGER status:TEXT ranked_date:TEXT')
STANDARD = BASE + columns('ar:REAL')
FARM = columns('beatmap_id:INTEGER artist:TEXT title:TEXT version:TEXT sr:REAL hit_length:INTEGER passcount:INTEGER farm_score:REAL quadrant:TEXT forgiveness:REAL max_pp:REAL dominant_mods:TEXT')
STREAM = columns('beatmap_id:INTEGER classifier_version:TEXT stream_score:REAL median_stream_bpm:REAL max_stream_notes:INTEGER long_stream_note_ratio:REAL')
MANIA = BASE + columns('mode_int:INTEGER key_count:INTEGER ar:REAL')
FEATURES = columns('beatmap_id:INTEGER classifier_version:TEXT key_count:INTEGER jack_score:REAL chordjack_score:REAL stream_score:REAL ln_score:REAL technical_score:REAL max_jack_run:INTEGER jack_ratio:REAL hold_ratio:REAL max_chordjack_run:INTEGER chordjack_event_ratio:REAL max_stream_run:INTEGER stream_event_ratio:REAL chord_event_ratio:REAL hold_coverage:REAL rhythm_cv:REAL')
OVERREP = columns('beatmap_id:INTEGER rank_score:REAL confidence:REAL raw_appearances:INTEGER band_count:INTEGER')
MANIA_FARM = columns('beatmap_id:INTEGER key_count:INTEGER farm_index:REAL confidence:REAL model_version:TEXT')
DT = columns('beatmap_id:INTEGER artist:TEXT title:TEXT version:TEXT nm_sr:REAL dt_sr:REAL hit_length:INTEGER source_hit_length:INTEGER passcount:INTEGER ranked_date:TEXT farm_score:REAL dominant_mods:TEXT stream_score:REAL stream_bpm:REAL checksum:TEXT calculator:TEXT')
CONVERT_CORE = BASE + columns('key_count:INTEGER checked_at:REAL unique_players:INTEGER')
CONVERT_OPTIONAL = columns('classifier_version:TEXT jack_score:REAL chordjack_score:REAL stream_score:REAL ln_score:REAL technical_score:REAL')
CONVERT = CONVERT_CORE + CONVERT_OPTIONAL
CONVERT_VALIDATION = columns('beatmap_id:INTEGER catalog_keys:INTEGER catalog_sr:REAL catalog_checked_at:REAL actual_keys:INTEGER converted_sr:REAL source_sr:REAL source_sha256:TEXT backend:TEXT verified_at:REAL')
CONVERT_BINDING = 'catalog-revision-and-actual-keys-v1'
META = columns('key:TEXT value:TEXT')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024**2), b''):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode() + b'\n'


def schemas(mode, role):
    if mode=='taiko' and role=='catalog':
        from .catalog_taiko import MAP_COLUMNS,CALIBRATION_COLUMNS
        return {'taiko_maps':MAP_COLUMNS,'taiko_auto_calibration':CALIBRATION_COLUMNS}
    if mode == 'loved' and role == 'catalog':
        from .catalog_loved import COLUMNS
        return {'beatmaps': COLUMNS}
    if role == 'converts' and mode in ('mania4', 'mania7'):
        return {'convert_maps': CONVERT, 'verified_converts': CONVERT_VALIDATION, 'metadata': META}
    if role == 'dt' and mode == 'standard':
        return {'dt_maps': DT, 'metadata': META}
    if role == 'farm' and mode != 'standard':
        return {'mania_farm_efficiency': MANIA_FARM, 'metadata': META}
    if role != 'catalog':
        raise ConfigError('invalid catalog companion role')
    if mode == 'standard':
        return {'beatmaps': STANDARD, 'farm_report': FARM, 'stream_features': STREAM}
    report = 'mania_overrep_report' if mode == 'mania4' else 'mania_7k_overrep_report'
    return {'maps': MANIA, 'mania_features': FEATURES, report: OVERREP}


def fingerprint(path):
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


@contextmanager
def source(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ConfigError('catalog input must be a bounded regular file')
    if any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ConfigError('catalog input must be a settled SQLite copy')
    before = fingerprint(path)
    digest = sha256(path)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 256 * 1024)
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ConfigError('catalog input integrity failed')
        yield db, {'sha256': digest, 'bytes': before[2]}
        if (fingerprint(path) != before or sha256(path) != digest
                or any(Path(str(path) + suffix).exists() for suffix in ('-wal', '-shm', '-journal'))):
            raise ConfigError('catalog input changed during export')
    finally:
        db.close()


def metadata(db, allowed):
    pairs = db.execute('SELECT key,value FROM metadata').fetchall()
    if len(pairs) > 32 or len({key for key, _ in pairs}) != len(pairs):
        raise ConfigError('invalid companion metadata')
    result = {key: value for key, value in pairs if key in allowed}
    if any(type(key) is not str or type(value) is not str or len(value) > MAX_TEXT for key, value in pairs):
        raise ConfigError('invalid companion metadata type')
    return result


def validate_value(name, kind, value):
    if value is None and name in {'ranked_date', 'ar', 'median_stream_bpm'}:
        return
    if kind == 'TEXT':
        if type(value) is not str or len(value.encode('utf-8')) > MAX_TEXT or '\x00' in value:
            raise ConfigError('invalid text in catalog export')
    elif kind == 'INTEGER':
        if type(value) is not int or value < 0 or value > 2**63 - 1:
            raise ConfigError('invalid integer in catalog export')
    elif (type(value) not in (int, float) or not math.isfinite(value)
          or (value < 0 and name not in {'farm_score', 'forgiveness', 'rank_score'})):
        raise ConfigError('invalid finite numeric value in catalog export')
    if name == 'beatmap_id' and value <= 0:
        raise ConfigError('invalid beatmap identity')
    if name in {'sr', 'nm_sr', 'dt_sr'} and not 0 < value <= 100:
        raise ConfigError('invalid catalog star rating')
    if name in {'confidence', 'hold_ratio', 'hold_coverage', 'jack_ratio', 'chordjack_event_ratio', 'stream_event_ratio', 'chord_event_ratio', 'long_stream_note_ratio'} and not 0 <= value <= 1:
        raise ConfigError('invalid catalog ratio')
    if name == 'farm_index' and not 0 <= value <= 100:
        raise ConfigError('invalid farm signal')


def create_table(db, table, fields):
    parts = [f'{name} {kind}' + (' PRIMARY KEY' if name in {'beatmap_id', 'key'} else '') for name, kind in fields]
    db.execute(f'CREATE TABLE {table} ({", ".join(parts)})')



def validate_row(fields, row, *, mode, table):
    for (name, kind), value in zip(fields, row):
        if table == 'convert_maps' and (name, kind) in CONVERT_OPTIONAL and value is None:
            continue
        validate_value(name, kind, value)
    item = dict(zip((name for name, _ in fields), row))
    if 'key_count' in item and item['key_count'] != MODES[mode][1]:
        raise ConfigError('catalog contains wrong native key count')
    if 'mode_int' in item and item['mode_int'] != 3:
        raise ConfigError('catalog contains wrong native ruleset')
    if table == 'mania_farm_efficiency' and item['model_version'] != 'mania_farm_efficiency_v1':
        raise ConfigError('mixed or unsupported Mania farm model')
    if table == 'dt_maps':
        from bot.standard_pool import DT_CALCULATOR
        if item['calculator'] != DT_CALCULATOR or not re.fullmatch('[0-9a-f]{32}', item['checksum']):
            raise ConfigError('invalid DT calculator/checksum evidence')
        if item['hit_length'] != math.ceil(item['source_hit_length'] / 1.5):
            raise ConfigError('invalid DT playing length')
    if table == 'verified_converts':
        if (item['catalog_keys'] != MODES[mode][1] or item['actual_keys'] != MODES[mode][1]
                or any(not 0 < item[k] <= 100 for k in ('catalog_sr','converted_sr','source_sr'))
                or not HASH.fullmatch(item['source_sha256']) or not item['backend']
                or item['verified_at'] <= 0):
            raise ConfigError('invalid Mania conversion verification evidence')
    return item


def _project_rows(db, table, fields, *, optional=()):
    """Static selection projection only; never copy source SQL/private columns."""
    obj = db.execute('SELECT type,sql FROM sqlite_master WHERE name=?',(table,)).fetchone()
    if not obj or obj[0] != 'table' or not obj[1] or 'VIRTUAL TABLE' in obj[1].upper():
        raise ConfigError('required conversion table is absent or unsupported')
    available={r[1] for r in db.execute(f'PRAGMA table_info({table})')}
    if not {name for name,_ in fields if name not in optional} <= available:
        raise ConfigError('required conversion columns are missing')
    projection=','.join(name if name in available else 'NULL' for name,_ in fields)
    seen=set()
    for index,row in enumerate(db.execute(f'SELECT {projection} FROM {table} ORDER BY beatmap_id')):
        if index >= MAX_ROWS: raise ConfigError('conversion row budget exceeded')
        item=dict(zip((name for name,_ in fields),row))
        bid=item['beatmap_id']
        if type(bid) is not int or bid<=0 or bid in seen:
            raise ConfigError('invalid or duplicate conversion beatmap identity')
        seen.add(bid)
        yield item


def _conversion_matches(candidate, evidence, keys):
    """Match the unchanged native reader's independent revision/key admission."""
    return (evidence is not None and candidate['key_count']==keys
            and evidence['catalog_keys']==candidate['key_count']
            and evidence['actual_keys']==keys and evidence['catalog_sr']==candidate['sr']
            and evidence['catalog_checked_at']==candidate['checked_at'])


def write_converts(path, candidates, validation, mode, *, meta):
    verified={r['beatmap_id']:r for r in _project_rows(validation,'verified_converts',CONVERT_VALIDATION)}
    retained=[]
    for item in _project_rows(candidates,'convert_maps',CONVERT,
                              optional={name for name,_ in CONVERT_OPTIONAL}):
        evidence=verified.get(item['beatmap_id'])
        if item['beatmap_id'] in EXCLUDED or not _conversion_matches(item,evidence,MODES[mode][1]):
            continue
        validate_row(CONVERT,tuple(item.values()),mode=mode,table='convert_maps')
        validate_row(CONVERT_VALIDATION,tuple(evidence.values()),mode=mode,table='verified_converts')
        retained.append((item,evidence))
    with sqlite3.connect(path) as out:
        out.execute('PRAGMA journal_mode=DELETE')
        out.execute('PRAGMA application_id=1347572547')
        out.execute('PRAGMA user_version=1')
        for table,fields in schemas(mode,'converts').items(): create_table(out,table,fields)
        for item,evidence in retained:
            out.execute(f'INSERT INTO convert_maps VALUES ({",".join("?" for _ in CONVERT)})',tuple(item.values()))
            out.execute(f'INSERT INTO verified_converts VALUES ({",".join("?" for _ in CONVERT_VALIDATION)})',tuple(evidence.values()))
        out.executemany('INSERT INTO metadata VALUES (?,?)',sorted(meta.items()))
        out.execute('CREATE INDEX convert_selection ON convert_maps(key_count,hit_length,passcount)')
        out.commit()
        if out.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
            raise ConfigError('exported conversion integrity failed')
    return {'convert_maps':len(retained),'verified_converts':len(retained)}


def copy_table(src, dst, table, fields, *, mode, ids=None):
    obj = src.execute('SELECT type,sql FROM sqlite_master WHERE name=?', (table,)).fetchone()
    if not obj or obj[0] != 'table' or not obj[1] or 'VIRTUAL TABLE' in obj[1].upper():
        raise ConfigError('required catalog table is absent or unsupported')
    actual = {r[1] for r in src.execute(f'PRAGMA table_info({table})')}
    if not {name for name, _ in fields} <= actual:
        raise ConfigError('required catalog columns are missing')
    create_table(dst, table, fields)
    names = [name for name, _ in fields]
    count = 0
    retained = set()
    insert = f'INSERT INTO {table} VALUES ({",".join("?" for _ in fields)})'
    for row in src.execute(f'SELECT {",".join(names)} FROM {table} ORDER BY beatmap_id'):
        count += 1
        if count > MAX_ROWS:
            raise ConfigError('catalog row budget exceeded')
        item = validate_row(fields, row, mode=mode, table=table)
        bid = item['beatmap_id']
        if bid in retained:
            raise ConfigError('duplicate catalog beatmap identity')
        retained.add(bid)
        if bid in EXCLUDED:
            continue
        if ids is not None and bid not in ids:
            raise ConfigError('companion contains an unknown beatmap')
        dst.execute(insert, row)
    return retained - set(EXCLUDED)


def write_db(path, src, mode, role, *, ids=None, meta=None):
    tables = schemas(mode, role)
    with sqlite3.connect(path) as out:
        out.execute('PRAGMA page_size=4096')
        out.execute('PRAGMA journal_mode=DELETE')
        out.execute('PRAGMA application_id=1347572547')
        out.execute('PRAGMA user_version=1')
        count = {}
        for table, fields in tables.items():
            if table == 'metadata':
                create_table(out, table, fields)
                out.executemany('INSERT INTO metadata VALUES (?,?)', sorted(meta.items()))
            else:
                retained = copy_table(src, out, table, fields, mode=mode, ids=ids)
                if table in {'maps', 'beatmaps'}:
                    ids = retained
                count[table] = out.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        if mode != 'standard' and role == 'catalog' and count.get('maps') != count.get('mania_features'):
            raise ConfigError('native structural evidence coverage is incomplete')
        if not count or not all(count.values()):
            raise ConfigError('catalog lacks required selection evidence')
        # Preserve the actual query ordering while indexes avoid scans for ID lookup.
        if role == 'catalog':
            table = 'beatmaps' if mode == 'standard' else 'maps'
            out.execute(f'CREATE INDEX selection_window ON {table}(sr,hit_length,passcount)')
        if role == 'dt':
            out.execute('CREATE INDEX dt_stars ON dt_maps(dt_sr)')
        out.commit()
        if out.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ConfigError('exported catalog integrity failed')
    return ids, count


def _identity(path, role, tables):
    return {'name': path.name, 'role': role, 'sha256': sha256(path), 'bytes': path.stat().st_size, 'tables': tables}


def _pack(folder, files):
    path = folder / 'bundle.tar.gz'
    with path.open('xb') as raw, gzip.GzipFile(fileobj=raw, filename='', mode='wb', mtime=0, compresslevel=6) as zipped, tarfile.open(fileobj=zipped, mode='w|', format=tarfile.USTAR_FORMAT) as archive:
        for item in sorted(files, key=lambda item: item['name']):
            info = tarfile.TarInfo(item['name'])
            info.size = item['bytes']
            info.mode = 0o444
            with (folder / item['name']).open('rb') as data:
                archive.addfile(info, data)
    return {'name': path.name, 'sha256': sha256(path), 'bytes': path.stat().st_size,
            'expanded_bytes': sum(item['bytes'] for item in files), 'codec': 'tar+gzip-v1'}


def export_bundle(catalog, destination, mode, *, dt=None, farm=None, converts=None, convert_validation=None):
    """Build a new local export directory; never overwrite an installed package."""
    if (mode in ('catch','loved','taiko') or mode not in MODES or (dt is not None and mode != 'standard')
            or (farm is not None and mode == 'standard')
            or ((converts is None)!=(convert_validation is None))
            or (converts is not None and mode=='standard')):
        raise ConfigError('invalid export mode/companion combination')
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ConfigError('export destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.catalog-export-', dir=destination.parent))
    try:
        with ExitStack() as stack:
            db, origin = stack.enter_context(source(catalog))
            inputs = {'catalog': origin}
            _, counts = write_db(temporary / 'catalog.db', db, mode, 'catalog')
            with sqlite3.connect((temporary / 'catalog.db').as_uri() + '?mode=ro', uri=True) as clean:
                table = 'beatmaps' if mode == 'standard' else 'maps'
                ids = {row[0] for row in clean.execute(f'SELECT beatmap_id FROM {table}')}
                feature_table = 'stream_features' if mode == 'standard' else 'mania_features'
                classifiers = [row[0] for row in clean.execute(f'SELECT DISTINCT classifier_version FROM {feature_table} ORDER BY classifier_version')]
            if len(classifiers) != 1 or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', classifiers[0]):
                raise ConfigError('unqualified or mixed structural classifiers')
            files = [_identity(temporary / 'catalog.db', 'catalog', counts)]
            capabilities = {'styles': ['variety', 'farm', 'stream'] if mode == 'standard' else ['variety', 'jack', 'chordjack', 'stream', 'ln', 'technical', 'rice', 'noodle'],
                            'mods': ['NM'], 'companions': [], 'unavailable': {'loved': 'no qualified catalog', 'converts': 'no matching source/evidence pair'}}
            analysis = {'structural_classifier': classifiers[0], 'farm': 'prepared-source-derived; no calculator claim'}
            if mode == 'standard':
                from bot.jump_catalog import CLASSIFIER_VERSION, ANALYSIS_SHA256
                capabilities['styles'].append('jump')
                analysis['jump'] = {'classifier': CLASSIFIER_VERSION, 'sha256': ANALYSIS_SHA256, 'binding': 'embedded-reader-evidence'}
            if dt is not None:
                from bot.standard_pool import DT_CALCULATOR
                ddb, dinfo = stack.enter_context(source(dt))
                inputs['dt'] = dinfo
                original = metadata(ddb, {'format', 'mods', 'clock_rate', 'calculator', 'built_at', 'source_size', 'source_mtime_ns', 'source_sha256'})
                try:
                    valid = (original['format'] == '1' and original['mods'] == 'DT' and original['clock_rate'] == '1.5'
                             and original['calculator'] == DT_CALCULATOR and math.isfinite(float(original['built_at']))
                             and float(original['built_at']) > 0 and int(original['source_size']) == origin['bytes'])
                    if 'source_sha256' in original:
                        valid = valid and original['source_sha256'] == origin['sha256']
                    else:
                        valid = valid and int(original['source_mtime_ns']) == Path(catalog).stat().st_mtime_ns
                except (KeyError, ValueError, TypeError):
                    valid = False
                if not valid:
                    raise ConfigError('DT evidence does not belong to the source catalog')
                managed = {key: original[key] for key in ('mods', 'clock_rate', 'calculator', 'built_at')}
                managed.update(format='2', source_sha256=files[0]['sha256'], origin_source_sha256=origin['sha256'])
                _, counts = write_db(temporary / 'dt.db', ddb, mode, 'dt', ids=ids, meta=managed)
                files.append(_identity(temporary / 'dt.db', 'dt', counts))
                capabilities['mods'].append('DT')
                capabilities['companions'].append('dt')
                analysis['dt'] = {'calculator': DT_CALCULATOR, 'built_at': original['built_at'], 'clock_rate': '1.5'}
            elif mode == 'standard':
                capabilities['unavailable']['DT'] = 'no matching prepared companion'
            if farm is not None:
                fdb, finfo = stack.enter_context(source(farm))
                inputs['farm'] = finfo
                meta = metadata(fdb, {'model_version', 'key_count'})
                if meta != {'model_version': 'mania_farm_efficiency_v1', 'key_count': str(MODES[mode][1])}:
                    raise ConfigError('unqualified Mania farm metadata')
                _, counts = write_db(temporary / 'farm.db', fdb, mode, 'farm', ids=ids, meta=meta)
                files.append(_identity(temporary / 'farm.db', 'farm', counts))
                capabilities['companions'].append('farm')
                capabilities['styles'].append('farm')
                analysis['farm'] = meta['model_version']
            elif mode != 'standard':
                capabilities['unavailable']['farm'] = 'no qualified efficiency companion'
            if converts is not None:
                cdb,cinfo=stack.enter_context(source(converts))
                vdb,vinfo=stack.enter_context(source(convert_validation))
                inputs['converts']=cinfo
                provenance={'binding':CONVERT_BINDING,'validation_sha256':vinfo['sha256'],
                            'validation_bytes':vinfo['bytes']}
                meta={'format':'1','keys':str(MODES[mode][1]),'binding':CONVERT_BINDING,
                      'candidate_source_sha256':cinfo['sha256'],'validation_source_sha256':vinfo['sha256']}
                counts=write_converts(temporary/'converts.db',cdb,vdb,mode,meta=meta)
                files.append(_identity(temporary/'converts.db','converts',counts))
                capabilities['companions'].append('converts')
                capabilities['unavailable'].pop('converts')
                analysis['converts']=provenance
            archive = _pack(temporary, files)
            manifest = {'format': FORMAT, 'export_schema': 1, 'reader': READER, 'mode': mode,
                        'ruleset': MODES[mode][0], 'keys': MODES[mode][1], 'files': files,
                        'sources': inputs, 'analysis': analysis, 'capabilities': capabilities,
                        'exclusions': {'revision': 1, 'beatmap_ids': list(EXCLUDED)}, 'archive': archive}
            manifest['content_id'] = hashlib.sha256(canonical(manifest)).hexdigest()
            validate_manifest(manifest)
            (temporary / 'manifest.json').write_bytes(canonical(manifest))
        # Source snapshots have been rechecked before publication of the local set.
        os.rename(temporary, destination)
        return manifest
    except Exception:
        shutil.rmtree(temporary)
        raise


def _closed(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ConfigError('catalog manifest has unknown or missing fields')


def _hash(value):
    if type(value) is not str or not HASH.fullmatch(value):
        raise ConfigError('catalog manifest requires a SHA256 identity')


def _size(value, *, maximum=MAX_FILE):
    if type(value) is not int or not 0 < value <= maximum:
        raise ConfigError('catalog manifest size outside limits')


def validate_manifest(manifest):
    """Closed application contract, not an authenticity assertion."""
    _closed(manifest, {'format', 'export_schema', 'reader', 'mode', 'ruleset', 'keys', 'files', 'sources', 'analysis', 'capabilities', 'exclusions', 'archive', 'content_id'})
    mode = manifest['mode']
    if mode == 'loved':
        from .catalog_loved import validate_manifest as validate_loved
        return validate_loved(manifest)
    if mode == 'taiko':
        from .catalog_taiko import validate_manifest as validate_taiko
        return validate_taiko(manifest)
    if mode == 'catch':
        from .catalog_catch import validate_manifest as validate_catch
        return validate_catch(manifest)
    if type(mode) is not str or mode not in MODES or type(manifest['format']) is not int or manifest['format'] != 1 or type(manifest['export_schema']) is not int or manifest['export_schema'] != 1 or manifest['reader'] != READER:
        raise ConfigError('unsupported catalog manifest/reader')
    if type(manifest['ruleset']) is not int or (manifest['ruleset'], manifest['keys']) != MODES[mode] or (mode != 'standard' and type(manifest['keys']) is not int):
        raise ConfigError('catalog manifest mode mismatch')
    files = manifest['files']
    if type(files) is not list or not 1 <= len(files) <= (2 if mode=='standard' else 3):
        raise ConfigError('catalog manifest file budget exceeded')
    roles = []
    for item in files:
        _closed(item, {'name', 'role', 'sha256', 'bytes', 'tables'})
        role = item['role']
        if type(role) is not str or role not in {'catalog', 'dt', 'farm','converts'} or item['name'] != role + '.db':
            raise ConfigError('unexpected catalog file identity')
        fields = schemas(mode, role)
        _closed(item['tables'], set(fields) - {'metadata'})
        for count in item['tables'].values():
            if role=='converts':
                if type(count) is not int or not 0<=count<=MAX_ROWS:
                    raise ConfigError('conversion count outside limits')
            else: _size(count, maximum=MAX_ROWS)
        if role=='converts' and item['tables']['convert_maps']!=item['tables']['verified_converts']:
            raise ConfigError('conversion evidence coverage mismatch')
        _hash(item['sha256']); _size(item['bytes'])
        roles.append(role)
    if roles[0] != 'catalog' or len(roles) != len(set(roles)):
        raise ConfigError('duplicate or incomplete catalog roles')
    _closed(manifest['sources'], roles)
    for origin in manifest['sources'].values():
        _closed(origin, {'sha256', 'bytes'}); _hash(origin['sha256']); _size(origin['bytes'])
    _closed(manifest['capabilities'], {'styles', 'mods', 'companions', 'unavailable'})
    capabilities = manifest['capabilities']
    expected_styles = ['variety', 'farm', 'stream', 'jump'] if mode == 'standard' else ['variety', 'jack', 'chordjack', 'stream', 'ln', 'technical', 'rice', 'noodle'] + (['farm'] if 'farm' in roles else [])
    if capabilities['styles'] != expected_styles or capabilities['mods'] != (['NM', 'DT'] if 'dt' in roles else ['NM']) or capabilities['companions'] != roles[1:]:
        raise ConfigError('catalog capabilities do not match required evidence')
    expected_unavailable = {'loved': 'no qualified catalog', 'converts': 'no matching source/evidence pair'}
    if 'converts' in roles: expected_unavailable.pop('converts')
    if mode == 'standard' and 'dt' not in roles:
        expected_unavailable['DT'] = 'no matching prepared companion'
    if mode != 'standard' and 'farm' not in roles:
        expected_unavailable['farm'] = 'no qualified efficiency companion'
    if capabilities['unavailable'] != expected_unavailable:
        raise ConfigError('invalid catalog unavailable capabilities')
    _closed(manifest['analysis'], {'structural_classifier', 'farm'} | ({'jump'} if mode == 'standard' else set()) | ({'dt'} if 'dt' in roles else set()) | ({'converts'} if 'converts' in roles else set()))
    expected_classifier = 'stream_v3' if mode == 'standard' else 'mania_patterns_v1'
    if manifest['analysis']['structural_classifier'] != expected_classifier or manifest['analysis']['farm'] != ('mania_farm_efficiency_v1' if 'farm' in roles else 'prepared-source-derived; no calculator claim'):
        raise ConfigError('unsupported catalog analysis provenance')
    if mode == 'standard':
        from bot.jump_catalog import CLASSIFIER_VERSION, ANALYSIS_SHA256
        if manifest['analysis']['jump'] != {'classifier': CLASSIFIER_VERSION, 'sha256': ANALYSIS_SHA256, 'binding': 'embedded-reader-evidence'}:
            raise ConfigError('incompatible embedded Jump evidence')
    if 'dt' in roles:
        from bot.standard_pool import DT_CALCULATOR
        evidence = manifest['analysis']['dt']
        _closed(evidence, {'calculator', 'built_at', 'clock_rate'})
        try:
            valid = type(evidence['built_at']) is str and 0 < float(evidence['built_at']) < math.inf
        except (ValueError, TypeError):
            valid = False
        if not valid or evidence['calculator'] != DT_CALCULATOR or evidence['clock_rate'] != '1.5':
            raise ConfigError('incompatible DT evidence provenance')
    if 'converts' in roles:
        evidence=manifest['analysis']['converts']
        _closed(evidence,{'binding','validation_sha256','validation_bytes'})
        _hash(evidence['validation_sha256']);_size(evidence['validation_bytes'])
        if evidence['binding']!=CONVERT_BINDING:
            raise ConfigError('incompatible conversion evidence provenance')
    _closed(manifest['exclusions'], {'revision', 'beatmap_ids'})
    if (type(manifest['exclusions']['revision']) is not int
            or type(manifest['exclusions']['beatmap_ids']) is not list
            or any(type(value) is not int for value in manifest['exclusions']['beatmap_ids'])
            or manifest['exclusions'] != {'revision': 1, 'beatmap_ids': list(EXCLUDED)}):
        raise ConfigError('initial catalog exclusions must be preserved')
    archive = manifest['archive']
    _closed(archive, {'name', 'sha256', 'bytes', 'expanded_bytes', 'codec'})
    _hash(archive['sha256']); _size(archive['bytes'])
    _size(archive['expanded_bytes'], maximum=2 * MAX_FILE)
    if archive['name'] != 'bundle.tar.gz' or archive['codec'] != 'tar+gzip-v1' or archive['expanded_bytes'] != sum(item['bytes'] for item in files):
        raise ConfigError('incompatible catalog archive contract')
    _hash(manifest['content_id'])
    body = {key: value for key, value in manifest.items() if key != 'content_id'}
    if hashlib.sha256(canonical(body)).hexdigest() != manifest['content_id']:
        raise ConfigError('catalog content identity mismatch')
    return manifest



def read_manifest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ConfigError('catalog manifest must be a bounded regular file')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ConfigError('duplicate catalog manifest field')
            result[key] = value
        return result
    try:
        manifest = json.loads(path.read_bytes(), object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise ConfigError('invalid catalog manifest JSON') from exc
    return validate_manifest(manifest)


def inspect_bundle(folder):
    """Validate a local export without granting signature/activation authority."""
    folder = Path(folder)
    manifest = read_manifest(folder / 'manifest.json')
    expected = {'manifest.json', 'bundle.tar.gz'} | {item['name'] for item in manifest['files']}
    if {path.name for path in folder.iterdir()} != expected:
        raise ConfigError('unexpected files in catalog export')
    for item in manifest['files'] + [manifest['archive']]:
        path = folder / item['name']
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item['bytes'] or sha256(path) != item['sha256']:
            raise ConfigError('catalog export content mismatch')
    if manifest['mode'] == 'loved':
        from .catalog_loved import inspect as inspect_loved
        return inspect_loved(folder, manifest)
    if manifest['mode'] == 'taiko':
        from .catalog_taiko import inspect as inspect_taiko
        return inspect_taiko(folder, manifest)
    if manifest['mode'] == 'catch':
        from .catalog_catch import inspect as inspect_catch
        return inspect_catch(folder, manifest)
    ids = None
    for item in manifest['files']:
        with source(folder / item['name']) as (db, _origin):
            fields = schemas(manifest['mode'], item['role'])
            objects = db.execute("SELECT type,name FROM sqlite_master WHERE type IN ('table','view','trigger')").fetchall()
            if sorted(objects) != sorted(('table', name) for name in fields):
                raise ConfigError('catalog shipping table allowlist mismatch')
            for table, columns in fields.items():
                info = db.execute(f'PRAGMA table_info({table})').fetchall()
                actual = tuple((r[1], r[2]) for r in info)
                key = 'key' if table == 'metadata' else 'beatmap_id'
                if actual != columns or any(r[5] != (1 if r[1] == key else 0) for r in info):
                    raise ConfigError('catalog shipping column allowlist mismatch')
                if table != 'metadata':
                    if db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] != item['tables'][table]:
                        raise ConfigError('catalog shipping row count mismatch')
                    if db.execute(f'SELECT COUNT(*) FROM {table} WHERE beatmap_id=?', (EXCLUDED[0],)).fetchone()[0]:
                        raise ConfigError('excluded map present in catalog export')
                    for row in db.execute(f'SELECT {",".join(name for name, _ in columns)} FROM {table}'):
                        validate_row(columns, row, mode=manifest['mode'], table=table)
            if item['role'] == 'catalog':
                table = 'beatmaps' if manifest['mode'] == 'standard' else 'maps'
                ids = {row[0] for row in db.execute(f'SELECT beatmap_id FROM {table}')}
            elif item['role'] == 'dt':
                meta = dict(db.execute('SELECT key,value FROM metadata'))
                _closed(meta, {'format', 'source_sha256', 'origin_source_sha256', 'calculator', 'built_at', 'mods', 'clock_rate'})
                if (meta.get('mods') != 'DT' or meta.get('clock_rate') != '1.5'
                        or meta.get('format') != '2' or meta.get('source_sha256') != manifest['files'][0]['sha256']
                        or meta.get('origin_source_sha256') != manifest['sources']['catalog']['sha256']
                        or meta.get('calculator') != manifest['analysis']['dt']['calculator']
                        or meta.get('built_at') != manifest['analysis']['dt']['built_at']):
                    raise ConfigError('DT companion provenance mismatch')
            if item['role'] == 'farm':
                meta = dict(db.execute('SELECT key,value FROM metadata'))
                if meta != {'key_count':str(manifest['keys']), 'model_version':'mania_farm_efficiency_v1'}:
                    raise ConfigError('Mania farm companion provenance mismatch')
            if item['role']=='converts':
                meta=dict(db.execute('SELECT key,value FROM metadata'))
                expected_meta={'format':'1','keys':str(manifest['keys']),'binding':CONVERT_BINDING,
                    'candidate_source_sha256':manifest['sources']['converts']['sha256'],
                    'validation_source_sha256':manifest['analysis']['converts']['validation_sha256']}
                if meta!=expected_meta:
                    raise ConfigError('conversion companion provenance mismatch')
                candidates={r['beatmap_id']:r for r in _project_rows(db,'convert_maps',CONVERT)}
                verified={r['beatmap_id']:r for r in _project_rows(db,'verified_converts',CONVERT_VALIDATION)}
                if set(candidates)!=set(verified) or any(not _conversion_matches(c,verified[bid],manifest['keys']) for bid,c in candidates.items()):
                    raise ConfigError('conversion companion revision/key mismatch')
            if manifest['mode'] != 'standard' and item['role'] == 'catalog' and item['tables']['maps'] != item['tables']['mania_features']:
                raise ConfigError('native structural evidence coverage is incomplete')
            for table in set(fields) - {'metadata', 'maps', 'beatmaps','convert_maps','verified_converts'}:
                if any(row[0] not in ids for row in db.execute(f'SELECT beatmap_id FROM {table}')):
                    raise ConfigError('catalog companion map closure mismatch')
    return manifest


def pool_from_bundle(folder, *, sr_min=0, sr_max=20, source_mode='variety', limit=200):
    """Exercise the real readers from an unsigned local export; not a host install."""
    manifest = inspect_bundle(folder)
    folder = Path(folder)
    if source_mode not in manifest['capabilities']['styles']:
        raise ConfigError('source capability is unavailable in this catalog')
    options = dict(sr_min=sr_min, sr_max=sr_max, min_passcount=0, max_length=3600,
                   max_map_age_years=0, limit=limit, ratings_db=None)
    if manifest['mode'] == 'loved':
        from bot.pool import MapPool
        return MapPool(str(folder/'catalog.db'), loved_mode=True, **options)
    if manifest['mode'] == 'taiko':
        from bot.taiko_pool import TaikoPool
        return TaikoPool(folder/'catalog.db',purpose='production',**options)
    if manifest['mode'] == 'catch':
        from bot.catch_pool import ProductionCatchPool
        return ProductionCatchPool(folder/'catalog.db', purpose='production', **options)
    if manifest['mode'] == 'standard':
        if 'dt' in manifest['capabilities']['companions']:
            from bot.standard_pool import StandardPool
            pool = StandardPool(str(folder / 'catalog.db'), standard_dt_db=str(folder / 'dt.db'),
                                standard_catalog_binding={'source_sha256': manifest['files'][0]['sha256'],
                                                          'dt_sha256': manifest['files'][1]['sha256']}, **options)
        else:
            from bot.pool import MapPool
            pool = MapPool(str(folder / 'catalog.db'), **options)
        if source_mode != 'variety':
            # Source setters deliberately retain the previous pool on an empty
            # result; expose an empty requested source here, never false parity.
            changed = getattr(pool, 'set_' + source_mode + '_mode')()
            if not changed:
                pool.maps = []
        return pool
    from bot.mania_pool import ManiaPool
    return ManiaPool(str(folder / 'catalog.db'), mania_keys=manifest['keys'],
                     mania_source_mode=source_mode, mania_farm_enabled='farm' in manifest['capabilities']['companions'],
                     mania_farm_db=str(folder / 'farm.db') if 'farm' in manifest['capabilities']['companions'] else '',
                     mania_convert_db=str(folder/'converts.db') if 'converts' in manifest['capabilities']['companions'] else '',
                     mania_convert_validation_db=str(folder/'converts.db') if 'converts' in manifest['capabilities']['companions'] else None, **options)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--mode', required=True, choices=MODES)
    parser.add_argument('--dt', type=Path)
    parser.add_argument('--farm', type=Path)
    parser.add_argument('--converts',type=Path)
    parser.add_argument('--convert-validation',type=Path)
    args = parser.parse_args(argv)
    try:
        if args.mode == 'loved':
            if any(x is not None for x in (args.dt,args.farm,args.converts,args.convert_validation)):
                raise ConfigError('Loved has no qualified companions')
            from .catalog_loved import export as export_loved
            manifest=export_loved(args.catalog,args.destination)
        else:
            manifest = export_bundle(args.catalog, args.destination, args.mode, dt=args.dt, farm=args.farm,
                                     converts=args.converts,convert_validation=args.convert_validation)
    except (ConfigError, OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, (str(exc) if isinstance(exc, ConfigError) else type(exc).__name__) + '\n')
    print(json.dumps(manifest, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
