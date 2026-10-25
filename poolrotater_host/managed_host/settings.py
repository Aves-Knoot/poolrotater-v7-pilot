"""Complete typed room-facing settings; installation/credential paths are excluded."""
import copy
import math
import json
from ..config import PRESETS, LANGUAGES, ConfigError
from ..owner_settings import BOOLEANS as NATIVE_BOOLEANS, NUMBERS as NATIVE_NUMBERS, INTEGERS as NATIVE_INTEGERS, EXTRA
from ..owner_integrations import validate_settings as validate_integration

# Reviewable field inventory used by UI, command validation and application receipts.
# Native topology/identity/filesystem/provider/Discord-token settings are never edits.
INTEGER_LISTS = frozenset(('admins', 'moderators'))
BOOLEANS=NATIVE_BOOLEANS|{'bot_chat_silenced','sparse_passcount_enabled'}
NUMBERS={**NATIVE_NUMBERS,'dt_auto_threshold':(0,20),'dt_pick_share':(0,1),
         'sparse_passcount_min_maps':(1,200000),'sparse_passcount_floor':(0,10**9),
         'mania_ln_pick_multiplier':(0,10),'custom_join_greeting_cooldown_seconds':(0,86400)}
INTEGERS=NATIVE_INTEGERS|{'sparse_passcount_min_maps','sparse_passcount_floor'}
# Existing constructor-owned pool settings cannot be acknowledged as hotloaded
# merely because the JSON/cfg value changed. Rebuild only this target worker.
RECREATE = frozenset(('preset','visibility','password','mania_ln_pick_multiplier',
    'dt_auto_threshold','dt_pick_share','sparse_passcount_enabled','sparse_passcount_min_maps','sparse_passcount_floor',
    'catch_include_loved','catch_min_length','mania_source_mode','mania_min_length','mania_style_min_score',
    'mania_farm_enabled','mania_min_rank_score','mania_min_confidence','mania_min_appearances','mania_min_bands',
    'mania_farm_bonus_max','mania_farm_bonus_min_confidence','mania_convert_max_share','mania_style_fallback_enabled'))
NEXT_MAP = frozenset(('selection_style','selection_styles','allowed_mods','required_mods','freestyle',
    'sr_min','sr_max','auto_difficulty','stream_enabled','stream_share','mania_source_mode',
    'max_length','real_max_length','min_passcount','pool_limit','max_map_age_years','loved_mode',
    'catch_include_loved','mania_farm_enabled'))
DISALLOWED_GAMEPLAY = frozenset(('close_room_on_exit','requests_per_minute','requests_burst_capacity',
    'shutdown_timeout_seconds','config_reload_seconds','hub_recovery_seconds',
    'lazer_coordinator_enabled','lazer_chat_patrol_enabled','lazer_rejoin_on_hard_restart',
    'lazer_rejoin_delay_seconds','lazer_join_delay_seconds','lazer_leave_confirm_delay_seconds',
    'lazer_leave_delay_seconds','lazer_minimum_room_dwell_seconds','lazer_visit_max_seconds',
    'lazer_visit_max_actions','lazer_chat_patrol_interval_seconds','lazer_switch_timeout_seconds',
    'lazer_queue_poll_seconds'))
FIELDS = ((BOOLEANS | set(NUMBERS) | EXTRA) - DISALLOWED_GAMEPLAY) | {
    'sr_min','sr_max','auto_difficulty','default_response_language','selection_style',
    'admins','moderators','custom_join_greetings','custom_join_greeting_changes','preset','visibility','password','selection_styles'}
STYLES = frozenset(('variety','farm','stream','jump','jack','chordjack','ln','technical','rice','noodle'))
MODS = frozenset(('NF','EZ','HD','HR','SD','DT','NC','HT','DC','FL','PF','FI','MR'))

# Encoded settings have a stricter admission bound than native JSON readers.
# Four accepted rooms plus their runtime bases fit the bounded host documents.
SETTINGS_JSON_LIMIT=1024*1024
ROOM_JSON_LIMIT=4*1024*1024
HOST_JSON_LIMIT=32*1024*1024
PROJECTION_JSON_LIMIT=8*1024*1024


def inventory():
    return {key: {'application': 'recreate' if key in RECREATE else 'next_map' if
            key in NEXT_MAP or key.startswith(('preferred_','jump_','stream_','mania_','catch_','high_star_'))
            else 'live', 'private': key=='password'} for key in sorted(FIELDS)}

