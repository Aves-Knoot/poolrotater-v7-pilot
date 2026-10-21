"""Stopped acquisition of missing full signed modes, preserving existing IDs.

The service UID never collects maps or performs analysis. Root authenticates
the same published bundles through existing TLS/TUF delivery, publishes sealed
versions, then switches the desired reference only while workers are stopped.
"""
import hashlib
import os
from pathlib import Path

from ..config import ConfigError
from ..catalog_export import MODES


def _require(value):
    if not value:raise ConfigError('managed_catalog_preparation_requires_review')


def prepare_store(store,repository,*,owner_lease_held=False):
    """Internal seam; the installed caller proves its stopped owner lease."""
    _require(owner_lease_held is True)
    if store.fixture:
        from ..runtime_package import fixture_only
        fixture_only()
    else:_require(os.geteuid()==0)
    record=store.path/'.staging/managed-prewarm.json'
    def read():
        from ..layout import read_json
        if not os.path.lexists(record):return None
        store._check_path(record)
        return read_json(record,maximum=65536)
    value=read();binding={k:store.binding[k] for k in ('installation_id','generation','host_root','service_uid')}
    if value is not None:
        _require(type(value) is dict and set(value)=={'format','binding','bootstrap_sha256','before','after','phase'}
                 and type(value['format']) is int and value['format']==1 and value['binding']==binding
                 and value['bootstrap_sha256']==store.trust.identity and value['phase'] in ('staging','ready','complete'))
        store._validate_reference(value['before'])
        if value['after'] is not None:store._validate_reference(value['after'])
    pending=store._read('pending.json',optional=True)
    if pending is not None:
        _require(value is not None and value['after'] is not None
                 and pending=={'format':1,'before':value['before'],'after':value['after']})
        store.recover()
    before=store.current()
    if set(before['modes'])==set(MODES):
        if value is not None and value['phase']!='complete':
            _require(before==value['after'])
            value['phase']='complete';store._write(record,value)
        return {'ok':True,'reference':before,'all_modes_prepared':True,'existing_modes_replaced':False}
    if value is None or value['phase']=='complete':
        old_candidate=store._read('candidate.json',optional=True)
        if old_candidate is not None:
            store._journal(old_candidate)
            _require(old_candidate['after']==before)
        value={'format':1,'binding':binding,'bootstrap_sha256':store.trust.identity,
               'before':before,'after':None,'phase':'staging'}
        store._write(record,value)
    _require(before==value['before'])
    candidate=store._read('candidate.json',optional=True)
    if candidate is not None and candidate.get('after')==before:
        # CatalogStore retains the committed candidate as historical evidence.
        # Only that exact terminal record may be replaced by a fresh stage.
        candidate=None
    if value['phase']=='staging':
        if candidate is None:
            store.stage(repository,sorted(set(MODES)-set(before['modes'])))
            candidate=store._read('candidate.json')
        store._journal(candidate)
        after=candidate['after']
        _require(candidate['before']==before and set(after['modes'])==set(MODES)
                 and all(after['modes'][m]==identity for m,identity in before['modes'].items()))
        store._complete(after)
        value.update(phase='ready',after=after);store._write(record,value)
    _require(candidate=={'format':1,'before':value['before'],'after':value['after']})
    after=store.commit()
    _require(after==value['after'])
    value['phase']='complete';store._write(record,value)
    return {'ok':True,'reference':after,'all_modes_prepared':True,'existing_modes_replaced':False}


def prepare(target,current):
    """Fixed protected source; never accepts paths/URLs/credentials from the hub."""
    from ..pilot_installation import _admin,_stopped
    from ..catalog_store import CatalogStore
    from ..catalog_delivery import HttpsOrigin
    from ..provisioning import protected
    from ..catalog_publication import _write as write_code
    from .catalog_source import ORIGIN,CA_PEM,CA_SHA256,BOOTSTRAP_SHA256
    _admin()
    _require(Path(current['host_root'])==target['root'] and Path(current['prefix'])==target['prefix'])
    with _stopped(target['root'],current['service_uid']):
        store=CatalogStore.open_pilot_administrative(target['root'].name)
        _require(store.trust.identity==BOOTSTRAP_SHA256)
        path=protected(target['prefix'])/'managed-catalog-ca.pem'
        if os.path.lexists(path):
            protected(path)
            _require(path.is_file() and path.stat().st_nlink==1 and path.stat().st_size<=65536
                     and hashlib.sha256(path.read_bytes()).hexdigest()==CA_SHA256)
        else:write_code(path,CA_PEM.encode('ascii'),mode=0o444,replace=False)
        if not os.path.lexists(store.path/'pending.json'):
            reference=store.current()
            if set(reference['modes'])==set(MODES):
                return {'ok':True,'reference':reference,'all_modes_prepared':True,'existing_modes_replaced':False}
        repository=HttpsOrigin(ORIGIN,target['prefix']/'managed-catalog-delivery',
                               ca_file=path,ca_sha256=CA_SHA256,timeout=30,deadline=60)
        return prepare_store(store,repository,owner_lease_held=True)
