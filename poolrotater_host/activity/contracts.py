"""Closed participation wire schema; no chat, arbitrary metadata or PP attribution."""
from ..network.contracts import (fields, require, identifier, integer, number, text,
                                 canonical, base)
MAX_EVENTS = 100
MAX_BODY = 65536
MODES = ('standard', 'mania4', 'mania7', 'catch', 'taiko')


def consent(value):
    fields(value, ('protocol_version','enabled','consent_id','consent_at'))
    base(value); require(type(value['enabled']) is bool,'explicit_statistics_consent_required')
    identifier(value['consent_id']); number(value['consent_at'])
    return value


def event(value):
    fields(value, ('event_id','installation_id','generation','consent_id','kind','payload'))
    for name in ('event_id','installation_id','generation','consent_id'): identifier(value[name])
    kind = value['kind']; p = value['payload']
    common = ('room_id','mode','user_id','username')
    if kind == 'baseline':
        fields(p, ('source_id','mode','user_id','username','total_seconds','visit_count','cutoff','first_join_at','last_join_at','last_leave_at'))
        identifier(p['source_id']);integer(p['user_id']);text(p['username'],80)
        require(p['mode'] in (*MODES,'mania'),'invalid_activity_mode')
        number(p['total_seconds']);integer(p['visit_count'],0);number(p['cutoff'])
        for key in ('first_join_at','last_join_at','last_leave_at'):
            if p[key] is not None:number(p[key])
        return value
    if kind == 'achievement':
        fields(p, ('room_id','mode','user_id','username','achievement_key','beatmap_id','playlist_item_id','created_at','profile_gain_pp','score_pp','accuracy','top_rank','historical'))
        integer(p['room_id']);integer(p['user_id']);integer(p['beatmap_id']);text(p['username'],80)
        text(p['achievement_key'],180);require(p['mode'] in (*MODES,'mania'),'invalid_activity_mode')
        if p['playlist_item_id'] is not None:integer(p['playlist_item_id'])
        number(p['created_at']);number(p['score_pp']);number(p['profile_gain_pp'],-1000000,1000000)
        integer(p['top_rank'],1,100);require(type(p['historical']) is bool,'invalid_activity_history')
        if p['accuracy'] is not None:number(p['accuracy'],0,100)
        return value
    if kind == 'privacy':
        fields(p, ('user_id','hidden','updated_at')); integer(p['user_id']);number(p['updated_at'])
        require(type(p['hidden']) is bool,'invalid_activity_privacy'); return value
    require(kind in ('presence','join','leave','match'),'invalid_activity_kind')
    extra = {'presence':('started_at','ended_at'),'join':('occurred_at',),'leave':('occurred_at',),
             'match':('match_key','beatmap_id','started_at','ended_at','score_submitted','accuracy')}[kind]
    fields(p, (*common,*extra));integer(p['room_id']);integer(p['user_id']);text(p['username'],80)
    require(p['mode'] in MODES,'invalid_activity_mode')
    if kind in ('join','leave'): number(p['occurred_at'])
    else:
        number(p['started_at']);number(p['ended_at']);require(p['ended_at']>=p['started_at'],'invalid_activity_interval')
    if kind == 'match':
        text(p['match_key'],160); integer(p['beatmap_id'])
        require(p['match_key'].startswith('room:'+str(p['room_id'])+':'),'invalid_activity_match_key')
        require(type(p['score_submitted']) is bool,'invalid_activity_score')
        if p['accuracy'] is not None:
            require(p['score_submitted'],'unconfirmed_activity_score');number(p['accuracy'],0,100)
    return value


def batch(value):
    fields(value, ('protocol_version','events'));base(value)
    require(type(value['events']) is list and len(value['events'])<=MAX_EVENTS,'activity_batch_too_large')
    require(len(canonical(value))<=MAX_BODY,'activity_batch_too_large',413)
    for item in value['events']: event(item)
    require(len({e['event_id'] for e in value['events']})==len(value['events']),'duplicate_activity_event')
    return value
