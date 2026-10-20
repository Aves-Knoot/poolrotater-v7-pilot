"""Closed M4 event/query contracts; no names, arbitrary chat or provider tokens."""
import re

from ..network.contracts import (require, fields, identifier, integer, number,
                                 canonical, base)
from bot.rating_outbox import provenance, storage_key

MAX_EVENTS = 500
MAX_BODY = 512*1024


def event(value):
    fields(value, ('format','event_id','installation_id','generation','sequence','kind',
                   'beatmap_id','user_id','rating','base_revision','predecessor',
                   'observed_at','map_origin','key_count','context'))
    require(type(value['format']) is int and value['format'] in (1,2), 'unsupported_rating_format')
    for key in ('event_id','installation_id','generation'): identifier(value[key])
    for key in ('sequence','beatmap_id','user_id'): integer(value[key])
    require(value['kind'] in ('rating_set','historical_seed'), 'unsupported_rating_kind')
    if value['kind']=='historical_seed':
        require(value['base_revision'] is None and value['predecessor'] is None,'invalid_historical_seed')
    integer(value['rating'], -1, 1)
    if value['base_revision'] is not None: integer(value['base_revision'], 0)
    if value['predecessor'] is not None: identifier(value['predecessor'])
    number(value['observed_at'])
    require(type(value['map_origin']) is str and re.fullmatch('[A-Za-z0-9_-]{1,32}', value['map_origin']), 'invalid_rating_origin')
    require(value['key_count'] is None or (type(value['key_count']) is int and value['key_count'] in (4,7)), 'invalid_rating_keys')
    fields(value['context'], ('ruleset_id','room_id','playlist_item_id','required_mods'))
    try: require(provenance(value['context']) == value['context'], 'invalid_rating_context')
    except ValueError: require(False, 'invalid_rating_context')
    require(value['format'] == (2 if value['context']['ruleset_id']==2 else 1), 'unsupported_rating_variant_format')
    try: storage_key(value['beatmap_id'], value['context'], map_origin=value['map_origin'], key_count=value['key_count'])
    except ValueError: require(False, 'invalid_rating_variant')
    return value


def batch(value):
    fields(value, ('protocol_version','batch_id','events'))
    base(value); identifier(value['batch_id'])
    require(type(value['events']) is list and 0 < len(value['events']) <= MAX_EVENTS, 'rating_batch_limit')
    require(len(canonical(value)) <= MAX_BODY, 'request_too_large', 413)
    return value


def query(value):
    fields(value, ('protocol_version','event_ids'))
    base(value)
    require(type(value['event_ids']) is list and 0 < len(value['event_ids']) <= 100, 'rating_query_limit')
    for item in value['event_ids']: identifier(item)
    require(len(set(value['event_ids'])) == len(value['event_ids']), 'duplicate_rating_query')
    return value
