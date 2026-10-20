"""Explicit stopped Catch addition with root-held configuration backups.

No room creation, OAuth access, service startup or automatic topology changes.
The normal protected administrator entry and stopped account/process guards are
required. A pending root-owned transition fences controller startup.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .config import ConfigError, contained, fields, validate
from .layout import atomic_json, file_lease, load, read_json
from .provisioning import _control, _write, protected
from .pilot_installation import administrative_binding, _stopped, require_no_service_processes
from .ratings.operations import _identity
from . import pilot_operations as pilot
from .network import operations as network
from . import owner_settings as gameplay, owner_integrations as integrations
from .catch_pilot_configuration import project

NAME='catch-pilot-transition.json'
PLAN='catch-pilot-transition-private.json'
TERMINAL=('committed','rolled_back')


def admission(root):
    """Read only the protected installation's compact transition fence."""
    root=Path(root);pointer=root/'state/runtime-installation.json'
    if not pointer.exists():return
    value=read_json(pointer)
    prefix=protected(Path(value['prefix']))
    fence=_control(prefix/NAME,optional=True)
    if fence is None:return
    marker,_=load(root)
    if (fence.get('format')!=1 or any(fence.get(k)!=marker[k] for k in ('installation_id','generation'))
            or fence.get('phase') not in TERMINAL):
        raise ConfigError('Catch configuration transition requires stopped completion')


def _bytes(root,name):
    path=contained(root,name)
    if not os.path.lexists(path):return None
    read_json(path,maximum=1024**2) # private owner/type/JSON checks before preserving raw bytes
    return path.read_bytes()


def _encoded(raw):return None if raw is None else base64.b64encode(raw).decode('ascii')
def _decoded(raw):return None if raw is None else base64.b64decode(raw,validate=True)
def _object(raw):return None if raw is None else json.loads(_decoded(raw))
def _canonical(value):return (json.dumps(value,indent=2,allow_nan=False)+'\n').encode()


def _fence(target,job,phase):
    job['phase']=phase
    _write(target['prefix']/PLAN,job,private=True)
    value={k:job[k] for k in ('format','id','installation_id','generation','parent_sha256','alias','phase')}
    value['changed_at']=time.time()
    _write(target['prefix']/NAME,value)


def _summary(job):
    return {'ok':True,'phase':job['phase'],'transition_id':job['id'],'alias':job['alias'],
            'configuration_only':True,'credentials_untouched':True,'ratings_untouched':True,
            'service_started':False,'provider_dispatch':False,'actual_room_testing':False}


def _load_job(target,marker,parent):
    job=_control(target['prefix']/PLAN,private=True)
    if (job.get('format')!=1 or any(job.get(k)!=marker[k] for k in ('installation_id','generation'))
            or (job.get('parent_sha256')!=parent['bundle_sha256'] and not (
                job.get('phase') in TERMINAL and _historical_parent(target,parent,job.get('parent_sha256'))))):
        raise ConfigError('Catch transition belongs to another installation or runtime')
    return job


def _historical_parent(target,parent,digest):
    """Retain terminal Catch rollback across exact committed controller upgrades.

    Pending configuration must finish under its original runtime. Terminal
    schema-1 records may use only the already checked native upgrade lineage;
    arbitrary old paths, foreign namespaces and unfinished/rollback links do
    not authorize adoption. The original parent digest remains provenance.
    """
    from .pilot_upgrade import _record,_transition
    from .runtime_lifecycle import read_transition,validate_runtime
    from .worker_releases import _committed_ancestors
    value=_control(target['prefix']/'pilot-upgrade.json',optional=True,maximum=512*1024)
    if value is None:return False
    value=_record(value,target['prefix'],parent['installation_id'])
    if value['phase']!='committed' or value['after']!=parent:return False
    if read_transition(target['prefix'],parent['installation_id'])!=_transition(value,'committed'):
        raise ConfigError('Catch configuration controller lineage requires review')
    validate_runtime(parent,target['prefix'])
    def candidates():
        yield value['before']
        yield from _committed_ancestors(parent,value)
    for ancestor in candidates():
        if ancestor['bundle_sha256']!=digest:continue
        if any(ancestor.get(k)!=parent[k]for k in ('installation_id','generation','host_root','service_uid','prefix','execution')):
            return False
        checked=validate_runtime(ancestor,target['prefix'])
        return 'poolrotater_host/catch_pilot_operations.py' in checked['manifest']['files']
    return False


