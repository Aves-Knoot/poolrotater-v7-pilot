"""Protected local maintenance mailbox; only the existing controller dispatches.

No account credentials, API adapters, shell commands or remote execution surface.
An explicit one-map request authorizes closure only after confirmed notice and a
real match boundary. Ordinary empty mode independently rechecks broker occupancy.
"""
import math
from pathlib import Path
import time
import uuid

from .config import ConfigError
from .layout import atomic_json, file_lease
from .pilot_installation import administrative_binding
from .provisioning import _control, _write

NAME = 'operator-maintenance.json'
RECEIPT = 'state/rolling/operator-maintenance.json'
TERMINAL = ('complete','cancelled','expired','requires_review')


def require_idle(target, parent):
    """Share deployment admission with worker/controller update commands."""
    job=_control(target['prefix']/NAME,optional=True)
    if job is None or job.get('parent_sha256')!=parent['bundle_sha256']: return
    receipt=_control(target['root']/RECEIPT,parent['service_uid'],private=True,optional=True)
    if not (receipt and receipt.get('id')==job.get('id') and receipt.get('phase') in ('complete','cancelled','expired')):
        raise ConfigError('maintenance_operation_pending')


def validate(value, parent):
    fields = {'format','id','installation_id','generation','parent_sha256','phase','mode','rooms','created_at','expires_at'}
    try:
        if type(value) is not dict or set(value) != fields or type(value['format']) is not int or value['format'] != 1: raise ValueError
        if str(uuid.UUID(value['id'])) != value['id']: raise ValueError
        if any(value[k] != parent[k] for k in ('installation_id','generation')): raise ValueError
        if value['parent_sha256'] != parent['bundle_sha256']: raise ValueError
        if value['phase'] not in ('requested','cancelled') or value['mode'] not in ('empty','one-map'): raise ValueError
        if (type(value['rooms']) is not list or not 1 <= len(value['rooms']) <= 50
                or any(type(a) is not str or not a.isascii() or not a.replace('-','').isalnum() or len(a)>32 for a in value['rooms'])
                or len(set(value['rooms'])) != len(value['rooms'])): raise ValueError
        for key in ('created_at','expires_at'):
            if type(value[key]) not in (int,float) or not math.isfinite(value[key]): raise ValueError
        if not 0 < value['expires_at']-value['created_at'] <= 86400: raise ValueError
    except (ValueError,TypeError,KeyError,AttributeError):
        raise ConfigError('maintenance_request_refused') from None
    return value


