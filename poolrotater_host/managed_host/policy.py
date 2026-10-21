"""Explicit versioned managed membership, distinct from resource ceilings."""
import os
from contextlib import contextmanager
from pathlib import Path
from ..config import ConfigError
from ..layout import read_json, load
from ..network.contracts import fields, require

NAME='state/managed-host-policy.json'

def seed(root,store,*,export_projection=True):
    """Initialize saved defaults once; never adopt temporary worker preferences."""
    from ..runtime_config import render
    from .settings import FIELDS
    from .projection import export
    marker,host=load(root)
    if store.snapshot()['rooms']:return
    original=render(Path(root))
    store.seed([{'alias':r['alias'],'settings':{k:v for k,v in r['settings'].items() if k in FIELDS}
                |{'preset':r['preset'],'visibility':r['settings'].get('managed_visibility','public')}} for r in original['rooms']])
    if export_projection:export(store)

def migrate(client,store):
    from .migration import migrate as apply
    return apply(client,store)

def startup_intent(root):
    """Stable desired startup set, independent of transient worker observations."""
    marker,host=load(root)
    if not enabled(root):return None
    store=state(root);seed(root,store)
    rows={r['alias']:r for r in store.snapshot()['rooms']};limits=store.limits()
    wanted=[r['alias'] for r in host['rooms'] if rows[r['alias']]['desired']=='running']
    return {'aliases':wanted[:limits['effective_limit']],
            'revisions':{a:rows[a]['revision'] for a in rows},
            'max_concurrent_rooms':limits['max_concurrent_rooms'],'provider_room_limit':limits['provider_room_limit'],
            'external_rooms':limits['external_rooms']}

def operator_intent(root,action):
    """Service-UID seam used under root lifecycle's deployment admission."""
    require(action in ('start','stop'),'invalid_managed_action')
    from .projection import export
    store=state(root);seed(root,store)
    if action=='stop':store.operator_stop()
    else:store.operator_start()
    export(store)

def prepare_runtime(root):
    """Finish local policy before constructing readers/workers, without hub I/O.

    The independent agent may be migrating concurrently. Its temporary lease is
    a bounded startup wait, not a reason to create independent readers and leave
    already-running workers on the wrong sharing mode after enrollment.
    """
    import time
    from ..layout import LeaseBusy
    from ..network.client import Client
    if not enabled(root):return
    client=Client(root);status=client.status()
    require(status.get('enrolled') is True and status.get('account_binding_matches') is True
            and status.get('pending_operation') is None,'managed_host_enrollment_required')
    deadline=time.monotonic()+30
    while True:
        try:
            store=state(root);migrate(client,store)
            require(store.snapshot()['policy_version']==1,'managed_policy_migration_requires_review')
            return
        except LeaseBusy:
            if time.monotonic()>=deadline:raise ConfigError('managed_policy_migration_busy') from None
            time.sleep(.1)

def profile(root):
    root=Path(root);path=root/NAME
    if not os.path.lexists(path):return None
    value=read_json(path);marker,_=load(root)
    fields(value,('format','policy_version','installation_id','generation','participation'))
    require(type(value['format']) is int and value['format']==1 and type(value['policy_version']) is int
            and value['policy_version']==1 and value['participation']=='managed','invalid_managed_policy')
    require(all(value[k]==marker[k] for k in ('installation_id','generation')),'foreign_managed_policy')
    return value

def enabled(root):return profile(root) is not None


def administrative_enabled(root,uid):
    """Root callers use the already registered service UID for owned state.

    Keep load()/checked_root() strict; privileged access is not a substitute for
    the installation's data ownership. Legacy hosts have no policy declaration.
    """
    if not os.path.lexists(Path(root)/NAME):return False
    from ..ratings.operations import _identity
    with _identity(uid):return enabled(root)

def require_enabled_settings(root,choices):
    if enabled(root) and any(value is not True for value in choices.values()):
        raise ConfigError('managed_membership_requires_all_features')

def prepare(root,marker,*,uid):
    """Root installer/upgrade seam. Records declared policy; no credential changes."""
    from ..provisioning import _control,_write
    root=Path(root);value={'format':1,'policy_version':1,
        **{k:marker[k] for k in ('installation_id','generation')},'participation':'managed'}
    old=_control(root/NAME,uid,private=True,optional=True)
    if old is not None and old!=value:raise ConfigError('managed_policy_migration_requires_review')
    if old is None:_write(root/NAME,value,owner=uid,private=True)
    return {'ok':True,'policy_version':1,'managed_policy_declared':True,'optional_sharing_controls':False,
            'credentials_changed':False,'rooms_started':False}