def _prepare(target,marker,parent,value):
    fields(value,('alias','sr_min','sr_max','title','expected_host_sha256'),'Catch transition request')
    if os.path.lexists(target['prefix']/PLAN):
        previous=_control(target['prefix']/PLAN,private=True)
        if previous.get('phase')!='rolled_back':
            raise ConfigError('Catch transition already exists; use resume or rollback')
        transition_id=str(uuid.UUID(previous['id']))
        history=target['prefix']/'catch-pilot-history';history.mkdir(mode=0o700,exist_ok=True);protected(history)
        archived=history/(transition_id+'.json')
        old=_control(archived,private=True,optional=True)
        if old is not None and old!=previous:raise ConfigError('Catch transition history changed')
        _write(archived,previous,private=True)
        (target['prefix']/PLAN).unlink();(target['prefix']/NAME).unlink(missing_ok=True)

    root=target['root'];uid=parent['service_uid']
    with _identity(uid):
        _,host=load(root)
        current=_bytes(root,'config/host.json')
        if hashlib.sha256(current).hexdigest()!=value['expected_host_sha256']:
            raise ConfigError('Catch transition host configuration changed')
        alias=value['alias']
        # Validate the alias with the pure projector before constructing paths.
        if type(alias) is not str or alias not in ('room1','room2','room3','room4'):
            raise ConfigError('Catch transition requires an explicit numbered lobby alias')
        retained=_bytes(root,f'config/rooms/{alias}.json')
        names={'config/host.json',gameplay.RECORD,integrations.RECORD,'state/runtime-config.json',
               f'config/rooms/{alias}.json',*[f"config/rooms/{r['alias']}.json" for r in host['rooms']]}
        before={name:_encoded(_bytes(root,name)) for name in sorted(names)}
        if before['state/runtime-config.json'] is None:
            raise ConfigError('Catch addition requires an existing materialized controller; fresh installs select the preset')
        project(marker,host,_object(before[gameplay.RECORD]),_object(before[integrations.RECORD]),
                {} if retained is None else json.loads(retained),
                alias=alias,sr_min=value['sr_min'],sr_max=value['sr_max'],title=value['title'])
        from .catalog_store import resolved_catalogs
        catalog=resolved_catalogs(root)
        if catalog is None or 'catch' not in catalog['bundles']:
            raise ConfigError('Catch transition requires an already verified and activated Catch catalog')
        content=catalog['bundles']['catch'][1]['content_id']
        from .catalog_store import CatalogStore
        store=CatalogStore.open(root);reference=store.current()
        legacy_reference=store._reference({m:v for m,v in reference['modes'].items() if m!='catch'},
                                          serial=reference['serial'])
    job={'format':1,'id':str(uuid.uuid4()),**{k:marker[k] for k in ('installation_id','generation')},
         'parent_sha256':parent['bundle_sha256'],'alias':alias,'request':value,
         'catalog_content_id':content,'rollback_catalog_reference':legacy_reference,'phase':'prepared','before':before,'after':{}}
    _fence(target,job,'prepared')
    return job


def _apply(target,marker,parent,job):
    if job['phase']=='committed':return _summary(job)
    if job['phase'] not in ('prepared','applying','requires_review'):
        raise ConfigError('Catch transition cannot resume this phase')
    root=target['root'];uid=parent['service_uid'];alias=job['alias'];before=job['before']
    host=_object(before['config/host.json']);request=job['request']
    result=project(marker,host,_object(before[gameplay.RECORD]),_object(before[integrations.RECORD]),
                   _object(before[f'config/rooms/{alias}.json']) or {},alias=alias,
                   sr_min=request['sr_min'],sr_max=request['sr_max'],title=request['title'])
    _fence(target,job,'applying')
    with _identity(uid):
        from .catalog_store import resolved_catalogs
        resolved=resolved_catalogs(root)
        if resolved['bundles']['catch'][1]['content_id']!=job['catalog_content_id']:
            raise ConfigError('Catch catalog changed during configuration transition')
        for name,value in (('config/host.json',result['host']),(gameplay.RECORD,result['gameplay']),
                           (integrations.RECORD,result['integrations'])):
            if value is None:continue
            current=_bytes(root,name)
            if current not in (_decoded(before[name]),_canonical(value)):
                raise ConfigError('Catch transition would overwrite an unrelated configuration edit')
            atomic_json(contained(root,name),value)
        from .runtime_config import render,RuntimeConfigBridge
        old=_object(before['state/runtime-config.json'])
        account=old['identity']['account_id']
        rendered=render(root,account_id=account)
        row=next(r for r in rendered['rooms'] if r['alias']==alias)
        native={**old,'identity':{k:rendered[k] for k in ('installation_id','generation','account_id','binding')},
                'bases':{**old['bases'],alias:row['settings']}}
        atomic_json(root/'state/runtime-config.json',native)
        atomic_json(root/f'config/rooms/{alias}.json',row['settings'])
        # Add only Catch's dedicated optional tables while the existing stopped
        # owner/process guards exclude concurrent room writers.
        from bot.catch_ppstats import CatchPPStatsStore
        from bot.scoped_ratings import ScopedRatingStore
        ScopedRatingStore(str(root/'state/controller-ratings.db'),ruleset_id=2)
        CatchPPStatsStore(str(root/'state/controller-ratings.db'),ruleset_id=2)
        # Native schema/default merge and real managed pool validation. This
        # starts no worker and never asks the provider to create a room.
        RuntimeConfigBridge(root,account_id=account).materialize()
        after={name:_encoded(_bytes(root,name)) for name in before}
    job['after']=after
    _fence(target,job,'committed')
    return _summary(job)


