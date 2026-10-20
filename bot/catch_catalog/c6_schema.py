"""Closed, versioned research-catalog contract. No runtime or source DB imports."""
import hashlib
import json
import math
import re
from urllib.parse import urlsplit,parse_qsl

from .errors import IntegrityError

FORMAT = 'catch-research-catalog-2'
ROW_SCHEMA = 'catch-native-NM-2'
FEATURE_SCHEMA = 'catch-features-1'
MAX_ROWS = 200000
MAX_LINE = 256 * 1024
MAX_RAW = 256 * 1024**2
MAX_COMPRESSED = 64 * 1024**2
FILES = ('catalog.jsonl.gz', 'schema.json', 'source-rights.json')
COHORTS = ('random_10000', 'top_1000', 'top_10000', 'country_current_panel', 'country_pilot_subset')
PROFILES = ('burst_ratio', 'edge_dash_fraction', 'hyperdash_density', 'precision_inverse_width',
            'reversal_density', 'sustained_fraction', 'tiny_peak5s')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise IntegrityError('duplicate catalog JSON field')
            value[key] = item
        return value
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IntegrityError('invalid catalog JSON') from exc


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


# JSON-serializable contract notation: exact object fields, scalar leaves,
# nullable leaves, bounded homogeneous lists, and explicit enums.
def nullable(value): return ['nullable', value]
def array(value, maximum): return ['list', value, maximum]
def enum(*values): return ['enum', *values]
def distribution():
    return {'count': 'int', **{k: nullable('number') for k in ('min', 'max', 'mean', 'median', 'p90', 'p99')}}


PROFILE = {name: {'percentile': nullable('number'), 'reason': nullable('text'), 'reference_maps': 'int'} for name in PROFILES}
SELECTION = {
    'map_id': 'positive_int', 'set_id': 'positive_int', 'mapper_id': 'positive_int',
    'map_sha256': 'sha256', 'variant_id': 'sha256', 'calculator_id': 'sha256',
    'native': enum(True), 'mods': array('text', 0), 'clock_rate': enum(1.0),
    'verified': 'bool', 'scoreable': 'bool', 'deleted': 'bool',
    'status': enum('ranked', 'approved', 'loved'), 'stars': 'number', 'max_combo': 'int',
    'duration': 'number', 'provider_duration': nullable('number'), 'ranked_date': nullable('text'),
    'ranked_year': nullable('int'), 'title': 'text', 'artist': 'text', 'version': 'text', 'mapper': 'text',
    'song_key': 'text', 'ss_pp': 'number', 'raw_profile': {name: nullable('number') for name in PROFILES},
    'profile': PROFILE, 'spike_ratio': nullable('number'), 'AR': 'number', 'CS': 'number',
    'representation_weight': enum(1.0), 'style_weight': enum(1.0), 'feedback': enum(None),
}
DENSITY = {name: {'average_per_second': nullable('number'), 'peak_1s': 'number', 'peak_5s': 'number', 'peak_10s': 'number'}
           for name in ('Fruit', 'Droplet', 'TinyDroplet', 'combo', 'scoring')}
GEOMETRY = {name: 'int' for name in ('walk_feasible_pairs', 'dash_required_lower_bound_pairs',
    'edge_dash_lower_bound_pairs', 'official_hyperdash_pairs', 'longest_hyperdash_chain', 'reversals',
    'reversals_after_hyperdash', 'near_edge_objects', 'zero_time_pairs', 'zero_time_disjoint_catch_intervals')}
GEOMETRY.update(catch_width_px='number', **{name: distribution() for name in (
    'centre_distance_catcher_widths', 'minimum_required_speed_px_per_ms', 'combo_intervals_ms',
    'between_hyperdashes_ms', 'distance_to_hyperdash_catcher_widths')})
