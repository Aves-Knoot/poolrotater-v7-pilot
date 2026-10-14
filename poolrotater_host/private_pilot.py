"""One stopped-configured private Standard pilot; parent-only room access secret."""
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
import copy,os,re,stat,uuid
from .config import ConfigError,contained
from .layout import atomic_json,checked_root,load,owner_lease,read_json
PROFILE='secrets/private-pilot.json'
ACCESS='secrets/private-pilot-access.json'
DESIRED='secrets/private-pilot-password.json'
DISCORD='secrets/private-pilot-discord.json'
FIELDS={'format','installation_id','generation','session_id','alias'}
ACCESS_FIELDS=FIELDS|{'room_id','channel_id','password'}
PRIVATE_TITLE='PoolRotater private test (Standard)'

@dataclass(frozen=True)
class PrivatePilot:
    installation_id:str
    generation:object
    session_id:str
    alias:str
    def record(self):return {'format':1,'installation_id':self.installation_id,'generation':self.generation,'session_id':self.session_id,'alias':self.alias}

def _private(root,name,*,maximum=8192):
    p=contained(root,name);s=p.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.geteuid() or s.st_nlink!=1 or stat.S_IMODE(s.st_mode)!=0o600 or s.st_size>maximum:raise ConfigError('private pilot record requires review')
    return read_json(p,maximum=maximum)

def _validated(root,value):
    marker,host=load(root)
    if type(value)is not dict or set(value)!=FIELDS or type(value['format'])is not int or value['format']!=1 or any(value[k]!=marker[k] for k in('installation_id','generation')):raise ConfigError('private pilot binding requires review')
    try:
        if type(value['session_id'])is not str or str(uuid.UUID(value['session_id']))!=value['session_id']:raise ValueError
    except Exception:raise ConfigError('private pilot session requires review')from None
    selected=[r for r in host['rooms']if r['alias']==value['alias']]
    if value['alias']!='room1' or len(selected)!=1 or selected[0]['preset']not in('standard-auto','standard-fixed'):raise ConfigError('private pilot requires configured Standard room1')
    return PrivatePilot(**{k:value[k]for k in FIELDS-{'format'}})

def load_profile(root):
    root=checked_root(root)
    try:value=_private(root,PROFILE)
    except FileNotFoundError:return None
    return _validated(root,value)

def status(root):
    profile=load_profile(root)
    return {'ok':True,'private_test_configured':profile is not None,'selected_alias':profile.alias if profile else None,'session_id':profile.session_id if profile else None,'access_record_present':bool(profile and os.path.lexists(contained(root,ACCESS))),'service_started':False,'public':False,'discord_status_visible':bool(profile and discord_status_enabled(root,profile))}

def discord_status_enabled(root,profile):
    """Explicit native Discord-board visibility; website/directory remain private."""
    if profile!=load_profile(root):raise ConfigError('private pilot profile changed')
    try:value=_private(root,DISCORD)
    except FileNotFoundError:return False
    if (type(value)is not dict or set(value)!=FIELDS|{'discord_status'}
            or any(value[k]!=profile.record()[k]for k in FIELDS)
            or type(value['discord_status'])is not bool):raise ConfigError('private pilot Discord visibility requires review')
    _validated(root,{k:value[k]for k in FIELDS})
    return value['discord_status']

def desired_password(root,profile):
    if profile!=load_profile(root):raise ConfigError('private pilot profile changed')
    try:value=_private(root,DESIRED)
    except FileNotFoundError:return None
    if type(value)is not dict or set(value)!=FIELDS|{'password'} or any(value[k]!=profile.record()[k]for k in FIELDS):raise ConfigError('private pilot password binding requires review')
    _validated(root,{k:value[k]for k in FIELDS})
    _password(value['password'])
    return value['password']

def _password(value):
    if type(value)is not str or not re.fullmatch('[A-Za-z0-9_-]{8,128}',value):raise ConfigError('private pilot password requires review')