def prepare_data(root,marker,*,uid,owner_lease_held=False):
    """Stopped installer/upgrade preparation of every supported room namespace."""
    import hashlib
    from ..pilot_installation import require_no_service_processes
    from ..ratings.operations import _identity
    from ..ratings.migration import migrate_ledger,snapshot_ledger,_private
    from ..layout import atomic_json
    from bot.scoped_ratings import ScopedRatingStore
    from bot.catch_ppstats import CatchPPStatsStore
    # The installer imports from a root-private temporary payload. Load lazy
    # code dependencies before changing UID; never widen its permissions or
    # touch player data as root merely to make imports work.
    from bot import rating_outbox
    from ..ratings import epoch_review
    require(os.geteuid()==0 and owner_lease_held is True,'managed_data_requires_stopped_admission')
    require_no_service_processes(uid)
    root=Path(root)
    with _identity(uid):
        actual,_=load(root);require(actual==marker,'foreign_managed_policy')
        migrate_ledger(root)
        path=root/'state/managed-data-preparation.json';backup=root/'backups/ratings-before-managed-v1.sqlite'
        value=read_json(path) if os.path.lexists(path) else None
        binding={k:marker[k] for k in ('installation_id','generation')}
        if value is None:
            require(not os.path.lexists(backup),'managed_untracked_backup_refused')
            value={'format':1,**binding,'phase':'prepared','backup':None};atomic_json(path,value)
        fields(value,('format',*binding,'phase','backup'))
        require(type(value['format']) is int and value['format']==1 and all(value[k]==v for k,v in binding.items())
                and value['phase'] in ('prepared','complete'),'managed_data_preparation_requires_review')
        if not os.path.lexists(backup):
            def record(receipt):
                value['backup']=receipt;atomic_json(path,value)
            snapshot_ledger(root/'state/controller-ratings.db',backup,before_publish=record)
        _private(backup)
        with backup.open('rb') as stream:pin=hashlib.file_digest(stream,'sha256').hexdigest()
        require(value['backup']=={'sha256':pin,'bytes':backup.stat().st_size},'managed_backup_changed')
        ScopedRatingStore(str(root/'state/controller-ratings.db'),ruleset_id=2)
        CatchPPStatsStore(str(root/'state/controller-ratings.db'),ruleset_id=2)
        value['phase']='complete';atomic_json(path,value)
    return {'ok':True,'managed_data_prepared':True,'rooms_started':False}

def state(root):
    """Open the same private store for controller and independent network agent."""
    from ..network.client import Client
    from ..diagnostic_logs.common import private_database,transaction
    from ..layout import file_lease
    import json
    from .state import State
    root=Path(root);marker,_=load(root);client=Client(root);credential=client.credential()
    binding={k:marker[k] for k in ('installation_id','generation')}
    # Controller startup and the independent network agent open this same
    # store concurrently. This is a short local binding/schema transaction,
    # not the lifetime owner lease: wait briefly rather than treating ordinary
    # contention as a failed controller startup. A wedged holder still fences
    # admission, and file_lease retains its owner/type/no-link validation.
    with file_lease(root/'state/managed-binding.lock',timeout=10.0):
        path=private_database(root/'state','managed-host.sqlite3')
        with transaction(path) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            row=db.execute("SELECT value FROM meta WHERE key='binding'").fetchone() if 'meta' in tables else None
            old=json.loads(row[0]) if row else None
        if old is not None:
            require(all(old[k]==binding[k] for k in binding),'host_identity_mismatch',409)
        store=State(root,**(old or {**binding,'host_id':credential['host_id'] if credential else marker['installation_id']}))
        if credential is not None and credential['host_id']!=store.binding['host_id']:
            client.check_account(client.profile()['account_id'])
            require(client.pending(client.profile()) is None,'network_pending_requires_resume')
            store.rebind(credential['host_id'])
        return store


@contextmanager
def network_detachment(root):
    """Membership cleanup requires deliberate shutdown and clean ownership.

    This preserves local maintenance/re-enrollment without allowing an active
    managed host to discard central administration or mandatory participation.
    The owner lease fences controller startup throughout the bounded operation.
    """
    root=Path(root)
    if not enabled(root):
        yield
        return
    from ..layout import file_lease
    from ..reconciliation import _journal
    with file_lease(root/'state/owner.lock'):
        store=state(root);snapshot=store.snapshot();limits=store.limits()
        require(limits['capacity_used']==0 and all(r['desired']=='stopped'
            and r['room_id'] is None and r['room_generation'] is None
            and r['phase'] in ('stopped','unobserved') and r['pending_operation'] is None
            for r in snapshot['rooms']),'managed_network_detachment_requires_stopped_rooms')
        marker,host=load(root);aliases={r['alias'] for r in host['rooms']}
        path=root/'state/rolling/ownership.json'
        if os.path.lexists(path):
            journal=_journal(read_json(path),aliases)
            for row in journal['rooms'].values():
                require(row.get('phase') in ('parked','stopped') and row.get('operation')=='closed'
                    and row.get('ownership_unknown') is False
                    and not any(row.get(k) for k in ('room_id','pid','creation_candidate','error','target')),
                    'managed_network_detachment_requires_clean_ownership')
        else:
            require(all(r['phase']=='unobserved' for r in snapshot['rooms']),
                    'managed_network_detachment_requires_clean_ownership')
        yield
