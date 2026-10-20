"""Explicit, secret-free legacy gameplay projection for a stopped owner adoption.

No production paths are opened here. The administrator supplies reviewed objects
and performs the stopped-service/account-lease admission before writing RECORD.
Installation identity, topology and gameplay type/ranges are checked again on
every read; auth/transport/storage paths cannot enter this optional overlay.
"""
from __future__ import annotations

import copy
import math
import os

from .config import ConfigError, LANGUAGES, PRESETS, contained, fields
from .layout import read_json

RECORD = 'config/gameplay.json'
BOOLEANS = frozenset({
    'freestyle', 'stream_enabled', 'user_commands_enabled', 'voteskip_enabled',
    'adaptive_chat_polling', 'map_pp_display_enabled', 'map_pp_auto_announce_enabled',
    'mania_style_fallback_enabled', 'mania_high_star_relax_style', 'high_star_relax_enabled',
    'close_room_on_exit', 'lazer_coordinator_enabled', 'lazer_chat_patrol_enabled',
    'lazer_rejoin_on_hard_restart', 'loved_mode', 'mania_farm_enabled', 'catch_include_loved',
})
# Bounds are intentionally compatible with the currently used native settings;
# bool is never accepted as a number. No unreviewed string/path/identity fields.
NUMBERS = {
    'countdown_seconds': (0, 600), 'auto_start_seconds': (0, 600),
    'queue_ahead': (1, 20), 'min_players': (1, 16),
    'catch_min_length': (0, 3600), 'max_length': (0, 3600), 'real_max_length': (0, 3600),
    'min_passcount': (0, 10**9), 'pool_limit': (1, 200000), 'max_map_age_years': (0, 100),
    'preferred_length_share': (0, 1), 'preferred_min_length': (0, 3600),
    'preferred_max_length': (0, 3600), 'jump_preferred_length_share': (0, 1),
    'jump_preferred_min_length': (0, 3600), 'jump_preferred_max_length': (0, 3600),
    'jump_preferred_ar_min': (0, 12), 'jump_preferred_ar_max': (0, 12),
    'jump_preferred_ar_boost': (0, 10),
    'auto_sr_bias': (-10, 10), 'auto_sr_half_width': (.01, 10),
    'difficulty_vote_step': (.01, 10), 'difficulty_vote_ratio': (0, 1),
    'difficulty_vote_max_steps': (0, 100),
    'auto_empty_reset_seconds': (0, 86400), 'hard_restart_interval_seconds': (0, 604800),
    'hard_restart_warning_maps': (0, 100), 'hard_restart_close_delay_seconds': (0, 600),
    'score_delay_seconds': (0, 600), 'config_reload_seconds': (.1, 300),
    'chat_poll_seconds': (.1, 600), 'chat_poll_playing_seconds': (.1, 600),
    'chat_websocket_reconcile_seconds': (0, 86400), 'playlist_audit_seconds': (0, 600),
    'player_profile_cache_seconds': (0, 604800), 'download_availability_cache_days': (0, 3650),
    'rating_window_seconds': (0, 3600), 'info_interval_seconds': (0, 86400),
    'active_chat_countdown_extension_seconds': (0, 600),
    'active_chat_countdown_window_seconds': (0, 3600),
    'active_chat_countdown_min_messages': (1, 1000), 'active_chat_countdown_min_speakers': (1, 16),
    'start_wait_seconds': (0, 600), 'freedom_dive_cooldown_seconds': (0, 86400),
    'command_spam_burst': (1, 100), 'command_spam_window_seconds': (.1, 600),
    'command_spam_block_seconds': (0, 3600),
    'stream_share': (0, 1), 'stream_min_score': (0, 1), 'stream_min_bpm': (0, 1000),
    'stream_min_length': (0, 3600),
    'mania_min_length': (0, 3600), 'mania_style_min_score': (0, 1),
    'mania_min_rank_score': (0, 1), 'mania_min_confidence': (0, 1),
    'mania_min_appearances': (0, 10**9), 'mania_min_bands': (0, 1000),
    'mania_farm_bonus_max': (0, 10), 'mania_farm_bonus_min_confidence': (0, 1),
    'mania_farm_high_score_threshold': (0, 100), 'mania_farm_pick_multiplier': (0, 10),
    'performance_evidence_ratio': (0, 1), 'performance_evidence_min_top20': (0, 16),
    'performance_evidence_min_top30': (0, 16),
    'high_star_relax_min_maps': (0, 200000), 'high_star_relax_length_step': (0, 3600),
    'high_star_relax_age_step_years': (0, 100), 'high_star_relax_max_steps': (0, 100),
    'high_star_relax_threshold': (0, 20),
    'hub_recovery_seconds': (0, 3600), 'shutdown_timeout_seconds': (1, 600),
    'room_missing_confirmations': (1, 100), 'api_usage_log_seconds': (0, 86400),
    'requests_per_minute': (1, 60), 'requests_burst_capacity': (1, 90),
    'lazer_rejoin_delay_seconds': (0, 600), 'lazer_join_delay_seconds': (0, 600),
    'lazer_leave_confirm_delay_seconds': (0, 600), 'lazer_leave_delay_seconds': (0, 600),
    'lazer_minimum_room_dwell_seconds': (0, 600), 'lazer_visit_max_seconds': (0, 600),
    'lazer_visit_max_actions': (1, 100), 'lazer_chat_patrol_interval_seconds': (.01, 600),
    'lazer_switch_timeout_seconds': (0, 600), 'lazer_queue_poll_seconds': (.01, 600),
    'mania_convert_max_share': (0, 1),
}
INTEGERS = frozenset({
    'queue_ahead', 'min_players', 'pool_limit', 'min_passcount', 'difficulty_vote_max_steps',
    'hard_restart_warning_maps', 'active_chat_countdown_min_messages',
    'active_chat_countdown_min_speakers', 'command_spam_burst', 'mania_min_appearances',
    'mania_min_bands', 'performance_evidence_min_top20', 'performance_evidence_min_top30',
    'high_star_relax_min_maps', 'high_star_relax_max_steps',
    'room_missing_confirmations', 'requests_per_minute', 'requests_burst_capacity', 'lazer_visit_max_actions',
})
STYLES = frozenset({'variety','farm','jack','chordjack','stream','ln','technical','rice','noodle'})
MODS = frozenset({'NF','EZ','HD','HR','SD','DT','NC','HT','DC','FL','PF','FI','MR'})
EXTRA = frozenset({'command_prefix','room_name','allowed_mods','required_mods','mania_source_mode'})
GAMEPLAY_KEYS = BOOLEANS | set(NUMBERS) | EXTRA
BASE_EDITS = {'sr_min','sr_max','auto_difficulty','default_response_language'}


