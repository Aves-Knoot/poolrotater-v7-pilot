"""Private owner-local integrations and complete configuration parity audit.

Credentials are explicitly provisioned outside these JSON records. This module
checks only file metadata and environment-name presence at startup; it never
reads an env file, returns credential values or opens source production state.
"""
from __future__ import annotations

import copy
import math
import os
import re
import stat
from urllib.parse import urlsplit

from .config import ConfigError, contained, fields
from .layout import read_json, unique_fields

RECORD = 'config/integrations.json'
ENVIRONMENT_FILE = 'secrets/integrations.env'
PATHS = {
    'moderation_bans_file':'state/moderation/bans.json',
    'beatmap_exclusions_file':'state/moderation/beatmap-exclusions.json',
    'ai_context_file':'config/ai-context-{alias}.md',
    'discord_control_health_state_file':'state/controller-discord-health.json',
    'discord_public_status_state_file':'state/controller-discord-public.json',
    'map_pp_cache_dir':'state/controller-map-pp',
}
FLAGS=frozenset({'ai_enabled','discord_control_enabled','discord_modlog_enabled',
    'discord_modlog_include_instance_name','discord_control_accept_untargeted','chat_websocket_enabled'})
ENV_KEYS=frozenset({'ai_api_key_env','discord_control_bot_token_env','discord_modlog_webhook_env'})
ID_KEYS=frozenset({'discord_control_channel_id','discord_control_guild_id','discord_public_status_channel_id'})
TEXT_KEYS=frozenset({'ai_model','ai_endpoint','discord_public_status_channel_name',
    'discord_public_status_label','discord_control_room_alias','discord_control_primary_instance'})
URL_KEYS=frozenset({'ai_base_url','discord_invite_url','discord_public_join_base_url',
    'chat_websocket_endpoint','map_pp_osu_url_template'})
NUMBER_KEYS={
    'ai_config_reload_seconds':(.1,300),'ai_global_cooldown_seconds':(0,86400),
    'ai_user_cooldown_seconds':(0,86400),'ai_max_output_chars':(1,10000),
    'ai_max_output_tokens':(1,10000),'ai_max_pending':(1,100),'ai_max_prompt_chars':(1,100000),
    'ai_recent_chat_messages':(0,1000),'ai_temperature':(-1,2),'ai_timeout_seconds':(.1,600),
    'discord_invite_reminder_seconds':(0,604800),'discord_modlog_timeout_seconds':(.1,600),
    'discord_status_rename_spacing_seconds':(0,3600),'chat_websocket_reconnect_max_seconds':(1,600),
    'map_pp_cache_days':(0,3650),'map_pp_timeout_seconds':(.1,600),
    'map_pp_announce_wait_seconds':(0,600),'map_pp_command_wait_seconds':(0,600),
    'custom_join_greeting_cooldown_seconds':(0,86400),
}
LIST_KEYS=frozenset({'admins','moderators','discord_control_admin_role_names',
    'discord_control_moderator_role_names','discord_control_known_room_aliases'})
DICT_KEYS=frozenset({'custom_join_greetings','discord_control_channel_ids_by_room'})
INTEGRATION_KEYS=FLAGS|ENV_KEYS|ID_KEYS|TEXT_KEYS|URL_KEYS|set(NUMBER_KEYS)|LIST_KEYS|DICT_KEYS
HOST_KEYS=frozenset({'ruleset_id','mania_keys','auto_difficulty','sr_min','sr_max',
    'default_response_language','room_count','bot_user_id','version','discord_control_room_alias'})
MANAGED_KEYS=frozenset({'farm_db','standard_dt_db','standard_catalog_binding','mania_db','mania_farm_db',
    'mania_convert_db','mania_convert_validation_db','loved_db','ratings_db','api_rate_db','process_control_db',
    'lazer_coordinator_db','token_file','oauth_client_id_env','oauth_client_secret_env','spectator_url','log_file'})|set(PATHS)