def _text(value, maximum):
    return type(value) is str and 0<len(value)<=maximum and not any(ord(c)<32 or ord(c)==127 for c in value)

def merge(current,patch):
    result={**copy.deepcopy(current or {}),**copy.deepcopy(patch)}
    if 'custom_join_greeting_changes' in patch:
        from bot.custom_greetings import DEFAULT_JOIN_GREETINGS
        greetings=copy.deepcopy((current or {}).get('custom_join_greetings',DEFAULT_JOIN_GREETINGS))
        changes=patch['custom_join_greeting_changes']
        if type(greetings) is not dict or type(changes) is not dict:raise ConfigError('managed_settings_invalid_greetings')
        for key,value in changes.items():
            if value is None:greetings.pop(key,None)
            else:greetings[key]=value
        validate_integration({'custom_join_greetings':greetings},('room1','room2','room3','room4'))
        result.pop('custom_join_greeting_changes',None);result['custom_join_greetings']=greetings
    if 'selection_style' in patch:result.pop('selection_styles',None)
    if 'selection_styles' in patch:result.pop('selection_style',None)
    if 'preset' in patch and patch['preset']!=(current or {}).get('preset'):
        mode=patch['preset']
        if type(mode) is not str or mode not in PRESETS:raise ConfigError('managed_settings_unsupported_preset')
        # These values describe the chosen mode rather than unrelated host tuning.
        result['loved_mode']=patch.get('loved_mode',mode=='standard-loved')
        result['auto_difficulty']=patch.get('auto_difficulty',PRESETS[mode]['auto_difficulty'])
        if mode.startswith('mania-') or mode in ('catch-fixed','standard-loved','taiko-fixed','taiko-auto'):
            result['stream_enabled']=patch.get('stream_enabled',False)
        if mode in ('catch-fixed','taiko-fixed','taiko-auto'):
            for key,value in dict(allowed_mods=[],required_mods=[],freestyle=False,
                                  mania_farm_enabled=False,high_star_relax_enabled=False,
                                  min_passcount=0,max_map_age_years=0).items():
                result[key]=copy.deepcopy(patch.get(key,value))
        if mode.startswith('taiko-'):
            result['auto_sr_bias']=patch.get('auto_sr_bias',0.)
            result['auto_sr_half_width']=patch.get('auto_sr_half_width',.5)
        if 'selection_style' not in patch:result.pop('selection_style',None)
        if 'selection_styles' not in patch:result.pop('selection_styles',None)
        result.pop('mania_source_mode',None)
    if patch.get('visibility')=='public' and 'password' not in patch:result.pop('password',None)
    return result