def validate_settings(value, *, preset):
    if not isinstance(value,dict) or set(value)-GAMEPLAY_KEYS:
        raise ConfigError('owner gameplay contains undeclared fields')
    if preset not in PRESETS:raise ConfigError('owner gameplay preset invalid')
    for key in ('lazer_coordinator_enabled','lazer_chat_patrol_enabled','lazer_rejoin_on_hard_restart','loved_mode'):
        if value.get(key):raise ConfigError('owner gameplay unsupported active legacy transport or catalog')
    for key,item in value.items():
        if key in BOOLEANS:
            if type(item) is not bool:raise ConfigError('owner gameplay boolean invalid')
        elif key in NUMBERS:
            low,high=NUMBERS[key]
            if (type(item) not in (int,float) or not math.isfinite(item) or not low<=item<=high
                    or (key in INTEGERS and type(item) is not int)):
                raise ConfigError('owner gameplay number invalid')
        elif key=='command_prefix':
            if type(item) is not str or not 1<=len(item)<=3 or any(c not in '.!?/' for c in item):
                raise ConfigError('owner gameplay command prefix invalid')
        elif key=='room_name':
            if type(item) is not str or not 1<=len(item)<=120 or any(ord(c)<32 or ord(c)==127 for c in item):
                raise ConfigError('owner gameplay room name invalid')
        elif key in ('allowed_mods','required_mods'):
            if (type(item) is not list or len(item)>16 or any(type(m) is not str or m not in MODS for m in item)
                    or len(item)!=len(set(item))):
                raise ConfigError('owner gameplay mods invalid')
        elif key=='mania_source_mode' and (type(item) is not str or item not in STYLES):
            raise ConfigError('owner gameplay mania style invalid')
    for low,high in [('preferred_min_length','preferred_max_length'),
                     ('jump_preferred_min_length','jump_preferred_max_length'),
                     ('jump_preferred_ar_min','jump_preferred_ar_max')]:
        if low in value and high in value and value[low]>value[high]:
            raise ConfigError('owner gameplay preference range invalid')
    if preset == 'catch-fixed':
        from bot.mode import validate_catch_config
        try: validate_catch_config(value)
        except ValueError as exc: raise ConfigError('owner gameplay Catch settings invalid') from exc
    elif preset.startswith('mania-'):
        from bot.mode import validate_mania_mods
        try:validate_mania_mods(value.get('allowed_mods',[]),value.get('required_mods',[]),value.get('freestyle',False))
        except ValueError as exc:raise ConfigError('owner gameplay mania mods invalid') from exc
        if value.get('stream_enabled',False):raise ConfigError('owner gameplay standard stream in mania')
    else:
        # Match the native validator without importing its SignalR transport.
        allowed=set(value.get('allowed_mods',[]));required=set(value.get('required_mods',[]))
        if allowed & {'DT','NC','HT','DC'} or allowed & required:
            raise ConfigError('owner gameplay standard mods invalid')
        if value.get('freestyle') and (value.get('allowed_mods') or value.get('required_mods')):
            raise ConfigError('owner gameplay freestyle conflicts with mods')
    return copy.deepcopy(value)