MOVEMENT = {
    'counts': {name: 'int' for name in ('Fruit', 'Droplet', 'TinyDroplet', 'Banana')},
    'duration_scoring_seconds': 'number', 'density': DENSITY, 'geometry': GEOMETRY,
    'strain': {'official_750ms_sections': distribution(), 'peak_to_median': nullable('number'),
        'fraction_sections_at_least_half_peak': nullable('number'), 'longest_half_peak_run_seconds': 'number',
        'late_quarter_peak_fraction': nullable('number'), 'late_peak_to_earlier_p90': nullable('number')},
    'rests': {'gaps_at_least_2s': 'int', 'sum_gaps_at_least_2s_seconds': 'number',
        'longest_combo_gap_seconds': nullable('number'), 'declared_breaks': 'int'},
    'sliders': {'juice_streams': 'int', 'repeated_streams': 'int', 'repeat_counts': distribution(),
        'tiny_droplet_share': nullable('number')},
    'timing': {'bpm': distribution(), 'sv_points': 'int', 'timing_points': 'int'},
    'transition_context': {'dash_reach_margin_catcher_widths': distribution(),
        'dash_reach_margin_interpretation': 'text', 'rapid_reversals_at_most_200ms': 'int',
        'reversals_after_long_movement': 'int', 'reversals_per_scoring_second': nullable('number')},
    'visibility': {'approach_rate': 'number', 'circle_size': 'number', 'catch_width_fraction': 'number',
        'nominal_preempt_ms': 'number', 'interpretation': 'text'},
    'missing_reasons': {name: 'text' for name in ('density_average', 'empty_distributions', 'movement', 'strain_ratios', 'style_quality')},
}
PP = {'name': enum('ss', 'fc_one_tiny_miss', 'one_fruit_miss_combo_upper_bound'),
      'statistics': {k: 'int' for k in ('great', 'large_tick_hit', 'small_tick_hit', 'small_tick_miss', 'miss')},
      'accuracy': 'number', 'max_combo': 'int', 'pp': 'number', 'formula_crosscheck_absolute_error': 'number',
      'scoring_origin': 'text', 'miss_location': nullable('text')}
EVIDENCE = {'cohort': enum(*COHORTS), 'unique_players': 'int', 'panel_players': 'int',
    'source_available': 'bool', 'supported': 'bool', 'index': 'number', 'reason': nullable('text'),
    'prevalence': nullable('number'), 'wilson95': array(nullable('number'), 2),
    'baseline_peer_maps': 'int', 'baseline_fallback_level': nullable('int'),
    'baseline_prevalence': nullable('number'), 'posterior_prevalence': nullable('number')}
ROW = {
    'schema': enum(ROW_SCHEMA), 'selection': SELECTION,
    'identity': {'ruleset_id': enum(2), 'original_ruleset': enum('fruits'), 'native': enum(True),
        'mods': array('text', 0), 'clock_rate': enum(1.0), 'file_md5': 'md5',
        'feature_schema': enum(FEATURE_SCHEMA), 'official_commit': 'commit',
        'official_difficulty_version': 'positive_int', 'official_strain_replay_verified': enum(True)},
    'presentation': {'artist_unicode': nullable('text'), 'title_unicode': nullable('text'),
        'set_creator_label': 'text', 'set_creator_id': 'positive_int', 'map_mapper_display_name': nullable('text'),
        'beatmap_url': 'url', 'covers': {k: nullable('url') for k in (
            'card', 'card@2x', 'cover', 'cover@2x', 'list', 'list@2x', 'slimcover', 'slimcover@2x')}},
    'durations': {'audio_seconds': nullable('number'), 'provider_total_seconds': nullable('number'),
        'provider_hit_seconds': nullable('number'), 'mandatory_scoring_seconds': 'number'},
    'provider_metadata': {'passcount': nullable('int'), 'playcount': nullable('int'),
        'set_favourite_count': nullable('int'), 'beatmap_bpm': nullable('number'), 'set_bpm': nullable('number'),
        'beatmap_last_updated': nullable('text'), 'set_last_updated': nullable('text'), 'set_ranked_at': nullable('text'),
        'download_disabled': nullable('bool'), 'lazer_only': nullable('bool'), 'counter_observed_at': enum(None),
        'counter_scope': enum('provider_lifetime_counters; aggregation_not_independently_audited'), 'source_snapshot': 'text'},
    'difficulty': {'dimensions': {k: 'number' for k in ('ApproachRate','CircleSize','OverallDifficulty',
        'DrainRate','SliderMultiplier','SliderTickRate')}, 'provider_stars': nullable('number'),
        'provider_max_combo': nullable('int'), 'pp_scenarios': array(PP, 3)},
    'movement': MOVEMENT, 'evidence': array(EVIDENCE, 5),
    'eligibility': {'ranked_approved_reason': nullable('text'), 'include_loved_reason': nullable('text'),
        'loved_opt_in': 'bool', 'extreme_loved_review': 'bool', 'style_quality_labels': enum(None),
        'quality_weight': enum(1.0)},
    'confidence': {'file': enum('accepted_current_native_NM'), 'score_time_file': enum('unknown'),
        'subjective_labels': enum('unreviewed'), 'representation': enum('descriptive_nonrandom_overlapping_panels')},
    'lineage': {'file_source': enum('ppy-bulk','osu-map-files'), 'file_sha256': 'sha256',
        'provider_record_id': 'sha256', 'provider_body_sha256': 'sha256', 'feature_payload_sha256': 'sha256',
        'snapshot': 'text', 'source_ids': array('text', 8)},
    # Validated separately: exact null paths, with a nonempty reason for each.
    'missing': 'missing_map',
}