SECRET_KEYS=frozenset({'access_token','refresh_token','client_secret','password','api_key',
    'discord_bot_token','discord_webhook_url'})
LEGACY_CREDENTIAL_NAMES=frozenset({'POOLROTATER_LLM_API_KEY','POOLROTATER_DISCORD_BOT_TOKEN',
                                 'POOLROTATER_DISCORD_MODLOG_WEBHOOK'})
RESERVED_ENV_NAMES=frozenset({'HOME','PATH','USER','LOGNAME','SHELL','TMPDIR','TEMP','TMP',
    'SSL_CERT_FILE','SSL_CERT_DIR','REQUESTS_CA_BUNDLE','CURL_CA_BUNDLE',
    'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','ENV','BASH_ENV','IFS',
    'CDPATH','GCONV_PATH','LOCPATH','LIBPATH','SHLIB_PATH','LANG','LANGUAGE','TZ',
    'DISPLAY','XAUTHORITY','SSH_AUTH_SOCK','WGETRC','XDG_CONFIG_HOME','XDG_DATA_HOME',
    'XDG_CACHE_HOME','XDG_RUNTIME_DIR'})
RESERVED_ENV_PREFIXES=('PYTHON','LD_','DYLD_','POOLROTATER_','SYSTEMD_','GLIBC_','MALLOC_',
                       'LC_','BASH_','SHELLOPTS','OPENSSL_')


def credential_environment_name(value):
    if type(value) is not str or re.fullmatch('[A-Z][A-Z0-9_]{0,95}',value) is None:return False
    return value in LEGACY_CREDENTIAL_NAMES or (value not in RESERVED_ENV_NAMES
        and not value.startswith(RESERVED_ENV_PREFIXES))


def _text(value,maximum=256,*,empty=True):
    return type(value) is str and (empty or bool(value)) and len(value)<=maximum and all(ord(c)>=32 and ord(c)!=127 for c in value)


def _url(key,value):
    if not _text(value,1024):return False
    if not value:return True
    if key=='map_pp_osu_url_template':return value=='https://osu.ppy.sh/osu/{beatmap_id}'
    if key=='chat_websocket_endpoint':
        return value=='wss://notify.ppy.sh'
    parsed=urlsplit(value)
    return (parsed.scheme=='https' and parsed.hostname is not None and parsed.username is None
            and parsed.password is None and not parsed.query and not parsed.fragment)


def validate_settings(value,aliases):
    if not isinstance(value,dict) or set(value)-INTEGRATION_KEYS:
        raise ConfigError('owner integration contains undeclared fields')
    for key,item in value.items():
        good=False
        if key in FLAGS:good=type(item) is bool
        elif key in ENV_KEYS:good=credential_environment_name(item)
        elif key in ID_KEYS:good=type(item) is int and 0<=item<2**64
        elif key in TEXT_KEYS:good=_text(item,256)
        elif key in URL_KEYS:good=_url(key,item)
        elif key in NUMBER_KEYS:
            low,high=NUMBER_KEYS[key]
            good=type(item) in (int,float) and math.isfinite(item) and low<=item<=high
        elif key in ('admins','moderators'):
            good=type(item) is list and len(item)<=1000 and all(type(x) is int and 0<x<2**63 for x in item)
        elif key in LIST_KEYS:
            good=type(item) is list and len(item)<=100 and all(_text(x,100,empty=False) for x in item)
            if key=='discord_control_known_room_aliases':good=good and set(item)==set(aliases) and len(item)==len(set(item))
        elif key=='custom_join_greetings':
            good=type(item) is dict and len(item)<=1000 and all(_text(k,80,empty=False) and _text(v,1000) for k,v in item.items())
        elif key=='discord_control_channel_ids_by_room':
            good=type(item) is dict and set(item)<=set(aliases) and all(type(x) is int and 0<x<2**64 for x in item.values())
        if not good:raise ConfigError('owner integration field invalid')
    for key in ('discord_control_room_alias','discord_control_primary_instance'):
        if key in value and value[key] not in aliases:raise ConfigError('owner integration alias invalid')
    return copy.deepcopy(value)


