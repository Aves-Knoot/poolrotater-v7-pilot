"""Secret-free production Taiko NM catalog projection from completed research receipts."""
from __future__ import annotations

from collections import Counter
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import statistics
import tempfile
import zlib

from .config import ConfigError
from .catalog_export import EXCLUDED, _pack, canonical, _closed, _hash, _size

READER = 'poolrotater-taiko-selection-v1'
MODE = 'taiko'
RULESET = 1
CALCULATOR = 'taiko-official-nm-1'
OFFICIAL_COMMIT = 'b11d0d87824f33984587438074f3791ce06b159b'
DIFFICULTY_VERSION = 20251020
ENGINE = 'eb723269bb9affbec0421497848e514ee12194a3e44b536a1bb330d16d8098c4'
MAP_COLUMNS = (
    ('beatmap_id', 'INTEGER'), ('beatmapset_id', 'INTEGER'), ('artist', 'TEXT'),
    ('title', 'TEXT'), ('version', 'TEXT'), ('status', 'TEXT'),
    ('original_mode', 'INTEGER'), ('is_convert', 'INTEGER'), ('source_md5', 'TEXT'),
    ('source_sha256', 'TEXT'), ('taiko_nm_variant', 'TEXT'), ('sr', 'REAL'),
    ('hit_length', 'INTEGER'), ('max_pp', 'REAL'), ('calculator_id', 'TEXT'),
    ('difficulty_version', 'INTEGER'), ('source_commit', 'TEXT'),
    ('background_url', 'TEXT'), ('bpm_min', 'REAL'), ('bpm_max', 'REAL'),
    ('hit_count', 'INTEGER'), ('big_hits', 'INTEGER'), ('centre_hits', 'INTEGER'),
    ('rim_hits', 'INTEGER'), ('peak_hits_per_second', 'REAL'),
    ('mean_hits_per_second', 'REAL'), ('colour_change_fraction', 'REAL'),
    ('maximum_mono_streak', 'INTEGER'),
)
MAX_ROWS = 100_000
CALIBRATION_TABLE = 'taiko_auto_calibration'
CALIBRATION_COLUMNS = (('pp','REAL'),('sr','REAL'),('band_floor','INTEGER'),
                      ('players','INTEGER'),('scores','INTEGER'),('charts','INTEGER'))
