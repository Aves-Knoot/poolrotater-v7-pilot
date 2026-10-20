"""Pure explicit Catch-room projection for a stopped administrative transition.

There is no automatic adoption hook, provider operation or credential discovery.
The administrator must separately hold the normal stopped/process/account guards
and preserve before/after files in a recoverable transition receipt.
"""
import copy
import re

from .config import ConfigError, validate
from . import owner_integrations as integration
from . import owner_settings as gameplay

OPERATIONAL = frozenset({
    'countdown_seconds','auto_start_seconds','min_players','queue_ahead','max_length',
    'rating_window_seconds','voteskip_enabled','user_commands_enabled',
    'difficulty_vote_step','difficulty_vote_max_steps','difficulty_vote_ratio',
    'command_prefix','map_repeat_cooldown_plays','song_repeat_cooldown_plays',
    'config_reload_seconds','close_room_on_exit','hard_restart_interval_seconds',
    'hard_restart_warning_maps','hard_restart_close_delay_seconds',
})


def project(marker, host, gameplay_record, integration_record, retained_room, *,
            alias='room1', sr_min=3., sr_max=4., title='PoolRotater | Catch'):
    """Add exactly one unused alias; existing room objects remain identical."""
    validate(host)
    old_aliases=[r['alias'] for r in host['rooms']]
    if (type(alias) is not str or not re.fullmatch(r'[a-z][a-z0-9_-]{0,23}',alias)
            or alias in old_aliases or len(old_aliases)>=4
            or any(r['preset']=='catch-fixed' for r in host['rooms'])):
        raise ConfigError('Catch pilot requires an unused alias and account slot')
    if type(retained_room) is not dict or any(retained_room.get(k) for k in integration.SECRET_KEYS):
        raise ConfigError('Catch integration input must contain configuration, not credentials')
    if type(title) is not str or not title or len(title)>160 or any(ord(c)<32 for c in title):
        raise ConfigError('Catch pilot title invalid')
    new_host=copy.deepcopy(host)
    new_host['rooms'].append({'alias':alias,'preset':'catch-fixed','sr_min':sr_min,'sr_max':sr_max,
                              'language':host['rooms'][0]['language'],'catalog':'catalogs/catch.db'})
    validate(new_host)
    identity={k:marker[k] for k in ('installation_id','generation')}
    if gameplay_record is not None:
        old_gameplay=gameplay.validate_record(gameplay_record,marker,host)
    else:
        old_gameplay={'format':1,**identity,'rooms':{
            r['alias']:{'preset':r['preset'],'settings':{}} for r in host['rooms']}}
    new_gameplay=copy.deepcopy(old_gameplay)
    from .gameplay_defaults import settings_for
    settings=settings_for('catch-fixed')
    template=old_gameplay['rooms'][old_aliases[0]]['settings']
    settings.update({k:copy.deepcopy(v) for k,v in template.items() if k in OPERATIONAL})
    settings.update(room_name=title,auto_difficulty=False,allowed_mods=[],required_mods=[],freestyle=False,
                    loved_mode=False,catch_include_loved=False,stream_enabled=False,mania_farm_enabled=False,
                    high_star_relax_enabled=False,min_passcount=0,max_map_age_years=0)
    settings={k:v for k,v in settings.items() if k in gameplay.GAMEPLAY_KEYS}
    new_gameplay['rooms'][alias]={'preset':'catch-fixed','settings':settings}
    gameplay.validate_record(new_gameplay,marker,new_host)
    if integration_record is None:
        new_integration=None
    else:
        integration.validate_record(integration_record,marker,host)
        new_integration=copy.deepcopy(integration_record)
        primary=old_aliases[0]
        row=copy.deepcopy(new_integration['rooms'][primary])
        # Shared account/Discord identity, roles, greetings and environment names
        # remain established. Only the unused room's retained channel is adopted.
        current=row['settings']; retained=retained_room
        if current.get('discord_control_enabled'):
            channel=retained.get('discord_control_channel_id')
            if not channel:raise ConfigError('Catch pilot has no retained Discord control channel')
            integration.discord_id(channel,allow_zero=False)
            current['discord_control_channel_id']=channel
        current['discord_control_room_alias']=alias
        if 'discord_public_status_label' in current:current['discord_public_status_label']='Lobby 1 · Catch' if alias=='room1' else 'Catch'
        if 'discord_public_status_channel_name' in current and retained.get('discord_public_status_channel_name'):
            current['discord_public_status_channel_name']=retained['discord_public_status_channel_name']
        # All optional state paths are host-local declarations, never copied files.
        row['paths']['ai_context_file']=False
        new_integration['rooms'][alias]=row
        aliases=sorted(old_aliases+[alias])
        for item in new_integration['rooms'].values():
            s=item['settings']
            if 'discord_control_known_room_aliases' in s:s['discord_control_known_room_aliases']=aliases
            if 'discord_control_channel_ids_by_room' in s and current.get('discord_control_channel_id'):
                s['discord_control_channel_ids_by_room'][alias]=current['discord_control_channel_id']
        integration.validate_record(new_integration,marker,new_host)
    return {'host':new_host,'gameplay':new_gameplay,'integrations':new_integration,
            'alias':alias,'provider_dispatch':False,'existing_rooms_unchanged':True,
            'requires_stopped_transaction':True}