def classify(key):
    from .owner_settings import GAMEPLAY_KEYS
    if key in SECRET_KEYS:return 'credential_value_refused'
    if key in HOST_KEYS:return 'host_identity'
    if key in GAMEPLAY_KEYS:return 'gameplay'
    if key in INTEGRATION_KEYS:return 'owner_integration'
    if key in MANAGED_KEYS:return 'managed_mapping'
    return 'unclassified'


def project(effective_rooms,marker):
    """Private projection: IDs/greetings are retained only in the private record.

    Every key is classified. An unknown key is an explicit blocker; no values
    enter the public summary. PATHS requests preserve non-empty source policy
    state through a deterministic later stopped snapshot/import, never here.
    """
    if not isinstance(effective_rooms,dict) or not 1<=len(effective_rooms)<=4:
        raise ConfigError('owner effective room objects invalid')
    aliases=set(effective_rooms);rooms={};blockers=[];total=0
    if any(type(a) is not str or re.fullmatch('[a-z][a-z0-9_-]{0,23}',a) is None for a in aliases):
        raise ConfigError('owner integration alias invalid')
    for alias,cfg in effective_rooms.items():
        if not isinstance(cfg,dict):raise ConfigError('owner effective configuration invalid')
        settings=validate_settings({k:v for k,v in cfg.items() if k in INTEGRATION_KEYS},aliases)
        paths={key:bool(cfg.get(key)) for key in PATHS}
        rooms[alias]={'settings':settings,'paths':paths}
        for key in cfg:
            if type(key) is not str or re.fullmatch('[a-z][a-z0-9_]{0,95}',key) is None:
                raise ConfigError('owner effective key invalid')
            total+=1
            if classify(key) in ('unclassified','credential_value_refused'):blockers.append({'alias':alias,'key':key,'reason':classify(key)})
    for key in ('moderation_bans_file','beatmap_exclusions_file','discord_control_health_state_file',
                'discord_public_status_state_file','map_pp_cache_dir'):
        distinct={cfg[key] for cfg in effective_rooms.values() if cfg.get(key)}
        if len(distinct)>1:blockers.append({'alias':'all','key':key,'reason':'distinct_source_paths_require_explicit_mapping'})
    record={'format':1,**{k:marker[k] for k in ('installation_id','generation')},
            'environment_file':ENVIRONMENT_FILE,'rooms':rooms}
    return {'record':record,'summary':{'ok':not blockers,'checked_keys':total,'blockers':blockers,
            'credentials_copied':False,'production_mutated':False}}


def validate_record(value,marker,config):
    fields(value,('format','installation_id','generation','environment_file','rooms'),'owner integration record')
    if (type(value['format']) is not int or value['format']!=1
            or any(value[k]!=marker[k] for k in ('installation_id','generation'))
            or value['environment_file']!=ENVIRONMENT_FILE):raise ConfigError('owner integration binding invalid')
    aliases={r['alias'] for r in config['rooms']}
    if not isinstance(value['rooms'],dict) or set(value['rooms'])!=aliases:raise ConfigError('owner integration topology invalid')
    for alias,row in value['rooms'].items():
        fields(row,('settings','paths'),'owner integration room')
        validate_settings(row['settings'],aliases)
        if not isinstance(row['paths'],dict) or set(row['paths'])!=set(PATHS) or any(type(v) is not bool for v in row['paths'].values()):
            raise ConfigError('owner integration path declaration invalid')
        if row['settings'].get('discord_control_room_alias',alias)!=alias:raise ConfigError('owner integration room alias differs')
    return copy.deepcopy(value)


def load(root,marker,config):
    path=contained(root,RECORD)
    if not os.path.lexists(path):return None
    return validate_record(read_json(path,maximum=512*1024),marker,config)


