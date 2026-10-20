"""Closed native-NM production projection; research catalogs remain separate.

Authenticity is supplied by the host's existing signed catalog store. This
reader checks content and variant identity, and never grants signing authority.
Research evidence/cohorts, player rows, source SQL, map files and audio are not
part of this schema.
"""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

from .c6_schema import ROW, canonical, decode, sha, validate
from .errors import IntegrityError

SCHEMA = 'catch-native-selection-1'
CALCULATOR_ID = '40ee257db1209169dddb2e742cb16682d047a0eb43e98c0cd0bfc0870f9c3ef9'
MAX_BYTES = 256 * 1024**2
MAX_ROWS = 200000
MAX_PAYLOAD = 32 * 1024
SPEC = {k: ROW[k] for k in ('selection', 'identity', 'presentation', 'durations')}
SPEC.update(schema=['enum', SCHEMA],
            difficulty={'pp_scenarios': ROW['difficulty']['pp_scenarios']},
            provider_metadata={'passcount': ['enum', None]})
META_KEYS = {'schema', 'permission_id', 'source_manifest_sha256', 'rows', 'rows_sha256'}


def project(row):
    value = {k: row[k] for k in ('selection', 'identity', 'presentation', 'durations')}
    value.update(schema=SCHEMA, difficulty={'pp_scenarios': row['difficulty']['pp_scenarios']},
                 provider_metadata={'passcount': None})
    validate_row(value)
    return value


def validate_row(row):
    validate(row, SPEC)
    s, i, d = (row[k] for k in ('selection', 'identity', 'durations'))
    variant = sha(canonical({'map_sha256': s['map_sha256'], 'native': True,
                            'original_ruleset': 'fruits', 'ruleset': 'fruits',
                            'mods': [], 'clock_rate': 1.0}))
    cases = row['difficulty']['pp_scenarios']
    if (s['variant_id'] != variant or s['calculator_id'] != CALCULATOR_ID or s['map_id'] > 2**32-1
            or any(s[k] < 0 for k in ('stars', 'duration', 'ss_pp', 'AR', 'CS'))
            or s['duration'] != d['mandatory_scoring_seconds']
            or s['provider_duration'] != d['provider_total_seconds']
            or any(v is not None and v < 0 for v in d.values())
            or d['audio_seconds'] is not None
            or not cases or cases[0]['name'] != 'ss' or cases[0]['pp'] != s['ss_pp']
            or len({c['name'] for c in cases}) != len(cases)
            or any(c['pp'] < 0 or not 0 <= c['accuracy'] <= 1 for c in cases)):
        raise IntegrityError('invalid production Catch variant or judgement evidence')
    return row


def rows(path):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise IntegrityError('invalid production Catch file')
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_PAYLOAD)
            db.execute('PRAGMA query_only=ON'); db.execute('PRAGMA trusted_schema=OFF')
            objects = set(db.execute("SELECT type,name FROM sqlite_master WHERE type IN ('table','view','trigger')"))
            if objects != {('table', 'catch_maps'), ('table', 'metadata')}:
                raise IntegrityError('production Catch table allowlist mismatch')
            expected = {'catch_maps': (('beatmap_id','INTEGER'), ('variant_id','TEXT'), ('payload','TEXT')),
                        'metadata': (('key','TEXT'), ('value','TEXT'))}
            for table, columns in expected.items():
                info = db.execute(f'PRAGMA table_info({table})').fetchall()
                if tuple((r[1], r[2]) for r in info) != columns or any(r[5] != (1 if j==0 else 0) for j,r in enumerate(info)):
                    raise IntegrityError('production Catch column allowlist mismatch')
            if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise IntegrityError('production Catch integrity failed')
            pairs = db.execute('SELECT key,value FROM metadata').fetchall()
            meta = dict(pairs)
            if len(pairs)!=len(META_KEYS) or set(meta)!=META_KEYS or meta['schema']!=SCHEMA:
                raise IntegrityError('invalid production Catch metadata')
            for key in ('permission_id', 'source_manifest_sha256', 'rows_sha256'):
                validate(meta[key], 'sha256')
            count=0; digest=hashlib.sha256(); variants=set()
            for mid, variant, payload in db.execute('SELECT beatmap_id,variant_id,payload FROM catch_maps ORDER BY beatmap_id'):
                if type(payload) is not str or len(payload.encode())>MAX_PAYLOAD:
                    raise IntegrityError('production Catch row exceeds bound')
                row=validate_row(decode(payload)); count+=1
                if count>MAX_ROWS or type(mid) is not int or mid!=row['selection']['map_id'] or variant!=row['selection']['variant_id']:
                    raise IntegrityError('production Catch index/identity mismatch')
                if variant in variants:raise IntegrityError('duplicate production Catch variant')
                variants.add(variant)
                digest.update(canonical(row)+b'\n')
                yield row
            if not count or meta['rows']!=str(count) or meta['rows_sha256']!=digest.hexdigest():
                raise IntegrityError('production Catch content count/digest mismatch')
    except sqlite3.Error as exc:
        raise IntegrityError('invalid production Catch database') from exc