def validate(value, spec, path='row'):
    def fail(): raise IntegrityError('catalog schema violation at ' + path)
    if isinstance(spec, dict):
        if not isinstance(value, dict) or set(value) != set(spec): fail()
        for key, child in spec.items(): validate(value[key], child, path + '/' + key)
    elif isinstance(spec, list):
        if spec[0] == 'nullable':
            if value is not None: validate(value, spec[1], path)
        elif spec[0] == 'list':
            if not isinstance(value, list) or len(value) > spec[2]: fail()
            for i, child in enumerate(value): validate(child, spec[1], path + '/' + str(i))
        elif spec[0] == 'enum':
            if not any(type(value) == type(v) and value == v for v in spec[1:]): fail()
        else: fail()
    elif spec in ('int', 'positive_int'):
        if type(value) is not int or value < (1 if spec == 'positive_int' else 0): fail()
    elif spec == 'number':
        if type(value) not in (int,float) or not math.isfinite(value): fail()
    elif spec == 'bool':
        if type(value) is not bool: fail()
    elif spec in ('sha256','md5','commit'):
        if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{'+str({'sha256':64,'md5':32,'commit':40}[spec])+'}',value): fail()
    elif spec in ('text','url'):
        if not isinstance(value,str) or len(value)>4096 or any(ord(x)<32 for x in value): fail()
        if spec == 'url':
            try:
                parsed=urlsplit(value)
                if (parsed.scheme!='https' or parsed.hostname not in ('assets.ppy.sh','osu.ppy.sh')
                        or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None,443)
                        or any(k.lower() in {'token','access_token','refresh_token','client_secret','code','key','authorization'}
                               for k,v in parse_qsl(parsed.query))): fail()
            except ValueError: fail()
    elif spec == 'missing_map':
        if not isinstance(value,dict) or any(not isinstance(k,str) or not isinstance(v,str) or not v for k,v in value.items()): fail()
    else: fail()


def null_paths(value, path=''):
    result=[]
    if isinstance(value,dict):
        for key,item in value.items():
            if path=='' and key=='missing': continue
            result.extend(null_paths(item,path+'/'+key))
    elif isinstance(value,list):
        for i,item in enumerate(value):result.extend(null_paths(item,path+'/'+str(i)))
    elif value is None: result.append(path)
    return result