def configure(root,*,alias='room1',password=None,discord_status=None,owner_lease_held=False):
    root=checked_root(root)
    if password is not None:_password(password)
    if discord_status is not None and type(discord_status)is not bool:raise ConfigError('private pilot Discord visibility input requires review')
    with nullcontext()if owner_lease_held else owner_lease(root):
        current=load_profile(root)
        if current is not None:
            if current.alias!=alias:raise ConfigError('private pilot already configured')
            desired_password(root,current)
            discord_status_enabled(root,current)
            if password is not None:atomic_json(contained(root,DESIRED),{**current.record(),'password':password})
            if discord_status is not None:atomic_json(contained(root,DISCORD),{**current.record(),'discord_status':discord_status})
            return status(root)
        marker,_=load(root);value={'format':1,'installation_id':marker['installation_id'],'generation':marker['generation'],'session_id':str(uuid.uuid4()),'alias':alias}
        _validated(root,value)
        if os.path.lexists(contained(root,ACCESS)):raise ConfigError('retained private pilot access requires explicit clear')
        if os.path.lexists(contained(root,DESIRED)):raise ConfigError('retained private pilot password requires explicit clear')
        if os.path.lexists(contained(root,DISCORD)):raise ConfigError('retained private pilot Discord visibility requires explicit clear')
        atomic_json(contained(root,PROFILE),value)
        if password is not None:atomic_json(contained(root,DESIRED),{**value,'password':password})
        if discord_status is not None:atomic_json(contained(root,DISCORD),{**value,'discord_status':discord_status})
        return status(root)

def clear(root,*,owner_lease_held=False):
    root=checked_root(root)
    with nullcontext()if owner_lease_held else owner_lease(root):
        profile=load_profile(root)
        if os.path.lexists(contained(root,DISCORD)):
            value=_private(root,DISCORD)
            if type(value)is not dict or set(value)!=FIELDS|{'discord_status'} or type(value['discord_status'])is not bool:raise ConfigError('private pilot Discord visibility requires review')
            _validated(root,{k:value[k]for k in FIELDS})
            if profile and any(value[k]!=profile.record()[k]for k in FIELDS):raise ConfigError('private pilot Discord visibility binding requires review')
        if os.path.lexists(contained(root,DESIRED)):
            value=_private(root,DESIRED)
            if type(value)is not dict or set(value)!=FIELDS|{'password'}:raise ConfigError('private pilot password requires review')
            _validated(root,{k:value[k]for k in FIELDS});_password(value['password'])
            if profile and any(value[k]!=profile.record()[k]for k in FIELDS):raise ConfigError('private pilot password binding requires review')
        if os.path.lexists(contained(root,ACCESS)):
            value=_private(root,ACCESS)
            if type(value)is not dict or set(value)!=ACCESS_FIELDS:raise ConfigError('private pilot access requires review')
            _validated(root,{k:value[k]for k in FIELDS})
            if profile and any(value[k]!=profile.record()[k]for k in FIELDS):raise ConfigError('private pilot access binding requires review')
            contained(root,ACCESS).unlink()
        contained(root,DISCORD).unlink(missing_ok=True)
        contained(root,DESIRED).unlink(missing_ok=True)
        if profile:contained(root,PROFILE).unlink()
        return status(root)

def capture_access(root,profile,*,room_id,channel_id,password):
    if profile!=load_profile(root):raise ConfigError('private pilot profile changed')
    if any(type(v)is not int or not 0<v<10**19 for v in(room_id,channel_id)) or type(password)is not str or not re.fullmatch('[A-Za-z0-9_-]{8,128}',password):raise ConfigError('private pilot access was not confirmed')
    atomic_json(contained(root,ACCESS),{**profile.record(),'room_id':room_id,'channel_id':channel_id,'password':password})

def directory(root,profile):
    return contained(root,'config/private-pilot/'+profile.session_id)

def private_room(root,room,session):
    profile=load_profile(root)
    if profile is None or profile.session_id!=session or room['alias']!=profile.alias:raise ConfigError('private pilot worker binding changed')
    result=copy.deepcopy(room);result['config_path']=str(directory(root,profile)/(profile.alias+'.json'))
    result['settings'].update(room_name=PRIVATE_TITLE,hard_restart_interval_seconds=0,auto_empty_reset_seconds=0)
    if discord_status_enabled(root,profile):
        settings=result['settings']
        if settings.get('discord_control_enabled')is not True or not int(settings.get('discord_public_status_channel_id',0)or 0):
            raise ConfigError('private pilot requested Discord board is not configured')
    else:result['settings']['discord_public_status_channel_id']=0
    return result

