"""Explicit stopped central migration with a private pre-M4 recovery image."""
import hashlib
import os

from ..layout import file_lease, read_json, atomic_json
from ..network.contracts import require, fields
from ..network.store import Store
from .migration import snapshot_ledger, _private
from .repository import prepare


def receipt(store):
    path=store.root/'ratings-migration.json'
    if not os.path.lexists(path):return None
    value=read_json(path);fields(value,('format','hub_id','phase','backup'))
    require(type(value['format']) is int and value['format']==1 and value['hub_id']==store.marker['hub_id']
            and value['phase'] in ('prepared','complete'),'ratings_migration_receipt_refused')
    if value['backup'] is not None:
        import re
        fields(value['backup'],('sha256','bytes'))
        require(type(value['backup']['sha256']) is str and re.fullmatch('[a-f0-9]{64}',value['backup']['sha256'])
                and type(value['backup']['bytes']) is int and 0<value['backup']['bytes']<=256*1024**2,'ratings_migration_receipt_refused')
    require(value['phase']!='complete' or value['backup'] is not None,'ratings_migration_receipt_refused')
    return value


def require_settled(store):
    record=receipt(store)
    require(record is None or record['phase']=='complete','ratings_migration_recovery_required',503)


def migrate(root):
    # Same lease is held by the central serve command until every request thread
    # has joined. A SQLite write lock alone is not a stopped-server proof.
    store=Store(root)
    with file_lease(store.root/'hub-server.lock'):
        value=receipt(store);path=store.root/'ratings-migration.json';backup=store.root/'ratings-before-m4.sqlite3'
        if value is None:
            require(not os.path.lexists(backup),'ratings_untracked_backup_refused')
            value={'format':1,'hub_id':store.marker['hub_id'],'phase':'prepared','backup':None}
            atomic_json(path,value)
        if value['phase']=='complete':
            prepare(store)
            return {'ok':True,'ratings_prepared':True,'already_prepared':True,'server_started':False}
        if not os.path.lexists(backup):
            def journal(metadata):
                value['backup']=metadata;atomic_json(path,value)
            snapshot_ledger(store.path,backup,before_publish=journal)
        _private(backup)
        with backup.open('rb') as handle:digest=hashlib.file_digest(handle,'sha256').hexdigest()
        require(value['backup']=={'sha256':digest,'bytes':backup.stat().st_size},'ratings_backup_identity_mismatch')
        prepare(store)
        value['phase']='complete';atomic_json(path,value)
        return {'ok':True,'ratings_prepared':True,'already_prepared':False,'server_started':False}
