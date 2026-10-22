"""Preset-qualified player settings, stricter than fleet administration."""
import copy
import math

from ..config import ConfigError, PRESETS
from ..managed_host import settings as native
from ..network.contracts import NetworkError, canonical, require
from bot.mode import MANIA_FREE_MODS

FIELDS=frozenset(('sr_min','sr_max','auto_difficulty','selection_styles','freestyle',
 'allowed_mods','required_mods','max_length','real_max_length','countdown_seconds',
 'auto_start_seconds','min_players','queue_ahead','voteskip_enabled','user_commands_enabled',
 'stream_share','preferred_min_length','preferred_max_length','preferred_length_share',
 'selection_filters','dt_mode'))
INTEGER_FIELDS=frozenset(('max_length','real_max_length','countdown_seconds',
                          'auto_start_seconds','min_players','queue_ahead'))
LIMITS={'max_length':(15,3600),'real_max_length':(15,3600),
 'countdown_seconds':(5,120),'auto_start_seconds':(5,120),
 'min_players':(1,16),'queue_ahead':(1,5),'stream_share':(0,1),
 'preferred_min_length':(0,3600),'preferred_max_length':(0,3600),
 'preferred_length_share':(0,1)}
FILTERS={'ar':(0,12),'length':(0,3600)}
STANDARD_MODS=frozenset(native.MODS-{'DT','NC','HT','DC','FI','MR'})
STANDARD_PRESETS=frozenset(('standard-auto','standard-fixed','standard-loved'))
CLOCK_MODS=frozenset(('DT','NC','HT','DC'))
REQUIRED_MOD_CONFLICTS=(frozenset(('NF','SD')),frozenset(('NF','PF')),
                        frozenset(('SD','PF')),frozenset(('EZ','HR')))


def _mod_list(value,allowed):
    return (type(value) is list and len(value)<=16 and
            all(type(item) is str and item in allowed for item in value) and
            len(set(value))==len(value))


def _fixed_fields(preset):
    if preset=='catch-fixed':
        return {'auto_difficulty':False,'allowed_mods':[],'required_mods':[],
                'freestyle':False}
    if preset.startswith('mania-') or preset=='standard-loved':
        return {'auto_difficulty':False}
    return {}


def _available_fields(preset):
    result=set(FIELDS)
    if preset in ('mania-4k','mania-7k','catch-fixed','standard-loved'):
        result.discard('auto_difficulty')
    if preset in ('mania-4k','mania-7k','catch-fixed','standard-loved'):
        result.discard('dt_mode')
    if preset in ('mania-4k','mania-7k','catch-fixed','standard-loved'):
        result.discard('stream_share')
    if preset=='catch-fixed':
        result.difference_update(('allowed_mods','required_mods','freestyle'))
    return result


def validate_filters(value,preset):
    require(type(value)is dict and not set(value)-set(FILTERS),'booking_invalid_filters')
    result={}
    for key,item in value.items():
        require(not(key=='ar'and (preset.startswith('mania-')or preset=='catch-fixed')),
                'booking_filter_unsupported')
        require(type(item)is dict and set(item)=={'min','max','mode'},'booking_invalid_filters')
        require(item['mode']in('hard','prefer'),'booking_invalid_filters')
        lo,hi=FILTERS[key]
        require(all(type(item[k])in(int,float)and math.isfinite(item[k])and lo<=item[k]<=hi
                    for k in('min','max'))and item['min']<=item['max'],'booking_invalid_filters')
        result[key]=dict(item)
    return result