def command(instance, action, *, operation_id=None, mode=None, rooms=None):
    if action not in ('request','cancel','status'): raise ConfigError('maintenance_action_refused')
    target, marker, parent = administrative_binding(instance)
    with file_lease(target['prefix']/'deployment-admission.lock'), file_lease(target['prefix']/'maintenance-admin.lock'):
        target, marker, parent = administrative_binding(instance)
        path = target['prefix']/NAME
        job = _control(path,optional=True)
        receipt = _control(target['root']/RECEIPT,parent['service_uid'],private=True,optional=True)
        if action == 'status':
            # An upgrade may retain a terminal predecessor request as evidence.
            return {'ok':True,'request':job,'progress':receipt,'provider_dispatch':False}
        if operation_id is None: raise ConfigError('maintenance_operation_id_required')
        try:
            if str(uuid.UUID(operation_id)) != operation_id: raise ValueError
        except (ValueError,TypeError,AttributeError): raise ConfigError('maintenance_operation_id_required') from None
        if action == 'cancel':
            if not job or job['id'] != operation_id: raise ConfigError('maintenance_operation_changed')
            validate(job,parent)
            job={**job,'phase':'cancelled'};_write(path,job)
        else:
            from .pilot_operations import _service_state
            state=_service_state(instance)
            if state['ActiveState'] != 'active' or not state['MainPID']:
                raise ConfigError('maintenance_requires_running_controller')
            for name in ('controller-update.json','worker-deployment.json'):
                other=_control(target['prefix']/name,optional=True)
                if other and other.get('phase') not in ('complete','cancelled'):
                    # Completed worker jobs retain phase queued in root intent;
                    # their separate authenticated result is checked below.
                    if name=='worker-deployment.json':
                        from .updates.native import require_idle_worker_deployment
                        require_idle_worker_deployment(target,parent)
                    else: raise ConfigError('maintenance_deployment_pending')
            from .ratings.operations import _identity
            from .layout import load
            with _identity(parent['service_uid']): _, config=load(target['root'])
            aliases=list(rooms or [r['alias'] for r in config['rooms']])
            if not set(aliases) <= {r['alias'] for r in config['rooms']}:
                raise ConfigError('maintenance_select_existing_rooms')
            if job and job['id']==operation_id:
                validate(job,parent)
                if job['mode']!=mode or job['rooms']!=aliases or job['phase']!='requested':
                    raise ConfigError('maintenance_operation_changed')
                return {'ok':True,'request':job,'progress':receipt,'provider_dispatch':False}
            if job and (not receipt or receipt.get('id')!=job['id'] or receipt.get('phase') not in ('complete','cancelled','expired')):
                raise ConfigError('maintenance_operation_pending')
            now=time.time()
            job=validate({'format':1,'id':operation_id,'installation_id':marker['installation_id'],
                'generation':marker['generation'],'parent_sha256':parent['bundle_sha256'],
                'phase':'requested','mode':mode,'rooms':aliases,'created_at':now,'expires_at':now+86400},parent)
            _write(path,job)
        return {'ok':True,'request':job,'progress':receipt,'provider_dispatch':False,
                'next_action':'observe_maintenance_status; stop_and_upgrade_locally_when_complete'}