def settings(root,record,alias):
    if record is None:return {}
    row=record['rooms'][alias];value=copy.deepcopy(row['settings'])
    value.update({key:str(contained(root,path.format(alias=alias))) if enabled else '' for key,enabled in row['paths'].items() for path in (PATHS[key],)})
    return value


def _ban_bytes(root):
    """Native BanStore publishes0640; private0700 parents keep it service-only."""
    path=contained(root,PATHS['moderation_bans_file'])
    for parent in (root,path.parent.parent,path.parent):
        info=parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid()
                or stat.S_IMODE(info.st_mode)!=0o700):
            raise ConfigError('owner integration preserved bans parent invalid')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_gid!=os.getegid()
                or info.st_nlink!=1 or stat.S_IMODE(info.st_mode) not in (0o600,0o640)
                or info.st_size>8*1024*1024):raise ConfigError('owner integration preserved bans metadata invalid')
        raw=stream.read(8*1024*1024+1)
    if len(raw)>8*1024*1024:raise ConfigError('owner integration preserved bans invalid')
    return raw


def _ban_policy(root):
    import json
    try:
        return json.loads(_ban_bytes(root),object_pairs_hook=unique_fields,
            parse_constant=lambda _:(_ for _ in ()).throw(ConfigError('owner integration preserved bans invalid')))
    except ConfigError:raise
    except (ValueError,UnicodeError) as exc:raise ConfigError('owner integration preserved bans invalid') from exc


def admission(root,record,*,capabilities=()):
    """Gate before any SharedDiscord/worker/room creation; no env file contents."""
    if record is None:return
    required=set()
    for alias,row in record['rooms'].items():
        cfg=row['settings']
        for flag,key in [('discord_control_enabled','discord_control_bot_token_env'),
                         ('discord_modlog_enabled','discord_modlog_webhook_env'),('ai_enabled','ai_api_key_env')]:
            if cfg.get(flag):
                if key not in cfg:raise ConfigError('owner integration credential reference missing')
                required.add(cfg[key])
        if cfg.get('chat_websocket_enabled') and 'provider_chat_websocket' not in capabilities:
            raise ConfigError('owner integration chat websocket adapter not qualified')
    if required:
        path=contained(root,ENVIRONMENT_FILE)
        try:info=path.lstat()
        except FileNotFoundError:raise ConfigError('owner integration credentials not provisioned') from None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1
                or info.st_mode&0o077 or not 0<info.st_size<=65536):raise ConfigError('owner integration credential file metadata invalid')
        if any(name not in os.environ or not os.environ[name] for name in required):
            raise ConfigError('owner integration credential environment not provisioned')
    # Declared existing private policies cannot silently become empty defaults.
    # These are NEW host-local copies, never source production files.
    for alias,row in record['rooms'].items():
        for key in ('moderation_bans_file','beatmap_exclusions_file','ai_context_file'):
            if not row['paths'][key]:continue
            path=contained(root,PATHS[key].format(alias=alias))
            try:info=path.lstat()
            except FileNotFoundError:raise ConfigError('owner integration preserved state not imported') from None
            mode_ok=(stat.S_IMODE(info.st_mode) in (0o600,0o640) and info.st_gid==os.getegid()
                     if key=='moderation_bans_file' else not info.st_mode&0o077)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1
                    or not mode_ok):raise ConfigError('owner integration preserved state metadata invalid')
            if key=='moderation_bans_file':
                policy=_ban_policy(root)
                if (not isinstance(policy,dict) or type(policy.get('schema')) is not int or policy['schema']!=1
                        or not isinstance(policy.get('bans'),list) or not isinstance(policy.get('audit'),list)):
                    raise ConfigError('owner integration preserved bans invalid')
                seen=set()
                for item in policy['bans']:
                    if (not isinstance(item,dict) or type(item.get('user_id')) is not int
                            or item['user_id']<=0 or item['user_id'] in seen):
                        raise ConfigError('owner integration preserved bans invalid')
                    seen.add(item['user_id']);expiry=item.get('expires_at')
                    if expiry is not None and (type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry<=0):
                        raise ConfigError('owner integration preserved bans invalid')
            if key=='beatmap_exclusions_file':
                policy=read_json(path,maximum=1024*1024)
                if (not isinstance(policy,dict) or type(policy.get('schema')) is not int or policy['schema']!=1
                        or not isinstance(policy.get('exclusions'),list)
                        or any(not isinstance(item,dict) or type(item.get('beatmap_id')) is not int or item['beatmap_id']<=0
                               for item in policy['exclusions'])):
                    raise ConfigError('owner integration preserved exclusions invalid')


