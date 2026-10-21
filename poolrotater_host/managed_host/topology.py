"""Pure stopped-host plan for four addressable slots; new slots stay stopped.

The root upgrade witness records this plan before SQLite or configuration writes.
No ownership evidence, credentials or provider requests are manufactured here.
"""
import copy
import os
from pathlib import Path
from ..config import ConfigError, defaults
from ..layout import load, read_json
from ..runtime_config import render, STATE_FILE
from .. import owner_integrations, owner_settings
from .projection import read, NAME
from .settings import FIELDS,HOST_JSON_LIMIT

SLOTS='state/managed-slots.json'
ALIASES=tuple('room'+str(i) for i in range(1,5))


def plan(root,account_id):
    root=Path(root);marker,host=load(root);old={r['alias'] for r in host['rooms']}
    if not old<=set(ALIASES):raise ConfigError('managed_room_topology_requires_review')
    added=[a for a in ALIASES if a not in old]
    if not added:return {}
    shadow=read(root,marker)
    if shadow is None or set(shadow['rooms'])!=old:
        raise ConfigError('managed_room_topology_requires_review')
    # Render original bases before changing anything. All retained native
    # preference deltas are reconciled by RuntimeConfigBridge after publication.
    before=render(root,account_id=account_id)
    extended=copy.deepcopy(host);managed=copy.deepcopy(shadow)
    for a in added:
        room=defaults(['standard-fixed'])['rooms'][0];room['alias']=a
        extended['rooms'].append(room)
        managed['rooms'][a]={'revision':0,'desired':'stopped',
                            'settings':{'preset':'standard-fixed','visibility':'public'}}
    gameplay_path=root/owner_settings.RECORD
    gameplay=owner_settings.validate_record(read_json(gameplay_path),marker,host) if os.path.lexists(gameplay_path) else None
    if gameplay is not None:
        for a in added:gameplay['rooms'][a]={'preset':'standard-fixed','settings':{}}
    integrations=owner_integrations.load(root,marker,host)
    if integrations is not None:
        for row in integrations['rooms'].values():
            if 'discord_control_known_room_aliases' in row['settings']:
                row['settings']['discord_control_known_room_aliases']=list(ALIASES)
        # Carry shared player/staff policy only when every retained slot agrees.
        # Discord channels, AI, credentials and per-room paths are not cloned.
        shared={}
        for key in ('admins','moderators','custom_join_greetings','custom_join_greeting_cooldown_seconds','discord_invite_url'):
            values=[r['settings'].get(key) for r in integrations['rooms'].values()]
            if values and values[0] is not None and all(v==values[0] for v in values):shared[key]=copy.deepcopy(values[0])
        for a in added:
            integrations['rooms'][a]={'settings':copy.deepcopy(shared),
                                      'paths':dict.fromkeys(owner_integrations.PATHS,False)}
    context={'host':extended,'gameplay':gameplay,'integrations':integrations,'managed':managed}
    after=render(root,account_id=account_id,_preparation=context)
    for room in after['rooms']:
        if room['alias'] in added:
            managed['rooms'][room['alias']]['settings']={k:copy.deepcopy(v) for k,v in room['settings'].items() if k in FIELDS}
            managed['rooms'][room['alias']]['settings'].update(preset=room['preset'],visibility='public')
    writes={'config/host.json':extended,NAME:managed,SLOTS:{'format':1,
        **{k:marker[k] for k in ('installation_id','generation')},
        'original_aliases':sorted(old),'added_aliases':added}}
    if gameplay is not None:writes[owner_settings.RECORD]=gameplay
    if integrations is not None:writes[owner_integrations.RECORD]=integrations
    state_path=root/STATE_FILE
    if os.path.lexists(state_path):
        state=read_json(state_path,maximum=HOST_JSON_LIMIT)
        keys=('installation_id','generation','account_id','binding')
        if (type(state) is not dict or set(state)!={'format','identity','bases'}
            or type(state['format']) is not int or state['format']!=1
            or state['identity']!={k:before[k] for k in keys}
            or type(state['bases']) is not dict or set(state['bases'])!=old):
            raise ConfigError('runtime configuration binding changed; coordinated reconfiguration required')
        state=copy.deepcopy(state);state['identity']={k:after[k] for k in keys}
        state['bases'].update({r['alias']:r['settings'] for r in after['rooms'] if r['alias'] in added})
        writes[STATE_FILE]=state
    return {name:{'kind':'json','value':value} for name,value in writes.items()}