def scrub(value):
    if type(value)is dict:return {k:scrub(v)for k,v in value.items()if k.casefold()!='password'}
    if type(value)is list:return [scrub(v)for v in value]
    if type(value)is tuple:return tuple(scrub(v)for v in value)
    return value

def configured_rooms(root,bridge,profile):
    """Shadow native preferences after the unchanged all-room admission/render."""
    from run_multiroom import load_rooms
    from .runtime_config import render_room,validate_room_data,pinned_keys
    if profile!=load_profile(root):raise ConfigError('private pilot profile changed')
    original=bridge.load_rooms();native=render_room(root,profile.alias,account_id=bridge.account_id)
    private=private_room(root,native,profile.session_id);folder=directory(root,profile)
    if not folder.parent.exists():folder.parent.mkdir(mode=0o700)
    if not folder.exists():folder.mkdir(mode=0o700)
    for p in(folder,folder.parent):
        s=p.lstat()
        if not stat.S_ISDIR(s.st_mode)or s.st_uid!=os.geteuid()or s.st_mode&0o077:raise ConfigError('private pilot preferences require private directories')
    path=Path(private['config_path']);universal=folder/'universal.json';supervisor=folder/'supervisor.json'
    if not path.exists():
        values=read_json(Path(native['config_path']));validate_room_data(values,native['settings']);values.update({k:private['settings'][k]for k in('room_name','hard_restart_interval_seconds','auto_empty_reset_seconds','discord_public_status_channel_id')});atomic_json(path,values)
    values=read_json(path);wanted=private['settings']['discord_public_status_channel_id']
    if type(values.get('discord_public_status_channel_id'))is not type(wanted)or values.get('discord_public_status_channel_id')!=wanted:
        # Only this fixed session setting may transition under stopped explicit
        # opt-in. Validate all other pinned values before changing the shadow.
        old=values.get('discord_public_status_channel_id');original_id=native['settings']['discord_public_status_channel_id']
        if not ((type(old)is int and old==0)or(type(old)is type(original_id)and old==original_id)):raise ConfigError('private pilot Discord status channel requires review')
        previous=copy.deepcopy(private['settings']);previous['discord_public_status_channel_id']=old
        validate_room_data(values,previous);values['discord_public_status_channel_id']=wanted;atomic_json(path,values)
    validate_room_data(values,private['settings'])
    if not universal.exists():atomic_json(universal,{})
    if read_json(universal)!={}:raise ConfigError('private pilot universal overrides require review')
    if not supervisor.exists():atomic_json(supervisor,{'deployment_empty_seconds':3600})
    shadow=load_rooms([path],universal)[0]
    selected=next(c for c in original if c['_instance_name']==profile.alias)
    # Preserve only parent-owned hidden transport/catalog/account context;
    # public gameplay values come from the shadow, never persisted originals.
    shadow.update({k:copy.deepcopy(v)for k,v in selected.items()if k.startswith('_')and k not in('_config_path','_universal_config_path','_default_config','_universal_config_keys','_supervisor_overrides')})
    shadow['_supervisor_overrides']=copy.deepcopy(selected['_supervisor_overrides'])
    shadow['_supervisor_overrides'].update({k:private['settings'][k]for k in pinned_keys(private['settings'])})
    shadow['_host_private_pilot_session']=profile.session_id
    shadow['_supervisor_overrides']['_host_private_pilot_session']=profile.session_id
    shadow['_supervisor_overrides'].update({k:private['settings'][k]for k in('room_name','hard_restart_interval_seconds','auto_empty_reset_seconds','discord_public_status_channel_id')})
    result=[]
    for cfg in original:
        target=shadow if cfg['_instance_name']==profile.alias else cfg
        # Only explicit opted-in room1 may use its original native Discord board.
        # Other aliases stay suppressed; website/directory masking is separate.
        public_id=private['settings']['discord_public_status_channel_id']if cfg['_instance_name']==profile.alias else 0
        target['discord_public_status_channel_id']=public_id
        target['_supervisor_overrides']['discord_public_status_channel_id']=public_id
        result.append(target)
    return result