def project_legacy(defaults, room_configs, universal):
    """Pure projection of reviewed objects, using the existing precedence code.

    Output identifies integrations still requiring explicit owner-local setup;
    it never copies their credential names/paths/URLs/IDs into the overlay.
    """
    if not all(isinstance(x,dict) for x in (defaults,room_configs,universal)) or not 1<=len(room_configs)<=4:
        raise ConfigError('owner legacy projection objects invalid')
    rooms=[];overrides={};gaps=set()
    for alias,cfg in effective_legacy(defaults,room_configs,universal).items():
        import re
        if type(alias) is not str or not re.fullmatch('[a-z][a-z0-9_-]{0,23}',alias):
            raise ConfigError('owner legacy alias invalid')
        if cfg['ruleset_id']==2:
            raise ConfigError('Catch research requires explicit offline setup; legacy owner migration is not qualified')
        if cfg['ruleset_id']==3:preset='mania-'+str(cfg.get('mania_keys',4))+'k'
        else:preset='standard-auto' if cfg.get('auto_difficulty',True) else 'standard-fixed'
        spec=PRESETS[preset]
        low,high=cfg.get('sr_min',spec['sr_min']),cfg.get('sr_max',spec['sr_max'])
        if (type(low) not in (int,float) or type(high) not in (int,float)
                or not 0<=low<high<=20):raise ConfigError('owner legacy difficulty invalid')
        language=cfg.get('default_response_language','en')
        if type(language) is not str or language not in LANGUAGES:raise ConfigError('owner legacy language invalid')
        catalog='standard.db' if spec['ruleset']==0 else 'mania_'+str(spec['keys'])+'k.db'
        rooms.append({'alias':alias,'preset':preset,'sr_min':low,'sr_max':high,'language':language,
                      'catalog':'catalogs/'+catalog})
        overrides[alias]={'preset':preset,'settings':validate_settings(
            {k:v for k,v in cfg.items() if k in GAMEPLAY_KEYS},preset=preset)}
        for key,label in [('ai_enabled','ai'),('discord_control_enabled','discord_control'),
                          ('discord_modlog_enabled','discord_modlog'),('chat_websocket_enabled','chat_websocket')]:
            if cfg.get(key):gaps.add(label)
        if cfg.get('map_pp_display_enabled'):gaps.add('map_pp_native_runtime_qualification')
        for key in ('admins','moderators','custom_join_greetings'):
            if cfg.get(key):gaps.add('owner_local_'+key)
        if cfg.get('moderation_bans_file'):gaps.add('owner_local_moderation')
        if cfg.get('beatmap_exclusions_file'):gaps.add('signed_catalog_exclusions_review')
        if cfg.get('mania_convert_db'):gaps.add('mania_conversion_companion_not_distributed')
        if cfg.get('loved_mode'):gaps.add('loved_catalog_not_distributed')
    return {'rooms':rooms,'overrides':overrides,'integration_requirements':sorted(gaps),
            'credentials_copied':False,'production_mutated':False}


