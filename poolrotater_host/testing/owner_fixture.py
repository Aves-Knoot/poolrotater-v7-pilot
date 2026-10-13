"""Invented owner configuration for isolated installed-adoption qualification.

Pure construction only: no discovery, source files, credentials or provider calls.
Values exercise the publicly documented four-room owner feature surface without
embedding a real account, channel, player, token or production configuration.
"""
import copy


def source_config(*, integrations_enabled=False, overrides=None):
    from run_bot import DEFAULT_CONFIG
    defaults=copy.deepcopy(DEFAULT_CONFIG)
    defaults.update(default_response_language='en',mania_keys=4,room_count=4,
        mania_farm_enabled=False,moderation_bans_file='invented/bans.json',
        beatmap_exclusions_file='invented/exclusions.json')
    # Explicit synthetic paths remain private mapping inputs, never followed.
    defaults.update(mania_convert_db='')
    rooms={
        'room1':{'sr_min':4,'sr_max':5,'auto_sr_half_width':.35},
        'room2':{'sr_min':3.5,'sr_max':4.5,'auto_difficulty':False,'auto_sr_half_width':.30},
        'room3':{'ruleset_id':3,'mania_settings':{'mania_keys':7,'sr_min':3,'sr_max':4,
            'max_map_age_years':0,'min_passcount':1000,'pool_limit':400,
            'difficulty_vote_max_steps':10,'mania_farm_high_score_threshold':44}},
        'room4':{'ruleset_id':3,'mania_settings':{'mania_keys':4,'sr_min':3,'sr_max':4,
            'max_map_age_years':0,'min_passcount':1000,'pool_limit':400,
            'difficulty_vote_max_steps':10,'mania_farm_high_score_threshold':60.6}}}
    shared=dict(countdown_seconds=15,auto_start_seconds=10,queue_ahead=1,min_players=1,
        max_length=240,max_map_age_years=8,min_passcount=10000,pool_limit=1200,
        difficulty_vote_step=.5,difficulty_vote_max_steps=15,auto_empty_reset_seconds=45,
        hard_restart_interval_seconds=1800,requests_per_minute=50,requests_burst_capacity=60,
        freestyle=True,ai_enabled=False,discord_control_enabled=integrations_enabled,
        discord_modlog_enabled=integrations_enabled,chat_websocket_enabled=integrations_enabled,
        discord_public_status_channel_id=445566 if integrations_enabled else 0,
        discord_control_channel_id=445567,discord_control_guild_id=445568,
        discord_control_bot_token_env='INVENTED_DISCORD_BOT_TOKEN',
        discord_modlog_webhook_env='INVENTED_DISCORD_MODLOG_WEBHOOK',
        chat_websocket_endpoint='wss://notify.ppy.sh',admins=[112233],moderators=[223344],
        custom_join_greetings={'112233':'Synthetic greeting {name}'},
        map_pp_display_enabled=True,map_pp_auto_announce_enabled=True)
    if overrides:shared.update(copy.deepcopy(overrides))
    return {'defaults':defaults,'rooms':rooms,'universal':shared}


def effective(source):
    from poolrotater_host.owner_settings import effective_legacy
    return effective_legacy(source['defaults'],source['rooms'],source['universal'])


def review_mappings(source, root):
    """Private explicit path/identity pairs only; never waive gameplay/integrations."""
    from bot.mode import merge_room_config
    from run_bot import DEFAULT_CONFIG
    from poolrotater_host.owner_integrations import classify
    from poolrotater_host.runtime_config import render
    target={r['alias']:merge_room_config(DEFAULT_CONFIG,r['settings'],{}) for r in render(root)['rooms']}
    return {alias:{key:(value,target[alias][key]) for key,value in config.items()
        if key in target[alias] and (type(value) is not type(target[alias][key]) or value!=target[alias][key])
        and classify(key) in ('managed_mapping','host_identity')}
        for alias,config in effective(source).items()}


def policy_objects(source):
    """Complete required synthetic private policies/context and language/cache."""
    from poolrotater_host.owner_migration import _required_states
    result={}
    for target in _required_states(effective(source)):
        if target.endswith('bans.json'):
            result[target]={'schema':1,'bans':[{'user_id':998877,'expires_at':9999999999}],
                'audit':[{'action':'synthetic temporary ban','user_id':998877}]}
        elif 'exclusions' in target:result[target]={'schema':1,'exclusions':[{'beatmap_id':1549496}]}
        else:result[target]='Invented owner context preserved exactly\n'
    result['state/player-language-v1/112233.json']={'format':1,'language':'ja','offered':True}
    result['state/controller-map-pp/osu/101.osu']='osu file format v14\n[Metadata]\nTitle:Synthetic\n'
    result['state/controller-map-pp/results/101__NM__99p5.json']={
        'beatmap_id':101,'calculated_at':1.0,'backend':'invented-calculator',
        'mods':[],'values':[{'accuracy':99.5,'pp':220.0}]}
    return result
