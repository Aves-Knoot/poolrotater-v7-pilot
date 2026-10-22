"""Closed relay contract and stable human-readable channel labels."""
import re
import unicodedata
from ..network.contracts import fields, identifier, integer, number, require, text

PATH = '/v1/hosts/self/discord'
# Intentionally exclude process/account topology, shell and whole-host lifecycle.
COMMANDS = frozenset(('help','commands','settings','say','start','abort','forceskip',
    'usercommands','silence','voteskip','ppannounce','aichat','range','auto','length',
    'maxlength','maxage','countdown','autostart','minplayers','queueahead',
    'difficultyvote','diffvote','overview','activity','players','ratings','retention',
    'mapstats','who','apiusage','lazerstatus','kick','ban','unban','bans','pp'))


def slug(value):
    value=unicodedata.normalize('NFKD',str(value)).encode('ascii','ignore').decode().lower()
    return re.sub('[^a-z0-9]+','-',value).strip('-')[:32] or 'host'


def room_kind(preset, settings=None):
    settings=settings or {}
    kind=('4k-mania' if preset=='mania-4k' else '7k-mania' if preset=='mania-7k'
          else 'catch' if preset=='catch-fixed' else 'standard')
    if preset=='standard-loved' or settings.get('selection_style')=='loved':kind='loved'
    automatic=settings.get('auto_difficulty',preset=='standard-auto')
    return kind+('-autodiff' if automatic else '-setdiff')


def channel_name(slot, preset, *, operator=None, settings=None):
    integer(slot,1,9999)
    return f'l{slot}-'+(slug(operator)+'-' if slot>=5 else '')+room_kind(preset,settings)


def command(value):
    fields(value,('id','host_id','installation_id','generation','alias','room_id',
                  'channel_id','discord_user_id','username','role','content','expires_at'))
    for k in ('id','host_id','installation_id','generation'):identifier(value[k])
    require(type(value['alias']) is str and re.fullmatch('room[1-4]',value['alias']), 'invalid_request')
    integer(value['room_id'],1)
    for k in ('channel_id','discord_user_id'):integer(value[k],1,2**63-1)
    text(value['username'],80);text(value['content'],1000);number(value['expires_at'])
    require(value['role'] in ('ADMIN','MOD'),'invalid_request')
    content=value['content'];parts=content[1:].split(maxsplit=1) if content.startswith('.') else []
    require(bool(parts) and parts[0].casefold() in COMMANDS
            and '\n' not in content and '\r' not in content, 'invalid_request')
    from bot.discord_control import DiscordControl
    require(DiscordControl({},start=False)._role_allows_command(value['role'],content), 'invalid_request')
    return value


def exchange(value):
    fields(value,('protocol_version','installation_id','generation','sent_at','receipts','replies'))
    require(type(value['protocol_version']) is int and value['protocol_version']==1,'unsupported_protocol')
    identifier(value['installation_id']);identifier(value['generation']);number(value['sent_at'])
    require(type(value['receipts']) is list and len(value['receipts'])<=16,'invalid_request')
    require(type(value['replies']) is list and len(value['replies'])<=16,'invalid_request')
    for row in value['receipts']:
        fields(row,('id','result'));identifier(row['id'])
        require(row['result'] in ('dispatched','refused','uncertain'),'invalid_request')
    for row in value['replies']:
        fields(row,('id','request_id','content'));identifier(row['id']);identifier(row['request_id']);text(row['content'],1800)
    return value
