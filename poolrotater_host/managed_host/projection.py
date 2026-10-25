"""Secret-free desired defaults for native configuration materialization.

The authoritative journal is SQLite; this atomic projection lets guarded worker
reloads check their mode/config without opening the controller's command database.
Passwords and command actors/payloads never enter the projection.
"""
import copy
import os
from pathlib import Path
from ..layout import atomic_json, read_json, load
from ..config import ConfigError, PRESETS
from ..network.contracts import fields, require, integer
from .settings import validate, FIELDS,PROJECTION_JSON_LIMIT

NAME='config/managed-rooms.json'

def read(root,marker=None):
    root=Path(root);path=root/NAME
    if not os.path.lexists(path):return None
    if marker is None:marker,_=load(root)
    value=read_json(path,maximum=PROJECTION_JSON_LIMIT)
    return validate_document(value,marker)

def validate_document(value,marker):
    fields(value,('format','installation_id','generation','rooms'))
    require(type(value['format']) is int and value['format']==1 and
            all(value[k]==marker[k] for k in ('installation_id','generation')),'foreign_managed_room_projection')
    require(type(value['rooms']) is dict and 1<=len(value['rooms'])<=4,'invalid_managed_room_projection')
    from .contracts import alias
    for name,row in value['rooms'].items():
        alias(name);fields(row,('revision','desired','settings'))
        integer(row['revision'],0,2**53-1)
        require(row['desired'] in ('running','stopped') and type(row['settings']) is dict
                and 'password' not in row['settings'],'invalid_managed_room_projection')
        validate(row['settings'],preset=row['settings'].get('preset'),allow_redacted=True)
    return value

def document(state):
    """Build a validated secret-free projection without publishing it."""
    with __import__('poolrotater_host.diagnostic_logs.common',fromlist=['transaction']).transaction(state.path) as db:
        binding=state._get(db,'binding');rooms={}
        for row in db.execute('SELECT alias,revision,desired,config FROM rooms ORDER BY alias'):
            import json
            settings=json.loads(row['config']);settings.pop('password',None)
            rooms[row['alias']]={'revision':row['revision'],'desired':row['desired'],'settings':settings}
    if not rooms:return None
    return {'format':1,**{k:binding[k] for k in ('installation_id','generation')},'rooms':rooms}

def export(state):
    value=document(state)
    if value is None:return
    # No unnecessary rewrite: worker reload and native configuration caches use
    # metadata changes as their signals.
    path=state.root/NAME
    if path.exists() and read_json(path,maximum=PROJECTION_JSON_LIMIT)==value:return
    atomic_json(path,value)

def configuration(root,marker,original,*,shadow=None):
    shadow=read(root,marker) if shadow is None else validate_document(shadow,marker)
    if shadow is None:return copy.deepcopy(original)
    expected={r['alias'] for r in original['rooms']}
    require(set(shadow['rooms'])==expected,'managed_room_topology_requires_review')
    result=copy.deepcopy(original)
    for row in result['rooms']:
        desired=shadow['rooms'][row['alias']]['settings'];preset=desired['preset']
        row['preset']=preset
        if 'sr_min' in desired:row['sr_min']=desired['sr_min']
        if 'sr_max' in desired:row['sr_max']=desired['sr_max']
        if 'default_response_language' in desired:row['language']=desired['default_response_language']
        spec=PRESETS[preset]
        filename='loved.db' if preset=='standard-loved' else 'standard.db' if spec['ruleset']==0 else 'taiko.db' if spec['ruleset']==1 else 'catch.db' if spec['ruleset']==2 else 'mania_'+str(spec['keys'])+'k.db'
        row['catalog']='catalogs/'+filename
    return result

def settings_for(root,alias):
    shadow=read(root)
    return copy.deepcopy(shadow['rooms'][alias]['settings']) if shadow else {}