def effective_legacy(defaults,room_configs,universal):
    """Private effective config matching current headless supervisor precedence."""
    from bot.mode import merge_room_config
    if not all(isinstance(x,dict) for x in (defaults,room_configs,universal)) or not 1<=len(room_configs)<=4:
        raise ConfigError('owner legacy projection objects invalid')
    result={}
    for alias,raw in room_configs.items():
        try:cfg=merge_room_config(defaults,raw,universal)
        except (ValueError,TypeError,AttributeError) as exc:raise ConfigError('owner legacy room invalid') from exc
        # The nested block is source syntax, not a second effective setting.
        # Its validated leaves have already been applied by merge_room_config;
        # stopped adoption archives the exact raw source object separately.
        cfg.pop('mania_settings',None)
        cfg.pop('catch_settings',None)
        cfg.update(close_room_on_exit=True,lazer_coordinator_enabled=False,
                   lazer_chat_patrol_enabled=False,lazer_rejoin_on_hard_restart=False,
                   discord_control_accept_untargeted=False)
        result[alias]=cfg
    # The running shared transport uses primary pacing/process paths and aliases.
    from .owner_integrations import discord_id
    primary=next(iter(result.values()));aliases=list(result)
    for alias,cfg in result.items():
        for key in ('api_rate_db','process_control_db'):
            if key in primary:cfg[key]=primary[key]
        cfg['discord_control_room_alias']=alias
        cfg['discord_control_primary_instance']=aliases[0]
        cfg['discord_control_known_room_aliases']=sorted(aliases)
        cfg['discord_control_channel_ids_by_room']={a:c['discord_control_channel_id']
            for a,c in result.items() if discord_id(c.get('discord_control_channel_id',0))>0}
    return copy.deepcopy(result)


def make_record(marker, config, projection):
    if projection['rooms']!=config['rooms']:
        raise ConfigError('owner projection topology differs from host')
    value={'format':1,**{k:marker[k] for k in ('installation_id','generation')},
           'rooms':copy.deepcopy(projection['overrides'])}
    return validate_record(value,marker,config)


def validate_record(value, marker, config):
    fields(value,('format','installation_id','generation','rooms'),'owner gameplay record')
    if type(value['format']) is not int or value['format']!=1 or any(value[k]!=marker[k] for k in ('installation_id','generation')):
        raise ConfigError('owner gameplay identity invalid')
    expected={room['alias']:room for room in config['rooms']}
    if not isinstance(value['rooms'],dict) or set(value['rooms'])!=set(expected):
        raise ConfigError('owner gameplay topology invalid')
    for alias,room in value['rooms'].items():
        fields(room,('preset','settings'),'owner gameplay room')
        if room['preset']!=expected[alias]['preset']:raise ConfigError('owner gameplay preset changed')
        validate_settings(room['settings'],preset=room['preset'])
    return copy.deepcopy(value)


def load_overrides(root, marker, config):
    path=contained(root,RECORD)
    if not os.path.lexists(path):return {}
    return validate_record(read_json(path),marker,config)['rooms']
