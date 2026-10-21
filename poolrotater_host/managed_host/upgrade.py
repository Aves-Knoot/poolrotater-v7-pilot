"""Exact stopped configuration preparation for a staged successor.

The updater's root-owned job pins the predecessor configuration. Each planned
replacement is recorded outside the service UID's writable tree *before* its
first write. Recovery admits only original or planned bytes, never a fresh
snapshot of arbitrary edits. Credentials are neither copied nor rewritten.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from ..config import ConfigError
from ..provisioning import _control, _write, protected
from .settings import HOST_JSON_LIMIT

MAXIMUM=64*1024*1024
NAMES=('profiles','projection','topology','native')
SLOTS='state/managed-slots.json'
STATE='state/runtime-config.json'


def _require(value):
    if not value:raise ConfigError('update_recovery_configuration_changed')


def _sha(value):
    return hashlib.sha256((json.dumps(value,indent=2,allow_nan=False)+'\n').encode()).hexdigest()


def _path(target,job):
    value=job.get('id')
    try:valid=type(value) is str and str(uuid.UUID(value))==value
    except (ValueError,AttributeError):valid=False
    _require(valid)
    return target['prefix']/('managed-preparation-'+value+'.json')


def _binding(target,job):
    before,after=job['before'],job['after']
    _require(all(before[k]==after[k] for k in ('installation_id','generation','service_uid','host_root','prefix')))
    _require(Path(after['host_root'])==target['root'] and Path(after['prefix'])==target['prefix'])
    return {'operation_id':job['id'],'installation_id':after['installation_id'],
            'generation':after['generation'],'before_sha256':before['bundle_sha256'],
            'after_sha256':after['bundle_sha256'],'configuration':job['configuration']}


def _inventory(target,uid):
    from ..controller_update import _config_hashes
    result=_config_hashes(target['root'])
    for name in (STATE,SLOTS):
        path=target['root']/name
        if os.path.lexists(path):
            _control(path,uid,private=True,maximum=HOST_JSON_LIMIT)
            result[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _allowed(name):
    return name in ('config/network.json','config/ratings.json','config/reporting.json',
                    'config/ratings-reader.json','config/discord-relay.json',
                    'config/managed-rooms.json','config/rooms/universal.json','config/host.json',
                    'config/gameplay.json','config/integrations.json',STATE,SLOTS) or bool(
                    re.fullmatch(r'config/rooms/room[1-4]\.json(?:\.lock)?',name))


def _validate(value,binding):
    _require(type(value) is dict and set(value)=={'format','binding','initial','steps','complete'}
             and type(value['format']) is int and value['format']==1 and value['binding']==binding
             and type(value['initial']) is dict and type(value['complete']) is bool
             and type(value['steps']) is list and len(value['steps'])<=len(NAMES))
    _require({k:v for k,v in value['initial'].items() if k not in (STATE,SLOTS)}==binding['configuration'])
    before=value['initial']
    for index,step in enumerate(value['steps']):
        _require(type(step) is dict and set(step)=={'name','before','after','writes','complete'}
                 and step['name']==NAMES[index] and step['before']==before
                 and type(step['writes']) is dict and type(step['complete']) is bool)
        _require(index==len(value['steps'])-1 or step['complete'])
        after=dict(before)
        for name,entry in step['writes'].items():
            _require(_allowed(name) and type(entry) is dict and entry.get('kind') in ('json','empty'))
            if entry['kind']=='json':
                _require(set(entry)=={'kind','value'} and not name.endswith('.lock'))
                after[name]=_sha(entry['value'])
            else:
                _require(set(entry)=={'kind'} and name.endswith('.json.lock'))
                after[name]=hashlib.sha256(b'').hexdigest()
        _require(step['after']==after)
        before=after
    _require(not value['complete'] or len(value['steps'])==len(NAMES) and all(s['complete'] for s in value['steps']))
    return value


def _current_matches(value,actual):
    if not value['steps']:return actual==value['initial']
    step=value['steps'][-1]
    if step['complete']:return actual==step['after']
    before,after=step['before'],step['after']
    return set(actual)<=set(after) and all(actual.get(k) in (before.get(k),after.get(k)) for k in after)


def matches(target,job,*,require_complete=False):
    """None means legacy transaction; False means a witnessed drift/refusal."""
    path=_path(target,job)
    value=_control(path,private=True,optional=True,maximum=MAXIMUM)
    if value is None:return None
    protected(target['prefix'])
    value=_validate(value,_binding(target,job))
    return (not require_complete or value['complete']) and _current_matches(value,_inventory(target,job['after']['service_uid']))


def network_safe(target,job):
    """A held upgrade may restart the agent after its profile migration is pinned."""
    value=_control(_path(target,job),private=True,optional=True,maximum=MAXIMUM)
    if value is None:return False
    protected(target['prefix']);value=_validate(value,_binding(target,job))
    return (len(value['steps'])>=2 and all(s['complete'] for s in value['steps'][:2])
            and not any(s['name']=='topology' and not s['complete'] for s in value['steps'])
            and _current_matches(value,_inventory(target,job['after']['service_uid'])))


def _publish(target,uid,name,entry):
    path=target['root']/name
    _require(_allowed(name))
    if entry['kind']=='json':
        if _control(path,uid,private=True,optional=True,maximum=MAXIMUM)!=entry['value']:
            _write(path,entry['value'],owner=uid,private=True)
    elif not os.path.lexists(path):
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:os.fchown(descriptor,uid,uid);os.fsync(descriptor)
        finally:os.close(descriptor)
    else:
        # Lock data is never silently truncated, even while the controller is
        # stopped. The witness already requires exact empty bytes here.
        info=path.lstat()
        _require(info.st_uid==uid and not info.st_mode&0o077 and info.st_nlink==1
                 and not path.is_symlink() and path.read_bytes()==b'')


def _step(target,job,value,name,build,before_publish=None):
    uid=job['after']['service_uid'];index=NAMES.index(name);path=_path(target,job)
    _require(_current_matches(value,_inventory(target,uid)))
    if len(value['steps'])<=index:
        _require(len(value['steps'])==index and (not index or value['steps'][-1]['complete']))
        writes=build()
        before=_inventory(target,uid)
        _require(_current_matches(value,before))
        after=dict(before)
        for key,entry in writes.items():
            after[key]=_sha(entry['value']) if entry['kind']=='json' else hashlib.sha256(b'').hexdigest()
        value['steps'].append({'name':name,'before':before,'after':after,'writes':writes,'complete':False})
        _validate(value,_binding(target,job))
        _write(path,value,private=True)
    step=value['steps'][index]
    if step['complete']:return
    if before_publish is not None:before_publish(step['writes'])
    for relative,entry in step['writes'].items():_publish(target,uid,relative,entry)
    _require(_inventory(target,uid)==step['after'])
    step['complete']=True;_write(path,value,private=True)


def prepare(target,current,job):
    """Called by successor root code under deployment admission, before start."""
    from ..pilot_installation import _admin
    _admin();_require(current==job['after'])
    protected(target['prefix'])
    uid=current['service_uid'];binding=_binding(target,job);path=_path(target,job)
    from ..pilot_installation import _stopped
    from ..update_lifecycle import wait_stopped
    from ..ratings.operations import _identity
    from ..network.client import Client
    from . import policy
    from .migration import profile_plan,migrate
    from .projection import document,NAME
    from ..runtime_config import RuntimeConfigBridge
    # A short-lived dedicated-UID helper can appear after provisioning, even
    # after the earlier cleanup checkpoint passed. Re-observe the same stopped
    # ownership/process guards with the existing bounded cleanup/retry policy.
    wait_stopped(target['root'],uid)
    # Verify original/replay bytes before any preparation work.
    existing=_control(path,private=True,optional=True,maximum=MAXIMUM)
    if existing is None:
        _require({k:v for k,v in _inventory(target,uid).items() if k not in (STATE,SLOTS)}==job['configuration'])
    else:_require(_current_matches(_validate(existing,binding),_inventory(target,uid)))
    with _stopped(target['root'],uid):
        value=_control(path,private=True,optional=True,maximum=MAXIMUM)
        if value is None:
            initial=_inventory(target,uid)
            _require({k:v for k,v in initial.items() if k not in (STATE,SLOTS)}==job['configuration'])
            value={'format':1,'binding':binding,'initial':initial,'steps':[],'complete':False}
            _write(path,value,private=True)
        value=_validate(value,binding)
        _require(_current_matches(value,_inventory(target,uid)))
        if value['complete']:return {'ok':True,'managed_configuration_prepared':True,'replayed':True}
        def profiles():
            with _identity(uid):plan=profile_plan(Client(target['root']))
            return {name:{'kind':'json','value':data} for name,data in plan.items()}
        _step(target,job,value,'profiles',profiles)
        with _identity(uid):
            client=Client(target['root']);store=policy.state(target['root'])
            # No unpinned config projection is emitted between steps.
            migrate(client,store,export_projection=False)
        _require(_current_matches(value,_inventory(target,uid)))
        def projection():
            with _identity(uid):data=document(policy.state(target['root']))
            _require(data is not None)
            return {NAME:{'kind':'json','value':data}}
        _step(target,job,value,'projection',projection)
    # Finish/pin policy first. If catalog delivery fails, the independent agent
    # may remain reachable without racing an unrecorded profile/projection
    # migration against this job's original configuration hashes.
    from .catalogs import prepare as prepare_catalogs
    prepare_catalogs(target,current)
    with _stopped(target['root'],uid):
        latest=_control(path,private=True,maximum=MAXIMUM)
        _require(latest==value and _current_matches(value,_inventory(target,uid)))
        from . import topology
        def topology_plan():
            with _identity(uid):return topology.plan(target['root'],Client(target['root']).profile()['account_id'])
        def topology_seed(writes):
            with _identity(uid):topology.seed(target['root'],writes)
        _step(target,job,value,'topology',topology_plan,before_publish=topology_seed)
        def native():
            with _identity(uid):
                account=Client(target['root']).profile()['account_id']
                preview=RuntimeConfigBridge(target['root'],account_id=account).materialize(preview=True)
            writes={key:{'kind':'json','value':data} for key,data in preview['writes'].items()}
            writes[STATE]={'kind':'json','value':preview['state']}
            for room in preview['rooms']:
                name='config/rooms/'+room['alias']+'.json.lock'
                if not os.path.lexists(target['root']/name):writes[name]={'kind':'empty'}
            return writes
        _step(target,job,value,'native',native)
        value['complete']=True;_write(path,value,private=True)
    return {'ok':True,'managed_configuration_prepared':True,'rooms_started':False}
