"""Typed signed catalog acquisition; native workers own empty-safe activation.

No request can supply an origin, path, trust key, executable, or lifecycle bypass.
Root only stages immutable selection catalogs. Ratings and operator policy stay
outside this write set. Uncertain acquisition/commit is held for local review.
"""
import os
from pathlib import Path
import time

from ..config import ConfigError
from ..layout import atomic_json,file_lease
from ..network.contracts import require
from .contracts import request,digest

def administrator():
    require(os.geteuid()==0,'catalog_administrator_required')

def receipt_path(prefix,value):
    request(value)
    require(value['action']=='catalog','invalid_managed_action')
    return Path(prefix)/'catalog-rollouts'/(value['id']+'.json')

def validate_receipt(value,receipt):
    require(type(receipt) is dict and set(receipt)=={'format','request_sha256','phase','content_id','updated_at'}
            and receipt['format']==1 and receipt['request_sha256']==digest(value)
            and receipt['phase'] in ('prepared','review_required')
            and receipt['content_id']==value['patch']['content_id'],'catalog_unavailable')
    return receipt

def ready(prefix,value):
    from ..provisioning import _control,protected
    path=receipt_path(prefix,value)
    if not os.path.lexists(path):return False
    result=validate_receipt(value,_control(protected(path)))
    if result['phase']!='prepared':raise ConfigError('catalog_unavailable')
    return True

def prepare_one(target,parent,value,*,store,repository,clock=time.time):
    """Runs under root admission locks; never dispatches provider mutations."""
    request(value)
    administrator();require(value['action']=='catalog','invalid_managed_action')
    require(value['installation_id']==parent['installation_id'] and value['generation']==parent['generation'],
            'host_identity_mismatch')
    require(value['expires_at']>clock(),'operation_expired')
    path=receipt_path(target['prefix'],value)
    if path.exists():
        from ..provisioning import _control,protected
        result=validate_receipt(value,_control(protected(path)))
        require(result['phase']=='prepared','catalog_unavailable')
        return result
    mode=value['patch']['mode'];content=value['patch']['content_id']
    path.parent.mkdir(mode=0o755,exist_ok=True)
    from ..provisioning import protected
    protected(path.parent)
    path.parent.chmod(0o755)
    result={'format':1,'request_sha256':digest(value),'phase':'review_required',
            'content_id':content,'updated_at':clock()}
    # Persist a conservative fence before downloads or catalog pointer writes.
    # A crash never grants an automatic repeated commit.
    atomic_json(path,result,mode=0o644)
    before=store.current()
    if before['modes'].get(mode)!=content:
        staged=store.stage(repository,[mode])
        require(staged['modes'].get(mode)==content,'catalog_unavailable')
        require(all(staged['modes'].get(k)==v for k,v in before['modes'].items() if k!=mode),'catalog_unavailable')
        require(value['expires_at']>clock(),'operation_expired')
        store.commit()
    require(store.current()['modes'].get(mode)==content,'catalog_unavailable')
    result.update(phase='prepared',updated_at=clock());atomic_json(path,result,mode=0o644)
    return result

def prepare_pending(target,parent,pending):
    from ..catalog_store import CatalogStore,mode_for
    from ..catalog_delivery import HttpsOrigin
    from ..provisioning import protected
    from ..catalog_publication import _write
    from ..ratings.operations import _identity
    from .catalog_source import ORIGIN,CA_PEM,CA_SHA256,BOOTSTRAP_SHA256
    from .policy import state
    selected=[v for v in pending if v['action']=='catalog']
    if not selected:return
    require(os.geteuid()==0,'catalog_administrator_required')
    prefix=target['prefix'];ca=prefix/'managed-catalog-ca.pem'
    import hashlib
    if ca.exists():
        protected(ca);require(hashlib.sha256(ca.read_bytes()).hexdigest()==CA_SHA256,'catalog_unavailable')
    else:_write(ca,CA_PEM.encode('ascii'),mode=0o444,replace=False)
    for value in selected:
        # Validate local intent and mode; stopped slots must stay stopped.
        with _identity(parent['service_uid']):
            saved=state(target['root']);row=saved.room(value['alias'],private=True)
            if saved.operation(value['id'])['phase'] not in ('received','validated','waiting_boundary'):continue
            require(row['revision']==value['expected_revision'] and row['room_generation']==value['expected_room_generation'],
                    'configuration_revision_changed')
            require(mode_for({'preset':row['config']['preset']})==value['patch']['mode'],'catalog_unavailable')
        store=CatalogStore.open_pilot_administrative(target['root'].name)
        require(store.trust.identity==BOOTSTRAP_SHA256,'catalog_unavailable')
        origin=HttpsOrigin(ORIGIN,prefix/'managed-catalog-delivery',ca_file=ca,ca_sha256=CA_SHA256,
                           timeout=30,deadline=300)
        with file_lease(prefix/'catalog-rollout.lock'):
            prepare_one(target,parent,value,store=store,repository=origin)