CALIBRATION_DICTIONARY = (b'{\"maximum_statistics\":{\"great\":,\"large_tick_hit\":,\"small_tick_hit\":,\"miss\":,\"large_tick_miss\":,\"small_tick_miss\":},\"statistics\":{},\"mods\":[{\"acronym\":\"CL\",\"settings\":{}}]}' b'\"HD\"\"HR\"\"EZ\"\"DT\"\"HT\"\"NF\"\"NC\"\"FL\"\"SD\"\"PF\"\"speed_change\"')
HASH = re.compile(r'[0-9a-f]{64}\Z')
MD5 = re.compile(r'[0-9a-f]{32}\Z')
CAPABILITIES = {
    'mods': ['NM'], 'clock_rates': [1.0], 'styles': ['variety'], 'companions': [],
    'unavailable': {'mods': 'no modified Taiko calculations are qualified',
                    'styles': 'no subjective or experimental style labels are qualified'},
}
ANALYSIS = {
    'calculator': CALCULATOR, 'official_commit': OFFICIAL_COMMIT,
    'official_difficulty_version': DIFFICULTY_VERSION, 'engine': ENGINE,
    'source': 'completed verified Taiko selection and calculation receipts',
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('utf-8') + b'\n'


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _bounded_text(value, name, maximum=4096, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not str or '\x00' in value or len(value.encode('utf-8')) > maximum:
        raise ConfigError(f'invalid Taiko {name}')


def _research_receipts(root):
    """Verify the completed final pipeline receipt chain without mutating research."""
    root = Path(root)
    status_path = root / 'reports/pipeline-status.json'
    status = json.loads(status_path.read_bytes())
    stages = status.get('stages')
    if (status.get('phase') != 'complete' or status.get('lobby_operations') is not False
            or status.get('production_publication') is not False or type(stages) is not dict
            or len(stages) != 12):
        raise ConfigError('Taiko research pipeline is incomplete or has operated production')
    checked = {}
    for name, stage in stages.items():
        if stage.get('phase') != 'complete' or stage.get('error') is not None:
            raise ConfigError('Taiko research stage is incomplete')
        rel = stage.get('receipt')
        expected = stage.get('receipt_sha256')
        if type(rel) is not str or not rel.startswith('reports/') or type(expected) is not str:
            raise ConfigError('Taiko research receipt identity is invalid')
        path = root / rel
        if path.is_symlink() or not path.is_file() or _sha(path) != expected:
            raise ConfigError('Taiko research receipt checksum mismatch')
        receipt = json.loads(path.read_bytes())
        if receipt.get('name') != name or receipt.get('format') != 1:
            raise ConfigError('Taiko research receipt does not match pipeline stage')
        for output in receipt.get('outputs', []):
            out = root / output['path']
            if (not out.is_file() or out.is_symlink() or _sha(out) != output['sha256']):
                raise ConfigError('Taiko research output checksum mismatch')
        checked[name] = receipt
    selection = checked.get('selection_research', {}).get('result', {})
    if (selection.get('calculated_maps') != 39345
            or selection.get('calculation_verification', {}).get('verified_complete_results') != 39345):
        raise ConfigError('Taiko selection receipt does not verify the full calculation corpus')
    validation = checked.get('validation', {}).get('result', {})
    verified = validation.get('calculation_results', {}).get('verified_complete_results')
    if (verified != 39345 or validation.get('bulk_modern_payloads', {}).get('all_modern_payloads_verified') is not True
            or validation.get('panel_completed_players') != 9000
            or validation.get('panel_score_pages') != 16647
            or validation.get('panel_stopping', {}).get('census') is not False):
        raise ConfigError('Taiko full-data validation receipt is incomplete')
    return checked


def _record_map(records, kind, key):
    return records.get((kind, key))


def _record_indexes(db):
    """Scan source metadata once; avoid repeated full scans of the private 6.7 GiB DB."""
    result = {}
    cursor = db.execute("SELECT kind,natural_key,payload FROM records WHERE kind IN ('map','map_version','set')")
    while True:
        batch = cursor.fetchmany(512)
        if not batch:
            break
        for kind, key, raw in batch:
            payload = json.loads(raw)
            if kind == 'map':
                payload = {'file_sha256': payload.get('file_sha256')}
            elif kind == 'map_version':
                payload = {field: payload.get(field) for field in ('file_sha256','local_md5','native_nm_variant')}
            elif kind == 'set':
                payload = {'covers': payload.get('covers')}
            result[(kind,key)] = payload
    return result


def _value(record, field, *, required=False):
    node = record.get(field) if isinstance(record, dict) else None
    value = node.get('value') if isinstance(node, dict) else None
    if value is None and required:
        raise ConfigError(f'research map is missing required {field}')
    return value


def _projection(research_row, map_record, version_record, result, covers, source_identity=None):
    """Project only selection fields from checksum-matched, qualified inputs."""
    meta = research_row.get('metadata')
    if not isinstance(meta, dict):
        raise ConfigError('Taiko research map metadata is invalid')
    map_id = meta.get('map_id')
    original_mode = research_row.get('original_mode')
    status = research_row.get('status')
    is_convert = original_mode == 0
    if (type(map_id) is not int or map_id <= 0 or original_mode not in (0, 1)
            or status not in ('ranked', 'approved') or meta.get('status') != status
            or meta.get('original_mode') != original_mode):
        raise ConfigError('unqualified Taiko status/ruleset identity')
    sha = research_row.get('sha256')
    if not isinstance(sha, str) or not HASH.fullmatch(sha):
        raise ConfigError('invalid Taiko source SHA-256')
    if (result.get('phase') != 'complete' or result.get('map_id') != map_id
            or result.get('sha256') != sha or result.get('original_mode') != original_mode
            or result.get('status') != status or result.get('ruleset') != RULESET
            or result.get('converted') is not is_convert or result.get('mods') != []
            or result.get('clock_rate') != 1.0 or result.get('calculator') != CALCULATOR
            or result.get('difficulty_version') != DIFFICULTY_VERSION
            or result.get('source_commit') != OFFICIAL_COMMIT
            or result.get('engine') != ENGINE or not result.get('source_original_preserved')):
        raise ConfigError('Taiko result does not match exact source/difficulty qualification')
    qualification = result.get('qualification')
    required_qualification = {
        'judgements_from_official_hit_objects': True,
        'pp_wrapper_matches_direct_difficulty': True,
        'pp_wrapper_matches_direct_performance': True,
        'public_difficulty_matches_skill_replay': True,
    }
    if qualification != required_qualification:
        raise ConfigError('Taiko official calculation qualification is incomplete')
    attrs = result.get('official', {}).get('attributes', {})
    stars = attrs.get('StarRating')
    pp = result.get('official', {}).get('pp_scenarios', [{}])[0].get('pp')
    if (type(stars) not in (int, float) or not math.isfinite(stars) or not 0 < stars <= 100
            or type(pp) not in (int, float) or not math.isfinite(pp) or pp < 0):
        raise ConfigError('Taiko official NM stars/PP are unavailable')
    source_identity = source_identity or {}
    if not is_convert:
        if (not isinstance(map_record, dict) or map_record.get('file_sha256', {}).get('value') != sha):
            raise ConfigError('Taiko source metadata hash differs from calculated file')
        if (not isinstance(version_record, dict) or version_record.get('file_sha256', {}).get('value') != sha):
            raise ConfigError('Taiko map-version hash differs from calculated file')
    md5 = source_identity.get('source_md5') or (version_record or {}).get('local_md5', {}).get('value')
    variant = source_identity.get('taiko_nm_variant') or (version_record or {}).get('native_nm_variant', {}).get('value')
    if not isinstance(md5, str) or not MD5.fullmatch(md5):
        raise ConfigError('Taiko source MD5 is unavailable')
    if not isinstance(variant, str) or not HASH.fullmatch(variant):
        raise ConfigError('Taiko NM variant identity is unavailable')
    descriptive = research_row.get('descriptive', {})
    duration = descriptive.get('duration_seconds')
    hits = descriptive.get('hit_count')
    big = descriptive.get('big_hits')
    centre = descriptive.get('centre_hits')
    rim = descriptive.get('rim_hits')
    peak = descriptive.get('peak_hits_per_second')
    mean = descriptive.get('mean_hits_per_second')
    colour = descriptive.get('colour_change_fraction')
    mono = descriptive.get('maximum_mono_streak')
    if (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0
            or any(type(v) is not int or v < 0 for v in (hits, big, centre, rim, mono))
            or type(peak) not in (int, float) or not math.isfinite(peak) or peak < 0
            or type(mean) not in (int, float) or not math.isfinite(mean) or mean < 0
            or type(colour) not in (int, float) or not math.isfinite(colour) or not 0 <= colour <= 1):
        raise ConfigError('Taiko objective chart features are incomplete')
    artist, title, version = meta.get('artist'), meta.get('title'), meta.get('difficulty_name')
    for value, name in ((artist, 'artist'), (title, 'title'), (version, 'difficulty name')):
        _bounded_text(value, name)
    set_id = meta.get('set_id')
    if type(set_id) is not int or set_id <= 0:
        raise ConfigError('Taiko beatmapset identity is unavailable')
    bg = covers.get('cover') if isinstance(covers, dict) else None
    _bounded_text(bg, 'background URL', maximum=2048, nullable=True)
    timing = result.get('timing', {})
    bpm_min, bpm_max = timing.get('finite_bpm_min'), timing.get('finite_bpm_max')
    for value in (bpm_min, bpm_max):
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
            raise ConfigError('invalid Taiko timing metadata')
    return (
        map_id, set_id, artist, title, version, status, original_mode, int(is_convert), md5,
        sha, variant, float(stars), int(math.ceil(duration)), float(pp), CALCULATOR,
        DIFFICULTY_VERSION, OFFICIAL_COMMIT, bg, bpm_min, bpm_max, hits, big, centre,
        rim, float(peak), float(mean), float(colour), mono,
    )


def _decode_panel_scores(blob):
    if blob.startswith(b'C3J1'):
        decoder = zlib.decompressobj(zdict=CALIBRATION_DICTIONARY)
        raw = decoder.decompress(blob[4:], 4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024 or not decoder.eof or decoder.unused_data:
            raise ConfigError('Taiko panel score payload is invalid')
        return json.loads(raw)
    if blob.startswith(b'\x1f\x8b'):
        return json.loads(gzip.decompress(blob))
    raise ConfigError('Taiko panel score codec is unsupported')


def _monotonic_medians(points):
    """Equal-band weighted PAVA: make target SR nondecreasing with profile PP."""
    blocks = []
    for point in points:
        weight = max(1, point['scores'])
        blocks.append({'weight': weight, 'weighted': point['sr'] * weight, 'members': [point]})
        while len(blocks) > 1 and blocks[-2]['weighted'] / blocks[-2]['weight'] > blocks[-1]['weighted'] / blocks[-1]['weight']:
            right, left = blocks.pop(), blocks.pop()
            blocks.append({'weight': left['weight'] + right['weight'],
                           'weighted': left['weighted'] + right['weighted'],
                           'members': left['members'] + right['members']})
    adjusted = {}
    for block in blocks:
        value = block['weighted'] / block['weight']
        for point in block['members']:
            adjusted[point['pp']] = value
    for point in points:
        point['sr'] = round(adjusted[point['pp']], 4)
    return points


def derive_auto_calibration(panel_db, qualified_maps):
    """Aggregate NM top-play exposure by Taiko total-PP bands; no player rows escape."""
    panel_db = Path(panel_db).resolve()
    if panel_db.is_symlink() or not panel_db.is_file():
        raise ConfigError('Taiko profile panel database is unavailable')
    db = sqlite3.connect(panel_db.as_uri() + '?mode=ro', uri=True, timeout=5)
    db.execute('PRAGMA query_only=ON')
    if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
        db.close()
        raise ConfigError('Taiko profile panel database integrity failed')
    profiles = {uid: (float(pp), int(band)) for uid, pp, band, phase in db.execute(
        "SELECT uid,pp,band,phase FROM players WHERE phase='complete' AND pp>=0")
        if math.isfinite(float(pp))}
    # map id + current source MD5 prevent stale score aliases and converted variants.
    map_index = {int(m['beatmap_id']): (str(m['source_md5']), float(m['sr'])) for m in qualified_maps}
    aggregate = {}
    for uid, offset, blob, source_sha in db.execute(
            'SELECT uid,offset,payload,source_sha256 FROM score_pages ORDER BY uid,offset'):
        profile = profiles.get(uid)
        if profile is None:
            continue
        # source_sha256 is the provider-response receipt, not the locally encoded
        # C3J1 payload hash. The final validation receipt checks page lineage; the
        # bounded decoder below validates the retained payload stream itself.
        scores = _decode_panel_scores(blob)
        if type(scores) is not list:
            db.close()
            raise ConfigError('Taiko panel score page is not a list')
        pp, band = profile
        bucket = aggregate.setdefault(band, {'profile_pp_by_uid': {}, 'score_srs': [], 'players': set(), 'charts': set()})
        for score in scores:
            if (type(score) is not dict or score.get('ruleset_id') != 1 or score.get('passed') is not True
                    or score.get('mods') != []):
                continue
            bid = score.get('beatmap_id')
            match = map_index.get(bid)
            beatmap = score.get('beatmap')
            if (not match or type(beatmap) is not dict or beatmap.get('mode') != 'taiko'
                    or beatmap.get('checksum') != match[0]):
                continue
            bucket['players'].add(uid)
            bucket['profile_pp_by_uid'][uid] = pp
            bucket['score_srs'].append(match[1])
            bucket['charts'].add(bid)
    db.close()
    points = []
    for band, value in sorted(aggregate.items()):
        if len(value['players']) < 5 or len(value['score_srs']) < 20:
            continue
        points.append({'pp': round(float(statistics.median(value['profile_pp_by_uid'].values())), 2),
                       'sr': float(statistics.median(value['score_srs'])),
                       'band_floor': int(band), 'players': len(value['players']),
                       'scores': len(value['score_srs']), 'charts': len(value['charts'])})
    points.sort(key=lambda point: point['pp'])
    points = _monotonic_medians(points)
    total_profiles = len(profiles)
    covered_players = len({uid for value in aggregate.values() for uid in value['players']})
    return {
        'format': 1, 'kind': 'taiko_nm_topplay_exposure_heuristic',
        'qualification': 'ruleset-1 passed no-mod top scores joined to exact-source official Taiko NM chart stars',
        'convergence': 'research panel accepted at 9000 profiles; profile-panel report does not claim convergence',
        'not_a_claim': 'not an enjoyment, mastery, or skill guarantee; configured range remains fallback when unavailable',
        'profiles_available': total_profiles, 'profiles_with_qualified_nm_scores': covered_players,
        'coverage_fraction': round(covered_players / total_profiles, 6) if total_profiles else 0.0,
        'minimum_band_profiles': 5, 'minimum_band_scores': 20,
        'point_count': len(points), 'points': points,
    }


def _source_identity_indexes(root, receipts):
    """Bind native/conversion MD5 identity to the final receipt outputs."""
    root = Path(root)
    acquisition = receipts['acquisition']['outputs']
    native_output = next((item for item in acquisition if item['path'] == 'reports/native-coverage.json'), None)
    conversion_output = next((item for item in receipts['conversion_inputs']['outputs']
                              if item['path'] == 'reports/conversion-inputs.json'), None)
    if not native_output or not conversion_output:
        raise ConfigError('Taiko native/conversion source identity receipts are missing')
    native_path = root / native_output['path']
    conversion_path = root / conversion_output['path']
    native_doc = json.loads(native_path.read_bytes())
    conversion_doc = json.loads(conversion_path.read_bytes())
    if (native_doc.get('all_selected_files_accounted') is not True
            or native_doc.get('physical_file_coverage_complete') is not True
            or native_doc.get('original_bytes',{}).get('all_retained_native_files_verified') is not True
            or conversion_doc.get('phase') != 'complete' or conversion_doc.get('research_only') is not True):
        raise ConfigError('Taiko source identity coverage receipts are incomplete')
    native = {}
    for row in native_doc.get('maps', []):
        if (type(row.get('map_id')) is not int or row.get('file_phase') != 'verified_version_match'
                or row.get('reason') is not None):
            continue
        md5 = row.get('expected_md5')
        sha = row.get('file_sha256')
        if type(md5) is str and MD5.fullmatch(md5) and type(sha) is str and HASH.fullmatch(sha):
            native[(row['map_id'],sha)] = {'source_md5':md5,'status':row.get('status')}
    converted = {}
    for map_id, row in conversion_doc.get('candidates', {}).items():
        if (not isinstance(row,dict) or row.get('phase') != 'complete'
                or row.get('original_mode') != 0):
            continue
        meta_receipt, original_receipt = row.get('metadata_receipt',{}), row.get('original_receipt',{})
        sha, md5 = row.get('sha256'), row.get('provider_checksum')
        try: bid = int(map_id)
        except (TypeError,ValueError): continue
        metadata = row.get('metadata', {})
        status = metadata.get('status') if isinstance(metadata,dict) else None
        if (row.get('map_id') != bid or original_receipt.get('phase') != 'complete'
                or original_receipt.get('sha256') != sha or meta_receipt.get('phase') != 'complete'
                or meta_receipt.get('sha256') is None or type(sha) is not str or not HASH.fullmatch(sha)
                or type(md5) is not str or not MD5.fullmatch(md5)
                or metadata.get('id') != bid or metadata.get('mode_int') != 0
                or metadata.get('convert') is not False or metadata.get('checksum') != md5
                or status not in ('ranked','approved')):
            continue
        converted[(bid,sha)] = {'source_md5':md5, 'status':status,
            'taiko_nm_variant': hashlib.sha256(_canonical({'map_id':bid,'source_sha256':sha,
                'calculator':CALCULATOR,'difficulty_version':DIFFICULTY_VERSION,'source_commit':OFFICIAL_COMMIT,
                'ruleset':RULESET,'mods':[],'clock_rate':1.0})).hexdigest()}
    return native, converted, {
        'native_coverage': {'sha256':native_output['sha256'],'bytes':native_path.stat().st_size},
        'conversion_inputs': {'sha256':conversion_output['sha256'],'bytes':conversion_path.stat().st_size},
    }


def export(research, destination):
    """Build a production catalog from immutable completed research artifacts."""
    root = Path(research).resolve()
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ConfigError('Taiko export destination already exists')
    receipts = _research_receipts(root)
    native_identities, conversion_identities, identity_sources = _source_identity_indexes(root, receipts)
    analysis_path = root / 'state/analysis-maps.sqlite3'
    calc_path = root / 'state/calculation.sqlite3'
    research_path = root / 'state/research.sqlite3'
    panel_path = root / 'state/panel.sqlite3'
    selection_path = root / 'reports/selection-map-research.jsonl.gz'
    for path in (analysis_path, calc_path, research_path, panel_path, selection_path):
        if not path.is_file() or path.is_symlink():
            raise ConfigError('Taiko export input is missing or not a regular file')
    selection_receipt = next(x for x in receipts['selection_research']['outputs']
                             if x['path'] == 'reports/selection-map-research.jsonl.gz')
    selection_sha = selection_receipt['sha256']
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.taiko-export-', dir=destination.parent))
    stats = Counter()
    source_hashes = {p.name: _sha(p) for p in (analysis_path, calc_path, research_path, panel_path, selection_path)}
    try:
        analysis = sqlite3.connect(analysis_path.as_uri() + '?mode=ro', uri=True)
        calc = sqlite3.connect(calc_path.as_uri() + '?mode=ro', uri=True)
        records = sqlite3.connect(research_path.as_uri() + '?mode=ro', uri=True)
        for db in (analysis, calc, records):
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
        if analysis.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ConfigError('Taiko analysis maps database integrity failed')
        if calc.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ConfigError('Taiko calculation database integrity failed')
        if records.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ConfigError('Taiko research records database integrity failed')
        records_by_key = _record_indexes(records)
        jobs = {(r[2],r[1],r[3],r[4],r[5]): r for r in calc.execute(
            "SELECT id,map_id,sha256,original_mode,status,engine,phase,result_path,result_sha256 FROM jobs WHERE phase='complete'")}
        db_out = sqlite3.connect(stage / 'catalog.db')
        db_out.execute('PRAGMA page_size=4096')
        db_out.execute('PRAGMA journal_mode=DELETE')
        db_out.execute('PRAGMA application_id=1347572547')
        db_out.execute('PRAGMA user_version=1')
        columns_sql = ','.join(f'{name} {kind}' + (' PRIMARY KEY' if name == 'beatmap_id' else '')
                               for name, kind in MAP_COLUMNS)
        db_out.execute(f'CREATE TABLE taiko_maps ({columns_sql})')
        calibration_sql = ','.join(f'{name} {kind}' + (' PRIMARY KEY' if name == 'pp' else '')
                                   for name, kind in CALIBRATION_COLUMNS)
        db_out.execute(f'CREATE TABLE {CALIBRATION_TABLE} ({calibration_sql})')
        with gzip.open(selection_path, 'rt', encoding='utf-8') as src:
            for line in src:
                row = json.loads(line)
                map_id = row.get('map_id')
                if type(map_id) is not int or map_id <= 0:
                    stats['excluded_invalid_identity'] += 1
                    continue
                meta = row.get('metadata', {})
                original_mode, status = row.get('original_mode'), row.get('status')
                if original_mode not in (0, 1):
                    stats['excluded_unsupported_original_ruleset'] += 1
                    continue
                if status not in ('ranked', 'approved'):
                    stats['excluded_unranked_status'] += 1
                    continue
                if map_id in EXCLUDED:
                    stats['excluded_global_policy'] += 1
                    continue
                job = jobs.get((row.get('sha256'), map_id, original_mode, status, ENGINE))
                if not job:
                    stats['excluded_no_completed_exact_calculation'] += 1
                    continue
                job_id, calc_map, calc_sha, calc_mode, calc_status, engine, phase, result_rel, result_sha = job
                if (calc_map != map_id or calc_sha != row.get('sha256') or calc_mode != original_mode
                        or calc_status != status or engine != ENGINE or phase != 'complete'):
                    stats['excluded_calculation_identity_mismatch'] += 1
                    continue
                if (type(result_rel) is not str or not result_rel.startswith('calculation/results/')
                        or Path(result_rel).is_absolute() or '..' in Path(result_rel).parts):
                    stats['excluded_result_checksum_failure'] += 1
                    continue
                result_path = root / result_rel
                if not result_path.is_file() or result_path.is_symlink():
                    stats['excluded_result_checksum_failure'] += 1
                    continue
                result_blob = result_path.read_bytes()
                try:
                    result_raw = gzip.decompress(result_blob)
                except (OSError, EOFError, ValueError):
                    stats['excluded_result_checksum_failure'] += 1
                    continue
                if hashlib.sha256(result_raw).hexdigest() != result_sha:
                    stats['excluded_result_checksum_failure'] += 1
                    continue
                try:
                    result = json.loads(result_raw)
                except (ValueError, TypeError):
                    stats['excluded_result_checksum_failure'] += 1
                    continue
                del result_blob, result_raw
                # Reject charts without complete objective feature calculations, and
                # preserve explicit native vs converted identity on every row.
                key = f"taiko:{map_id}:{row['sha256']}"
                map_record = _record_map(records_by_key, 'map', key) if original_mode == 1 else None
                version_record = _record_map(records_by_key, 'map_version', key) if original_mode == 1 else None
                source_identity = (native_identities if original_mode == 1 else conversion_identities).get((map_id,row['sha256']))
                if (source_identity is None or source_identity.get('status') != status
                        or (original_mode == 1 and (map_record is None or version_record is None))):
                    stats['excluded_missing_source_provenance'] += 1
                    continue
                if (original_mode == 1 and version_record.get('local_md5',{}).get('value') != source_identity['source_md5']):
                    stats['excluded_source_md5_mismatch'] += 1
                    continue
                set_record = _record_map(records_by_key, 'set', f"provider:set:{meta.get('set_id')}")
                covers = _value(set_record, 'covers') if set_record else None
                try:
                    projected = _projection(row, map_record, version_record, result, covers, source_identity)
                except ConfigError as exc:
                    reason = str(exc)
                    # Explicitly retain only aggregate reason classes, never raw data.
                    label = 'excluded_qualification_failure'
                    if 'source MD5' in reason:
                        label = 'excluded_missing_source_md5'
                    elif 'source metadata hash' in reason or 'map-version hash' in reason:
                        label = 'excluded_source_hash_mismatch'
                    elif 'objective chart features' in reason:
                        label = 'excluded_missing_objective_features'
                    elif 'qualification' in reason or 'exact source/difficulty' in reason:
                        label = 'excluded_unqualified_calculation'
                    stats[label] += 1
                    continue
                if projected[0] != map_id or projected[7] != int(original_mode == 0):
                    stats['excluded_projection_identity_mismatch'] += 1
                    continue
                if stats['qualified_native' if not projected[7] else 'qualified_converted'] >= MAX_ROWS:
                    raise ConfigError('Taiko catalog row budget exceeded')
                try:
                    db_out.execute('INSERT INTO taiko_maps VALUES (' + ','.join('?' for _ in MAP_COLUMNS) + ')', projected)
                except sqlite3.IntegrityError as exc:
                    raise ConfigError('duplicate Taiko beatmap identity') from exc
                stats['qualified_converted' if projected[7] else 'qualified_native'] += 1
                stats['qualified_total'] += 1
        if not stats['qualified_total']:
            raise ConfigError('no qualified Taiko maps remain after receipt verification')
        digest = hashlib.sha256()
        for ordered_row in db_out.execute('SELECT * FROM taiko_maps ORDER BY beatmap_id'):
            digest.update(_canonical(dict(zip((n for n,_ in MAP_COLUMNS), ordered_row))))
        qualified_for_calibration = [dict(zip((n for n,_ in MAP_COLUMNS), row))
                                     for row in db_out.execute('SELECT * FROM taiko_maps')]
        calibration = derive_auto_calibration(panel_path, qualified_for_calibration)
        if calibration['profiles_available'] != receipts['validation']['result']['panel_completed_players']:
            raise ConfigError('Taiko profile panel row count differs from the validated receipt')
        for point in calibration['points']:
            db_out.execute(f'INSERT INTO {CALIBRATION_TABLE} VALUES (?,?,?,?,?,?)',
                           (point['pp'], point['sr'], point['band_floor'], point['players'], point['scores'], point['charts']))
        db_out.execute('CREATE INDEX taiko_selection_window ON taiko_maps(sr,hit_length,status)')
        db_out.execute('CREATE INDEX taiko_song_spacing ON taiko_maps(artist,title)')
        db_out.commit()
        db_out.close()
        analysis.close(); calc.close(); records.close()
        catalog_path = stage / 'catalog.db'
        file_item = {'name': 'catalog.db', 'role': 'catalog', 'sha256': _sha(catalog_path),
                     'bytes': catalog_path.stat().st_size,
                     'tables': {'taiko_maps': stats['qualified_total'],
                                'taiko_auto_calibration': len(calibration['points'])}}
        excluded = {key: value for key, value in sorted(stats.items()) if key.startswith('excluded_')}
        analysis = {
            **ANALYSIS,
            'qualification': {'native_ranked_approved': stats['qualified_native'],
                              'converted_ranked_approved': stats['qualified_converted'],
                              'official_taiko_nm_only': True, 'mods': [], 'clock_rate': 1.0},
            'auto_calibration': {k:v for k,v in calibration.items() if k != 'points'},
            'exclusion_counts': excluded,
            'catalog_rows_sha256': digest.hexdigest(),
        }
        files = [file_item]
        sources = {
            'selection_map_research': {'sha256': selection_sha, 'bytes': selection_path.stat().st_size},
            'analysis_maps': {'sha256': source_hashes['analysis-maps.sqlite3'], 'bytes': analysis_path.stat().st_size},
            'calculation_receipts': {'sha256': source_hashes['calculation.sqlite3'], 'bytes': calc_path.stat().st_size},
            'source_metadata': {'sha256': source_hashes['research.sqlite3'], 'bytes': research_path.stat().st_size},
            'profile_panel': {'sha256': source_hashes['panel.sqlite3'], 'bytes': panel_path.stat().st_size},
            **identity_sources,
        }
        archive = _pack(stage, files)
        manifest = {
            'format': 1, 'export_schema': 1, 'reader': READER, 'mode': MODE,
            'ruleset': RULESET, 'keys': None, 'files': files, 'sources': sources,
            'analysis': analysis, 'capabilities': json.loads(json.dumps(CAPABILITIES)),
            'exclusions': {'revision': 1, 'beatmap_ids': list(EXCLUDED)},
            'archive': archive,
        }
        manifest['content_id'] = hashlib.sha256(canonical(manifest)).hexdigest()
        validate_manifest(manifest)
        (stage / 'manifest.json').write_bytes(canonical(manifest))
        from .catalog_export import inspect_bundle
        inspect_bundle(stage)
        if any(_sha(path) != source_hashes[path.name] for path in (analysis_path, calc_path, research_path, panel_path, selection_path)):
            raise ConfigError('Taiko research source changed during export')
        os.rename(stage, destination)
        return manifest
    finally:
        for dbname in ('analysis', 'calc', 'records'):
            db = locals().get(dbname)
            if isinstance(db, sqlite3.Connection):
                try: db.close()
                except sqlite3.Error: pass
        if stage.exists():
            shutil.rmtree(stage)


def validate_manifest(value):
    """Validate the shared catalog manifest envelope and Taiko-specific analysis."""
    _closed(value, {'format','export_schema','reader','mode','ruleset','keys','files','sources',
                    'analysis','capabilities','exclusions','archive','content_id'})
    if (type(value['format']) is not int or value['format'] != 1
            or type(value['export_schema']) is not int or value['export_schema'] != 1
            or value['reader'] != READER or value['mode'] != MODE
            or type(value['ruleset']) is not int or value['ruleset'] != RULESET
            or value['keys'] is not None or value['capabilities'] != CAPABILITIES):
        raise ConfigError('unsupported Taiko production catalog contract')
    if type(value['files']) is not list or len(value['files']) != 1:
        raise ConfigError('Taiko catalog companion list is invalid')
    item = value['files'][0]
    _closed(item, {'name','role','sha256','bytes','tables'})
    if (item['name'] != 'catalog.db' or item['role'] != 'catalog'
            or item['tables'] != {'taiko_maps': value['analysis']['qualification']['native_ranked_approved'] + value['analysis']['qualification']['converted_ranked_approved'],
                                  'taiko_auto_calibration': value['analysis']['auto_calibration']['point_count']}):
        raise ConfigError('Taiko catalog file summary is invalid')
    _hash(item['sha256']); _size(item['bytes'])
    if type(value['sources']) is not dict or set(value['sources']) != {
            'selection_map_research','analysis_maps','calculation_receipts','source_metadata','profile_panel','native_coverage','conversion_inputs'}:
        raise ConfigError('Taiko source manifest fields mismatch')
    for source in value['sources'].values():
        _closed(source, {'sha256','bytes'}); _hash(source['sha256']); _size(source['bytes'], maximum=64 * 1024**3)
    analysis_keys = set(ANALYSIS) | {'qualification','auto_calibration','exclusion_counts','catalog_rows_sha256'}
    _closed(value['analysis'], analysis_keys)
    if any(value['analysis'].get(k) != v for k,v in ANALYSIS.items()):
        raise ConfigError('Taiko calculator provenance is unsupported')
    qualification = value['analysis']['qualification']
    _closed(qualification, {'native_ranked_approved','converted_ranked_approved','official_taiko_nm_only','mods','clock_rate'})
    if (type(qualification['native_ranked_approved']) is not int or qualification['native_ranked_approved'] < 0
            or type(qualification['converted_ranked_approved']) is not int or qualification['converted_ranked_approved'] < 0
            or qualification['official_taiko_nm_only'] is not True or qualification['mods'] != []
            or qualification['clock_rate'] != 1.0):
        raise ConfigError('Taiko qualification summary is invalid')
    ac=value['analysis']['auto_calibration']
    _closed(ac, {'format','kind','qualification','convergence','not_a_claim','profiles_available',
                 'profiles_with_qualified_nm_scores','coverage_fraction','minimum_band_profiles',
                 'minimum_band_scores','point_count'})
    if (ac['format'] != 1 or ac['kind'] != 'taiko_nm_topplay_exposure_heuristic'
            or type(ac['point_count']) is not int or ac['point_count'] < 0
            or type(ac['profiles_available']) is not int or ac['profiles_available'] < 0
            or type(ac['profiles_with_qualified_nm_scores']) is not int
            or ac['profiles_with_qualified_nm_scores'] < 0
            or type(ac['coverage_fraction']) not in (int,float) or not 0 <= ac['coverage_fraction'] <= 1):
        raise ConfigError('Taiko auto calibration summary is invalid')
    counts=value['analysis']['exclusion_counts']
    if (type(counts) is not dict or any(not isinstance(k,str) or not k.startswith('excluded_')
            or type(v) is not int or v < 0 for k,v in counts.items())):
        raise ConfigError('Taiko exclusion receipt is invalid')
    _hash(value['analysis']['catalog_rows_sha256'])
    if value['exclusions'] != {'revision':1,'beatmap_ids':list(EXCLUDED)}:
        raise ConfigError('initial catalog exclusions must be preserved')
    archive=value['archive']
    _closed(archive, {'name','sha256','bytes','expanded_bytes','codec'})
    if archive['name'] != 'bundle.tar.gz' or archive['codec'] != 'tar+gzip-v1':
        raise ConfigError('invalid Taiko catalog archive')
    _hash(archive['sha256']); _size(archive['bytes']); _size(archive['expanded_bytes'])
    if archive['expanded_bytes'] != item['bytes']:
        raise ConfigError('Taiko archive expanded size mismatch')
    _hash(value['content_id'])
    if hashlib.sha256(canonical({k:v for k,v in value.items() if k != 'content_id'})).hexdigest() != value['content_id']:
        raise ConfigError('Taiko content identity mismatch')
    return value


def read_rows(db_path):
    """Read the allowlisted, qualified map projection from a settled catalog DB."""
    path = Path(db_path).resolve()
    if not path.is_file() or path.is_symlink():
        raise ConfigError('Taiko catalog database is unavailable')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        schema = tuple((r['name'], r['type'].upper()) for r in db.execute('PRAGMA table_info(taiko_maps)'))
        if schema != MAP_COLUMNS:
            raise ConfigError('Taiko catalog schema mismatch')
        result = []
        for row in db.execute('SELECT * FROM taiko_maps ORDER BY beatmap_id'):
            item = dict(row)
            if (item['beatmap_id'] in EXCLUDED or item['status'] not in ('ranked','approved') or item['original_mode'] not in (0,1)
                    or item['is_convert'] != int(item['original_mode'] == 0)
                    or item['calculator_id'] != CALCULATOR or item['difficulty_version'] != DIFFICULTY_VERSION
                    or item['source_commit'] != OFFICIAL_COMMIT or item['sr'] <= 0 or item['hit_length'] <= 0
                    or not MD5.fullmatch(item['source_md5']) or not HASH.fullmatch(item['source_sha256'])
                    or not HASH.fullmatch(item['taiko_nm_variant'])):
                raise ConfigError('Taiko catalog contains an unqualified row')
            result.append(item)
        return tuple(result)
    except sqlite3.Error as exc:
        raise ConfigError('Taiko catalog database is invalid') from exc
    finally:
        db.close()


def inspect(folder, manifest):
    validate_manifest(manifest)
    path = Path(folder) / 'catalog.db'
    if _sha(path) != manifest['files'][0]['sha256']:
        raise ConfigError('Taiko catalog file checksum mismatch')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ConfigError('Taiko catalog database integrity failed')
        objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        if objects != [('index','taiko_selection_window'),('index','taiko_song_spacing'),('table','taiko_auto_calibration'),('table','taiko_maps')]:
            raise ConfigError('Taiko shipping database object allowlist mismatch')
        actual = tuple((row[1], row[2].upper()) for row in db.execute('PRAGMA table_info(taiko_maps)'))
        calibration_columns = tuple((row[1], row[2].upper()) for row in db.execute('PRAGMA table_info(taiko_auto_calibration)'))
        if (actual != MAP_COLUMNS or calibration_columns != CALIBRATION_COLUMNS
                or db.execute('SELECT COUNT(*) FROM taiko_maps').fetchone()[0] != manifest['files'][0]['tables']['taiko_maps']
                or db.execute('SELECT COUNT(*) FROM taiko_auto_calibration').fetchone()[0] != manifest['files'][0]['tables']['taiko_auto_calibration']):
            raise ConfigError('Taiko shipping schema or row count mismatch')
        digest = hashlib.sha256()
        for row in db.execute('SELECT * FROM taiko_maps ORDER BY beatmap_id'):
            item = dict(zip((name for name,_ in MAP_COLUMNS), row))
            if (item['beatmap_id'] in EXCLUDED or item['status'] not in ('ranked','approved') or item['original_mode'] not in (0,1)
                    or item['is_convert'] != int(item['original_mode'] == 0)
                    or item['calculator_id'] != CALCULATOR or item['difficulty_version'] != DIFFICULTY_VERSION
                    or item['source_commit'] != OFFICIAL_COMMIT or not HASH.fullmatch(item['source_sha256'])
                    or not MD5.fullmatch(item['source_md5']) or not HASH.fullmatch(item['taiko_nm_variant'])
                    or item['sr'] <= 0 or item['sr'] > 100 or item['hit_length'] <= 0):
                raise ConfigError('Taiko row failed production qualification inspection')
            digest.update(_canonical(item))
        if digest.hexdigest() != manifest['analysis']['catalog_rows_sha256']:
            raise ConfigError('Taiko projected rows checksum mismatch')
        points = db.execute('SELECT pp,sr,players,scores,charts FROM taiko_auto_calibration ORDER BY pp').fetchall()
        if (any(not all(math.isfinite(float(v)) for v in row[:2]) or row[0] < 0 or row[1] <= 0
                       or any(type(v) is not int or v < 0 for v in row[2:]) for row in points)
                or any(points[i][0] >= points[i+1][0] or points[i][1] > points[i+1][1]
                       for i in range(len(points)-1))):
            raise ConfigError('Taiko calibration points are invalid or nonmonotone')
    finally:
        db.close()
    return manifest