def environment_names(root):
    """Verified bootstrap allowlist only; neither values nor env-file reading."""
    from .layout import load as host_load
    marker,config=host_load(root);record=load(root,marker,config)
    if record is None:return set()
    names=set()
    for row in record['rooms'].values():
        cfg=row['settings']
        for flag,key in [('discord_control_enabled','discord_control_bot_token_env'),
                         ('discord_modlog_enabled','discord_modlog_webhook_env'),('ai_enabled','ai_api_key_env')]:
            if cfg.get(flag):
                if key not in cfg:raise ConfigError('owner integration credential reference missing')
                names.add(cfg[key])
    return names


def parity(source,target,*,mappings=None,additions=()):
    """Count-only exhaustive audit; exact value AND type, no secret values/hash.

    Explicit mappings are key -> (expected source, approved target), privately
    supplied by the stopped migration. They can only remap managed identities/
    paths; gameplay and integrations always remain exactly equal.
    """
    if not isinstance(source,dict) or not isinstance(target,dict):raise ConfigError('owner parity objects invalid')
    mappings={} if mappings is None else mappings;checked=0;mismatches=[];classes={}
    allowed_additions={'ratings_schema_prepared','ratings_reader_mode','ratings_reader_root','ratings_reader_epoch',
                       'standard_catalog_binding','standard_dt_db'}
    if not set(additions)<=allowed_additions:raise ConfigError('owner parity additions not reviewed')
    if any(type(k) is not str or re.fullmatch('[a-z][a-z0-9_]{0,95}',k) is None for k in set(source)|set(target)):
        raise ConfigError('owner parity key invalid')
    for key in sorted(set(source)|set(target)):
        if key not in source and key in target and key in additions:
            classes['distributed_addition']=classes.get('distributed_addition',0)+1;checked+=1;continue
        category=classify(key);classes[category]=classes.get(category,0)+1;checked+=1
        if category in ('unclassified','credential_value_refused'):
            mismatches.append({'key':key,'reason':category});continue
        if key not in source or key not in target:
            mismatches.append({'key':key,'reason':'missing_effective_key'});continue
        before,after=source[key],target[key]
        equal=type(before) is type(after) and before==after
        if not equal and key in mappings and category in ('managed_mapping','host_identity'):
            pair=mappings[key]
            equal=(type(pair) in (tuple,list) and len(pair)==2 and type(before) is type(pair[0])
                   and type(after) is type(pair[1]) and before==pair[0] and after==pair[1])
        if not equal:mismatches.append({'key':key,'reason':'effective_value_or_type_changed'})
    return {'ok':not mismatches,'checked_keys':checked,'classes':classes,'mismatches':mismatches,
            'credential_values_returned':False,'provider_dispatch':False}


def prepare_adoption(defaults,room_configs,universal,marker):
    """Pure private preparation, with exhaustive count-only scope admission.

    Source must already be authorized/private reviewed configuration objects.
    The returned gameplay/integration records are private migration inputs;
    only summary is suitable for support. No state/OAuth file is opened/copied.
    Unknown/credential-value fields block adoption, even when a feature is off.
    """
    from .owner_settings import effective_legacy,project_legacy
    effective=effective_legacy(defaults,room_configs,universal)
    integrations=project(effective,marker)
    gameplay=project_legacy(defaults,room_configs,universal)
    return {'gameplay_projection':gameplay,'integration_record':integrations['record'],
            'summary':integrations['summary']}
