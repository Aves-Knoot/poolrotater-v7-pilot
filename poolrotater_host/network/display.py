"""Versioned, closed public display evidence. No player identities or local secrets."""
import math
from .contracts import fields, require, integer, number, text, room, versions, identifier

V1_ROOM_FIELDS = ('alias','osu_room_id','preset','state','occupants','visibility','title','min_stars','max_stars','observed_at','code_version','catalog_version','current_map')
CAPABILITIES = {'ok': True, 'protocol_version': 2, 'display_version': 1,
                'heartbeat_path': '/v2/hosts/self/heartbeat', 'directory_path': '/v2/rooms'}


def settings(value):
    fields(value, ('ruleset_id','keys','auto_difficulty','selection_style','allowed_mods','required_mods','freestyle','countdown_seconds'))
    integer(value['ruleset_id'],0,3)
    require(value['keys'] is None or type(value['keys']) is int and value['keys'] in (4,7),'invalid_display_keys')
    require(value['keys'] is None or value['ruleset_id']==3,'invalid_display_keys')
    for key in ('auto_difficulty','freestyle'):require(type(value[key]) is bool,'invalid_display_setting')
    text(value['selection_style'],80);number(value['countdown_seconds'],0,600)
    for key in ('allowed_mods','required_mods'):
        require(type(value[key]) is list and len(value[key])<=32,'invalid_display_mods')
        for mod in value[key]:text(mod,16);require(mod.isascii() and mod.isalnum() and mod==mod.upper(),'invalid_display_mods')
        require(len(set(value[key]))==len(value[key]),'invalid_display_mods')


def display(value, *, directory=False):
    if value is None:return
    fields(value,('settings','playing_players','match'))
    settings(value['settings'])
    if value['playing_players'] is not None:integer(value['playing_players'],0,128)
    match=value['match']
    if match is None:return
    fields(match,('playlist_item_id','beatmap_id','started_at','clock_rate','participants') + (('clock_skew_seconds',) if directory else ()))
    integer(match['playlist_item_id']);integer(match['beatmap_id']);number(match['started_at'],.001)
    if match['clock_rate'] is not None:number(match['clock_rate'],.25,3)
    if match['participants'] is not None:integer(match['participants'],0,128)
    if directory and match['clock_skew_seconds'] is not None:number(match['clock_skew_seconds'],-30,30)


def room_v2(value):
    fields(value,V1_ROOM_FIELDS+('display',));room({k:value[k] for k in V1_ROOM_FIELDS});display(value['display'])
    if value['display'] is not None:
        shown=value['display']['settings'];preset=value['preset']
        require(shown['ruleset_id']==(3 if preset.startswith('mania-') else 2 if preset=='catch-fixed' else 0),'display_preset_mismatch')
        require(shown['keys'] in (None, 4 if preset=='mania-4k' else 7) if preset.startswith('mania-') else shown['keys'] is None,'display_preset_mismatch')
        if preset=='catch-fixed':
            require(shown['auto_difficulty'] is False and shown['freestyle'] is False
                    and shown['selection_style']=='variety'
                    and all(mod=='NM' for key in ('allowed_mods','required_mods') for mod in shown[key]),
                    'unsupported_catch_display_variant')
    if value['display'] and value['display']['playing_players'] is not None:
        require(value['state']=='playing' and value['display']['playing_players']<=value['occupants'],'display_player_mismatch')
    if value['display'] and value['display']['match']:
        match=value['display']['match'];current=value['current_map']
        require(value['state']=='playing' and current is not None and match['beatmap_id']==current['beatmap_id'] and match['started_at']<=value['observed_at']+5,'display_match_mismatch')
    return value


def heartbeat_v2(value):
    fields(value,('protocol_version','installation_id','generation','sequence','code_version','catalog_version','rooms','sent_at'))
    require(type(value['protocol_version']) is int and value['protocol_version']==2,'unsupported_protocol',409)
    identifier(value['installation_id']);identifier(value['generation']);integer(value['sequence'],0);versions(value);number(value['sent_at'])
    require(type(value['rooms']) is list and len(value['rooms'])<=4,'room_limit')
    for row in value['rooms']:room_v2(row)
    require(len({r['alias'] for r in value['rooms']})==len(value['rooms']),'duplicate_room_alias')
    require(len({r['osu_room_id'] for r in value['rooms']})==len(value['rooms']),'duplicate_room_id')
    return value


def heartbeat_any(value):
    from .contracts import heartbeat
    return heartbeat_v2(value) if value.get('protocol_version')==2 else heartbeat(value)


def v1_observation(value):
    return {**value,'rooms':[{k:r[k] for k in V1_ROOM_FIELDS} for r in value['rooms']]}


def clock_rate(item):
    """Only explicit playlist mods/visibility prove a common match clock."""
    if type(item) is not dict or item.get('freestyle') is not False:return None
    if type(item.get('required_mods')) is not list or type(item.get('allowed_mods')) is not list:return None
    speed=None
    fixed = {'NF','HD','HR','EZ','FL','SO','RX','AP','SD','PF','MR','CL','FI','RD','CN','CO','TP','TD','AT','V2','SV2','NM','1K','2K','3K','4K','5K','6K','7K','8K','9K','10K'}
    for optional in item['allowed_mods']:
        acronym=optional.get('acronym') if type(optional) is dict else optional
        if type(acronym) is not str or acronym not in fixed:return None
    for mod in item['required_mods']:
        acronym=mod.get('acronym') if type(mod) is dict else mod
        if type(acronym) is not str:return None
        if acronym not in fixed | {'DT','NC','HT','DC'}:return None
        if acronym in ('DT','NC','HT','DC'):
            if speed is not None:return None
            opts=mod.get('settings',{}) if type(mod) is dict else {}
            if type(opts) is not dict or set(opts)-{'speed_change'}:return None
            speed=opts.get('speed_change',1.5 if acronym in ('DT','NC') else .75)
            if type(speed) not in (int,float) or not math.isfinite(speed) or not .25<=speed<=3:return None
    return speed if speed is not None else 1.0