def validate_player_patch(value,current=None,preset=None):
    require(type(value)is dict and not set(value)-FIELDS and len(canonical(value))<=16384,
            'booking_settings_refused')
    current=copy.deepcopy(current or {})
    preset=preset or current.get('preset','standard-fixed')
    require(preset in PRESETS,'booking_preset_unsupported')
    available=_available_fields(preset)
    fixed=_fixed_fields(preset)
    for key,wanted in fixed.items():
        require(current.get(key,wanted)==wanted and value.get(key,wanted)==wanted,
                'booking_setting_unsupported')
    require(not (set(value)-available-set(fixed)),'booking_setting_unsupported')

    result=copy.deepcopy(value)
    for key in INTEGER_FIELDS:
        if key in result:
            require(type(result[key]) is int,'booking_invalid_setting')
    for key,(lo,hi) in LIMITS.items():
        if key in result:
            v=result[key]
            require(type(v)in(int,float)and math.isfinite(v)and lo<=v<=hi,
                    'booking_invalid_setting')
    sr_limit=100 if preset=='catch-fixed' else 20
    for key in ('sr_min','sr_max'):
        if key in result:
            v=result[key]
            require(type(v)in(int,float)and math.isfinite(v)and 0<=v<=sr_limit,
                    'booking_invalid_setting')
    if 'dt_mode' in result:
        require(preset in ('standard-auto','standard-fixed') and type(result['dt_mode']) is str
                and result['dt_mode'] in ('auto','off','mixed','full'),'booking_dt_unsupported')
    if 'selection_filters' in result:
        result['selection_filters']=validate_filters(result['selection_filters'],preset)

    mod_options=(STANDARD_MODS if preset in STANDARD_PRESETS else
                 MANIA_FREE_MODS if preset.startswith('mania-') else frozenset())
    mod_settings={**current,**result}
    for key in ('allowed_mods','required_mods'):
        if key in result:
            require(_mod_list(result[key],mod_options),'booking_mod_unsupported')
        if key in mod_settings:
            require(_mod_list(mod_settings[key],mod_options),'booking_mod_unsupported')
    if preset=='standard-loved' or preset in ('standard-auto','standard-fixed'):
        allowed=set(mod_settings.get('allowed_mods',[]))
        required=set(mod_settings.get('required_mods',[]))
        require(not ((allowed|required)&CLOCK_MODS),'booking_dt_unsupported')
        required_upper={item.upper() for item in required}
        require(not any(pair<=required_upper for pair in REQUIRED_MOD_CONFLICTS),
                'booking_invalid_setting')
        if mod_settings.get('dt_mode') in ('mixed','full'):
            require(not (required_upper-{'NF'}),'booking_dt_mod_conflict')
    if 'freestyle' in result:
        require(type(result['freestyle']) is bool,'booking_invalid_setting')

    # Validate the merged native settings even when the incoming patch contains
    # only booking-only filters or DT mode. Cross-field native conflicts must
    # not depend on which field happened to be edited last.
    relevant={key:copy.deepcopy(item) for key,item in current.items()
              if key in native.FIELDS and key in FIELDS}
    native_patch={key:copy.deepcopy(item) for key,item in result.items()
                  if key in native.FIELDS}
    combined_native={**relevant,**native_patch,'preset':preset}
    try:
        native.validate(combined_native,current=relevant,preset=preset,allow_redacted=True)
    except (ConfigError,TypeError,ValueError):
        raise NetworkError('booking_settings_refused')from None
    combined={**relevant,**result}
    for low,high in(('sr_min','sr_max'),('preferred_min_length','preferred_max_length')):
        if low in combined and high in combined:
            require(combined[low]<combined[high]if low=='sr_min'else combined[low]<=combined[high],
                    'booking_invalid_setting')
    return result


def inventory(preset):
    require(preset in PRESETS,'booking_preset_unsupported')
    standard=preset in STANDARD_PRESETS
    fixed=_fixed_fields(preset)
    defaults={key:PRESETS[preset][key] for key in ('sr_min','sr_max','auto_difficulty')}
    defaults.update(selection_styles=['variety'],freestyle=False,allowed_mods=[],required_mods=[],
                    max_length=240,real_max_length=240,countdown_seconds=20,auto_start_seconds=15,
                    min_players=1,queue_ahead=1,voteskip_enabled=True,user_commands_enabled=True,
                    preferred_min_length=0,preferred_max_length=240,preferred_length_share=0.5,
                    selection_filters={})
    if preset in ('standard-auto','standard-fixed'):
        defaults.update(dt_mode='auto',stream_share=0.5)
    styles=(['variety','farm','stream','jump']if standard and preset!='standard-loved' else
            ['variety','jack','chordjack','stream','ln','technical','rice','noodle','farm']
            if preset.startswith('mania-')else ['variety'])
    from .selection import UNSUPPORTED_FILTERS
    unsupported=dict(UNSUPPORTED_FILTERS)
    if preset.startswith('mania-')or preset=='catch-fixed':
        unsupported['ar']='This catalog does not qualify AR filtering for this ruleset.'
    max_sr=100 if preset=='catch-fixed' else 20
    limits={key:list(bounds) for key,bounds in LIMITS.items()}
    limits.update(sr_min=[0,max_sr],sr_max=[0,max_sr])
    options={'selection_styles':styles}
    if standard:
        options.update(allowed_mods=sorted(STANDARD_MODS),required_mods=sorted(STANDARD_MODS))
        if preset in ('standard-auto','standard-fixed'):
            options['dt_mode']=['auto','off','mixed','full']
    elif preset.startswith('mania-'):
        options.update(allowed_mods=sorted(MANIA_FREE_MODS),required_mods=sorted(MANIA_FREE_MODS))
    return {'fields':sorted(_available_fields(preset)),'filters':{
                key:{'min':bounds[0],'max':bounds[1],'values':'effective','modes':['hard','prefer']}
                for key,bounds in FILTERS.items() if key not in unsupported},
            'defaults':defaults,'options':options,'unsupported_filters':unsupported,
            'limits':limits,'cooldown_seconds':10,
            'application':{key:'next_map' if key in native.NEXT_MAP or
                key in ('selection_filters','dt_mode') or key.startswith('preferred_') else 'live'
                for key in _available_fields(preset)}}