def poll(supervisor):
    parent=getattr(supervisor,'protected_parent',None)
    if parent is None: return
    job=_control(Path(parent['prefix'])/NAME,optional=True)
    if job is None: return
    # Old terminal maintenance is retained after upgrading, never re-executed.
    if job.get('parent_sha256') != parent['bundle_sha256']: return
    validate(job,parent)
    if not set(job['rooms']) <= supervisor.configs.keys(): raise ConfigError('maintenance_room_topology_changed')
    path=supervisor.runtime/RECEIPT
    receipt=getattr(supervisor,'_operator_maintenance_receipt',None)
    if receipt is None or receipt['id']!=job['id']:
        previous=_control(path,parent['service_uid'],private=True,optional=True)
        receipt={'format':1,'id':job['id'],'phase':'draining','rooms':{},'mode':job['mode'],
                 'controller_pid':__import__('os').getpid(),'updated_at':time.time()}
        if previous and previous.get('id')==job['id']:
            # A new process must not repeat an occupied one-map closure merely
            # from an old receipt. Cancellation is available without provider I/O.
            receipt={**previous,'phase':previous['phase'] if previous.get('phase') in TERMINAL else 'requires_review'}
        supervisor._operator_maintenance_receipt=receipt
        supervisor._operator_maintenance_sent={}
        atomic_json(path,receipt)
    cancelled=job['phase']=='cancelled' or time.time()>=job['expires_at']
    if receipt['phase'] in TERMINAL and not cancelled: return
    rows=supervisor.registry.snapshot()['rooms']
    for alias in job['rooms']:
        row=rows.get(alias,{})
        worker=supervisor.workers.get(alias)
        if row.get('phase')=='parked' and row.get('operation')=='closed' and not row.get('room_id') and not row.get('ownership_unknown'):
            receipt['rooms'][alias]='closed';continue
        if worker is None or not worker.fresh():
            receipt['rooms'][alias]='waiting_for_fresh_worker';continue
        observed=worker.status.get('operator_maintenance',{})
        wanted='cancelled' if cancelled else 'requested'
        if observed.get('id')!=job['id'] or cancelled and observed.get('phase')!='cancelled':
            from rolling.protocol import PeerClosed,UncertainResult,RemoteError
            sent=getattr(supervisor,'_operator_maintenance_sent',{})
            key=(job['id'],alias,wanted)
            if time.monotonic()-sent.get(key,-100)<5:continue
            sent[key]=time.monotonic();supervisor._operator_maintenance_sent=sent
            try:worker.peer.call('maintenance',{**job,'phase':wanted},timeout=1)
            except (PeerClosed,UncertainResult):
                # The local request is operation-ID idempotent; retrying it
                # never replays an uncertain provider notice or room mutation.
                receipt['rooms'][alias]='waiting_for_worker_acknowledgement'
                break
            except RemoteError:
                receipt['phase']='requires_review'
                receipt['rooms'][alias]='worker_control_refused'
                break
            receipt['rooms'][alias]='cancelling' if cancelled else 'request_delivered'
            break  # Bounded one command per tick, including fifty-room hosts.
        receipt['rooms'][alias]=observed.get('phase','waiting_worker')
        if observed.get('phase') in ('notice_uncertain','notice_failed','room_changed'):
            receipt['phase']='requires_review'
            from .service_diagnostics import FAILURE_CODES
            code=observed.get('failure_code')
            status=observed.get('failure_http_status')
            receipt.setdefault('failures',{})[alias]={
                'failure_code':code if type(code) is str and code in FAILURE_CODES else 'service_failed',
                **({'failure_http_status':status} if type(status) is int and 100<=status<=599 else {})}
            continue
        if cancelled or observed.get('phase')!='ready' or supervisor.rollout_alias: continue
        empty_close = job['mode']=='empty' or worker.idle_empty()
        if job['mode']=='one-map' and not empty_close and not (observed.get('notice_confirmed') is True
                and observed.get('active_map_observed') is True):
            receipt['rooms'][alias]='waiting_for_notice_and_match_evidence'
            continue
        # Parent RPC callback and independent roster share this same lock. A late
        # join is checked immediately before closure by Broker.close_room too.
        with supervisor.shared.lock:
            if not worker.fresh() or worker.status.get('state')!='IDLE' or not worker.broker.known:
                continue
            current=_control(Path(parent['prefix'])/NAME)
            validate(current,parent)
            if current['id']!=job['id']:
                receipt['phase']='requires_review';break
            if current['phase']=='cancelled' or time.time()>=current['expires_at']:
                cancelled=True;continue
            if empty_close and not worker.idle_empty(): continue
            receipt['rooms'][alias]='closing'
            receipt.setdefault('closure_evidence',{})[alias]={
                'room_id':worker.status['room_id'],'mode':job['mode'],
                'notice_confirmed':observed.get('notice_confirmed') is True,
                'active_map_observed':observed.get('active_map_observed') is True,
                'require_empty':empty_close,
                'closure_started_at':time.time()}
            atomic_json(path,receipt)
            from rolling.broker import InfrastructureCloseDeferred
            try:worker.broker.close_room(worker.status['room_id'],require_empty=empty_close)
            except InfrastructureCloseDeferred:
                receipt['rooms'][alias]='waiting_for_fresh_empty_roster' if job['mode']=='empty' else 'waiting_for_fresh_roster'
                continue
            except Exception as error:
                from .service_diagnostics import failure_details
                receipt['phase']='requires_review'
                receipt['rooms'][alias]='closure_requires_review'
                receipt.setdefault('failures',{})[alias]=failure_details(error)
                break  # Never retry an uncertain dispatched closure.
        supervisor.dispose(worker)
        supervisor.record(alias,phase='parked',pid=None,error=None)
        receipt['rooms'][alias]='closed'
    if cancelled:
        pending=any(receipt['rooms'].get(alias) not in ('closed','cancelled') for alias in job['rooms'])
        receipt['phase']='cancelling' if pending else ('cancelled' if job['phase']=='cancelled' else 'expired')
    elif all(receipt['rooms'].get(a)=='closed' for a in job['rooms']): receipt['phase']='complete'
    receipt['updated_at']=time.time()
    atomic_json(path,receipt)