def validate(value, *, current=None, preset=None, allow_redacted=False):
    if type(value) is not dict or not value or set(value)-FIELDS:
        raise ConfigError('managed_settings_unknown_or_empty')
    if 'selection_style' in value and 'selection_styles' in value:
        raise ConfigError('managed_settings_conflicting_styles')
    if 'custom_join_greetings' in value and 'custom_join_greeting_changes' in value:
        raise ConfigError('managed_settings_conflicting_greetings')
    combined=merge(current,value)
    try:encoded=json.dumps(combined,ensure_ascii=True,allow_nan=False,separators=(',',':')).encode('utf-8')
    except (TypeError,ValueError):raise ConfigError('managed_settings_invalid_document') from None
    if len(encoded)>SETTINGS_JSON_LIMIT:raise ConfigError('managed_settings_document_too_large')
    mode=combined.get('preset',preset)
    if type(mode) is not str or mode not in PRESETS:raise ConfigError('managed_settings_unsupported_preset')
    if (mode.startswith('mania-') or mode=='standard-loved') and combined.get('auto_difficulty') is True:
        raise ConfigError('managed_settings_fixed_difficulty_required')
    if (mode.startswith('mania-') or mode in ('standard-loved','catch-fixed','taiko-fixed','taiko-auto')) and combined.get('stream_enabled') is True:
        raise ConfigError('managed_settings_unsupported_stream_pool')
    for key,item in value.items():
        if key in BOOLEANS or key=='auto_difficulty':
            good=type(item) is bool
        elif key in NUMBERS or key in ('sr_min','sr_max'):
            lo,hi=NUMBERS.get(key,(0,100 if mode=='catch-fixed' else 20))
            good=(type(item) in (int,float) and math.isfinite(item) and lo<=item<=hi
                  and (key not in INTEGERS or type(item) is int))
        elif key=='preset':good=item in PRESETS if type(item) is str else False
        elif key=='visibility':good=item in ('public','private') if type(item) is str else False
        elif key=='password':good=item is None or _text(item,128)
        elif key=='room_name':good=_text(item,100)
        elif key=='command_prefix':good=_text(item,3) and all(c in '.!?/' for c in item)
        elif key=='default_response_language':good=type(item) is str and item in LANGUAGES
        elif key in ('selection_style','mania_source_mode'):good=type(item) is str and item in STYLES
        elif key=='selection_styles':good=type(item) is list and len(item)<=8 and all(type(v) is str and v in STYLES for v in item) and len(set(item))==len(item)
        elif key in ('allowed_mods','required_mods'):
            good=(type(item) is list and len(item)<=16 and all(type(m) is str and m in MODS for m in item)
                  and len(set(item))==len(item))
        elif key in ('admins','moderators','custom_join_greetings'):
            try:validate_integration({key:item},('room1','room2','room3','room4'));good=True
            except (ConfigError,ValueError,TypeError):good=False
        elif key=='custom_join_greeting_changes':
            good=type(item) is dict and 0<len(item)<=50 and all(_text(k,80) and (v is None or
                type(v) is str and len(v)<=1000 and not any(ord(c)<32 or ord(c)==127 for c in v)) for k,v in item.items())
        else:good=False
        if not good:raise ConfigError('managed_settings_invalid_'+key)
    for low,high in [('sr_min','sr_max'),('preferred_min_length','preferred_max_length'),
                     ('jump_preferred_min_length','jump_preferred_max_length'),('jump_preferred_ar_min','jump_preferred_ar_max')]:
        if low in combined and high in combined and (combined[low]>combined[high] or low=='sr_min' and combined[low]==combined[high]):
            raise ConfigError('managed_settings_invalid_range')
    styles=combined.get('selection_styles',[combined.get('selection_style','variety')])
    valid_styles=STYLES-{'jump'} if mode.startswith('mania-') else {'variety'} if mode in ('standard-loved','catch-fixed','taiko-fixed','taiko-auto') else {'variety','farm','stream','jump'}
    if set(styles)-valid_styles or 'rice' in styles and 'noodle' in styles or 'variety' in styles and len(styles)>1:
        raise ConfigError('managed_settings_invalid_styles')
    if mode in ('taiko-fixed','taiko-auto'):
        from bot.mode import validate_taiko_config
        try:validate_taiko_config(combined)
        except ValueError:raise ConfigError('managed_settings_invalid_taiko') from None
    elif mode=='catch-fixed':
        if combined.get('min_passcount',0)!=0 or combined.get('max_map_age_years',0)!=0:
            raise ConfigError('managed_settings_invalid_catch_filters')
        from bot.mode import validate_catch_config
        try:validate_catch_config({k:v for k,v in combined.items() if k in FIELDS})
        except ValueError:raise ConfigError('managed_settings_invalid_catch') from None
    elif mode.startswith('mania-'):
        from bot.mode import validate_mania_mods
        try:validate_mania_mods(combined.get('allowed_mods',[]),combined.get('required_mods',[]),combined.get('freestyle',False))
        except ValueError:raise ConfigError('managed_settings_invalid_mania_mods') from None
    else:
        allowed=set(combined.get('allowed_mods',[]));required=set(combined.get('required_mods',[]))
        if allowed & {'DT','NC','HT','DC'} or allowed & required or len(required & {'DT','NC','HT','DC'})>1:
            raise ConfigError('managed_settings_invalid_mods')
        if combined.get('freestyle') and (allowed or required):raise ConfigError('managed_settings_freestyle_conflict')
    if combined.get('visibility','public')=='public' and combined.get('password'):
        raise ConfigError('managed_settings_password_requires_private')
    if combined.get('visibility')=='private' and not combined.get('password') and not allow_redacted:
        raise ConfigError('managed_settings_private_password_required')
    if 'loved_mode' in combined and combined['loved_mode'] is not (mode=='standard-loved'):
        raise ConfigError('managed_settings_loved_preset_conflict')
    return copy.deepcopy(value)

def application(patch):
    fields=inventory()
    return 'recreate' if any(fields[k]['application']=='recreate' for k in patch) else 'next_map' if any(
        fields[k]['application']=='next_map' for k in patch) else 'live'

def public_settings(settings):
    return {k:copy.deepcopy(v) for k,v in settings.items() if k!='password'} | {'password_configured':bool(settings.get('password'))}