def seed(root,writes):
    """Service-UID transaction, using only the root-witnessed slot plan."""
    if SLOTS not in writes:return
    from .policy import state
    value=writes[SLOTS]['value'];marker,_=load(root)
    if any(value[k]!=marker[k] for k in ('installation_id','generation')):
        raise ConfigError('managed_room_topology_requires_review')
    rooms=writes[NAME]['value']['rooms']
    state(root).seed_dormant([{'alias':a,'settings':rooms[a]['settings']} for a in value['added_aliases']])


INITIALIZATION='managed-initialization.json'


def pending(target):
    """Root-only pointer; no mutable service state can request a new plan."""
    from ..provisioning import _control
    value=_control(target['prefix']/INITIALIZATION,private=True,optional=True,maximum=16*1024*1024)
    if value is None:return False
    if type(value) is not dict or set(value)!={'format','job','complete'} or type(value['complete']) is not bool or value['format']!=1:
        raise ConfigError('managed_initialization_requires_review')
    return not value['complete']


def initialize(target,record,instance):
    """Stopped first-start topology transaction, under deployment admission.

    This reuses the upgrade's exact root-owned witness. An interrupted attempt
    resumes the same pinned job; no new configuration snapshot is accepted.
    """
    from ..pilot_installation import _admin,require_no_service_processes
    from ..provisioning import _control,_write,protected
    from .. import pilot_operations as pilot
    from ..network import operations as network
    from ..controller_update import _config_hashes
    from . import upgrade,catalogs
    import uuid
    _admin();protected(target['prefix'])
    path=target['prefix']/INITIALIZATION
    value=_control(path,private=True,optional=True,maximum=upgrade.MAXIMUM)
    if value is not None:pending(target)  # Validate even terminal pointer shape.
    from ..ratings.operations import _identity
    with _identity(record['service_uid']):marker,host=load(target['root'])
    if (value is None or value['complete']) and len(host['rooms'])==4:
        return catalogs.prepare(target,record)
    if value is not None and value['complete']:
        raise ConfigError('managed_initialization_requires_review')
    if value is not None and value['job']['after']!=record:
        raise ConfigError('managed_initialization_requires_review')
    service=pilot._service_state(instance)
    if service['MainPID'] or service['ActiveState'] not in ('inactive','failed'):
        raise ConfigError('managed_initialization_requires_stopped_controller')
    network._checked_unit(instance)
    observed=network._state(instance)
    if observed['MainPID'] or observed['ActiveState'] not in ('inactive','failed'):
        pilot._systemctl('stop',network.name(instance),timeout=25)
        observed=network._state(instance)
        if observed['MainPID'] or observed['ActiveState'] not in ('inactive','failed'):
            raise ConfigError('managed_initialization_requires_stopped_agent')
    require_no_service_processes(record['service_uid'])
    if value is None:
        value={'format':1,'complete':False,'job':{'id':str(uuid.uuid4()),'before':record,'after':record,
                'configuration':_config_hashes(target['root']),'rooms':[r['alias'] for r in host['rooms']]}}
        _write(path,value,private=True)
    try:
        result=upgrade.prepare(target,record,value['job'])
        value['complete']=True;_write(path,value,private=True)
    finally:
        # Keep central administration reachable after catalog outages, but
        # never let its migration writer race a half-published topology.
        if value['complete'] or upgrade.network_safe(target,value['job']):
            pilot._systemctl('start',network.name(instance),timeout=25)
    return {**result,'dormant_slots_prepared':True,'rooms_started':False}