def _rollback(target,marker,parent,job):
    if job['phase']=='rolled_back':return _summary(job)
    root=target['root'];uid=parent['service_uid'];alias=job['alias']
    _fence(target,job,'rolling_back')
    journal_update=None
    with _identity(uid):
        # The outer stopped guard proved all owned rooms closed. Archive a
        # newly used Catch journal entry instead of resetting sibling ownership.
        path=root/'state/rolling/ownership.json'
        if path.exists():
            journal=read_json(path);entry=journal['rooms'].get(alias)
            if entry is not None:
                if (entry.get('room_id') or entry.get('ownership_unknown') or entry.get('operation')!='closed'
                        or entry.get('phase') not in ('stopped','parked')):
                    raise ConfigError('Catch rollback requires confirmed closed ownership')
                job['archived_catch_ownership']=entry
                journal['rooms'].pop(alias);journal_update=journal
        # Keep current preferences on sibling rooms. Original bases plus their
        # current native configs let the ordinary bridge retain player edits.
        retained={name:_bytes(root,name) for name in job['before']
                  if name.startswith('config/rooms/') and name!=f'config/rooms/{alias}.json'}
        previous=_object(job['before']['config/host.json'])
        current=read_json(root/'config/host.json')
        keep_host=copy.deepcopy(current)
        keep_host['rooms']=[r for r in keep_host['rooms'] if r['alias']!=alias]
        topology=lambda h:[{k:r[k] for k in ('alias','preset','catalog')} for r in h['rooms']]
        if topology(keep_host)!=topology(previous) or keep_host['account']!=previous['account']:
            raise ConfigError('Catch rollback has an unrelated topology/account change')
        keep={ 'config/host.json':keep_host }
        for name in (gameplay.RECORD,integrations.RECORD):
            if not (root/name).exists():continue
            value=read_json(root/name,maximum=1024**2);value['rooms'].pop(alias,None)
            if name==integrations.RECORD:
                for item in value['rooms'].values():
                    x=item['settings']
                    if 'discord_control_known_room_aliases' in x:
                        x['discord_control_known_room_aliases']=[a for a in x['discord_control_known_room_aliases'] if a!=alias]
                    if 'discord_control_channel_ids_by_room' in x:x['discord_control_channel_ids_by_room'].pop(alias,None)
                integrations.validate_record(value,marker,keep_host)
            else:gameplay.validate_record(value,marker,keep_host)
            keep[name]=value
        job['archived_catch_config']=_encoded(_bytes(root,f'config/rooms/{alias}.json'))
        current_native=read_json(root/'state/runtime-config.json')
        restored_native=_object(job['before']['state/runtime-config.json'])
        restored_native['bases']={a:b for a,b in current_native['bases'].items() if a!=alias}
        keep['state/runtime-config.json']=restored_native
    # Save archival evidence before changing/removing its active path.
    _fence(target,job,'rolling_back')
    with _identity(uid):
        if journal_update is not None:atomic_json(root/'state/rolling/ownership.json',journal_update)
        for name,encoded in job['before'].items():
            if name in retained:continue
            if name in keep:
                atomic_json(contained(root,name),keep[name]);continue
            path=contained(root,name);raw=_decoded(encoded)
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                # Exact original bytes are retained in the root private plan.
                atomic_json(path,json.loads(raw))
        # Restore authoritative schema binding, then merge retained native edits
        # through ordinary validation during the next normal start.
        load(root)
    # The ordinary catalog rollback retains all immutable versions and scoped
    # policy/history; an older runtime receives a legacy-only complete ref.
    from .catalog_store import CatalogStore
    store=CatalogStore.open_pilot_administrative(target['prefix'].name)
    store.rollback(job['rollback_catalog_reference'])
    _fence(target,job,'rolled_back')
    return _summary(job)


def command(instance,action,*,stream=None):
    if action not in ('prepare','resume','rollback','status') or (stream is not None)!=(action=='prepare'):
        raise ConfigError('invalid Catch configuration operation')
    target,marker,parent=administrative_binding(instance)
    if action=='status':
        job=_control(target['prefix']/PLAN,private=True,optional=True)
        with _identity(parent['service_uid']):digest=hashlib.sha256(_bytes(target['root'],'config/host.json')).hexdigest()
        return {**(_summary(job) if job else {'ok':True,'phase':'absent'}),'host_config_sha256':digest}
    with file_lease(target['prefix']/'deployment-admission.lock'),file_lease(target['prefix']/'network-admin.lock'):
        for state in (pilot._service_state(instance),network._state(instance)):
            if (state['MainPID'] or state['ActiveState'] not in ('inactive','failed')
                    or state['UnitFileState'] not in ('','disabled','not-found')):
                raise ConfigError('Catch configuration requires disabled/stopped bot and network services')
        require_no_service_processes(parent['service_uid'])
        with _stopped(target['root'],parent['service_uid']):
            require_no_service_processes(parent['service_uid'])
            if action=='prepare':
                from .network.contracts import decode
                job=_prepare(target,marker,parent,decode(stream.read(8193),maximum=8192))
            else:job=_load_job(target,marker,parent)
            try:
                return _rollback(target,marker,parent,job) if action=='rollback' else _apply(target,marker,parent,job)
            except BaseException:
                _fence(target,job,'requires_review')
                raise