def validate_row(row):
    validate(row,ROW)
    if set(row['missing']) != set(null_paths(row)):
        raise IntegrityError('catalog nulls need exact explicit reasons')
    s=row['selection'];i=row['identity'];d=row['durations'];p=row['difficulty']
    if (row['lineage']['file_sha256']!=s['map_sha256']
            or s['stars']<0 or s['duration']<0 or s['ss_pp']<0
            or s['duration']!=d['mandatory_scoring_seconds'] or s['provider_duration']!=d['provider_total_seconds']
            or s['AR']!=p['dimensions']['ApproachRate'] or s['CS']!=p['dimensions']['CircleSize']):
        raise IntegrityError('catalog selection/feature identity mismatch')
    expected_variant=sha(canonical({'map_sha256':s['map_sha256'],'native':True,'original_ruleset':'fruits',
        'ruleset':'fruits','mods':[],'clock_rate':1.0}))
    if expected_variant!=s['variant_id']:
        raise IntegrityError('catalog variant digest disagrees with native NM identity')
    if d['audio_seconds'] is not None:
        raise IntegrityError('audio was not measured in this qualified version')
    if row['provider_metadata']['source_snapshot']!=row['lineage']['snapshot']:
        raise IntegrityError('provider metadata snapshot mismatch')
    cases=p['pp_scenarios']
    if not cases or cases[0]['name']!='ss' or cases[0]['pp']!=s['ss_pp'] or len({x['name']for x in cases})!=len(cases):
        raise IntegrityError('explicit PP scenarios do not match selection projection')
    for case in cases:
        if not 0<=case['accuracy']<=1 or case['pp']<0 or case['formula_crosscheck_absolute_error']<0:
            raise IntegrityError('invalid explicit PP scenario')
    if {x['cohort'] for x in row['evidence']}!=set(COHORTS) or len(row['evidence'])!=5:
        raise IntegrityError('missing or repeated research cohort')
    for e in row['evidence']:
        if e['unique_players']>e['panel_players'] or len(e['wilson95'])!=2 or e['index']<=0:
            raise IntegrityError('invalid aggregate cohort counts')
    for values in s['profile'].values():
        if values['percentile'] is not None and not 0<=values['percentile']<=1:
            raise IntegrityError('invalid descriptive percentile')
    # The importer checks eligibility independently of the exporter/reference.
    def reason(include_loved):
        if not s['verified']: return 'unverified_file'
        if s['deleted'] or not s['scoreable']: return 'unavailable'
        if s['status']=='loved' and not include_loved: return 'status_policy'
        return 'no_mandatory_scoring_objects' if s['max_combo']<=0 or s['duration']<=0 else None
    loved=reason(True)
    ordinary=reason(False)
    e=row['eligibility']
    if (e['include_loved_reason']!=loved or e['ranked_approved_reason']!=ordinary
            or e['loved_opt_in']!=(s['status']=='loved') or e['extreme_loved_review']!=(s['status']=='loved' and s['stars']>=20)):
        raise IntegrityError('catalog exclusion policy mismatch')
    expected_sources={row['lineage']['file_source'],'osu-public-api','c4-official-osu','ppy-bulk','c51-country-api'}
    if set(row['lineage']['source_ids'])!=expected_sources or len(row['lineage']['source_ids'])!=len(expected_sources):
        raise IntegrityError('catalog source lineage incomplete')


def schema_document():
    return {'format':FORMAT,'row_schema':ROW_SCHEMA,'row':ROW,
        'units': {'stars':'osu!catch stars, native NM', 'pp':'hypothetical pinned lazer NM judgement case',
            'durations':'seconds; audio unknown, provider hit/total and mandatory-scoring spans distinct',
            'movement':'_ms milliseconds; _px pixels; _per_second rates/s; _widths catcher widths; percentile 0..1',
            'AR_CS_OD_HP':'official difficulty dimensions; not star or quality scores',
            'evidence':'distinct panel player/map presence; cohorts overlap; not exposure, enjoyment or clear probability'},
        'policies': {'research_only':True,'production_ready':False,'native_NM_only':True,
            'style_quality_weights':'neutral, unreviewed','historical_file_identity':'unknown',
            'map_mapper_attribution':'selection.mapper_id is public beatmap owner; selection.mapper/set_creator_label is set creator, not verified guest-mapper display name',
            'missing':'one nonempty reason for every null JSON pointer; zero and false are data',
            'distribution':'withheld pending source-rights qualification; no live integration'}}
