"""Evidence-bound per-room drain using the existing operator maintenance mailbox.

Call ``request`` from controller-update admission while holding
``deployment-admission.lock`` and after the update, configuration, and booking
checks. ``proof`` is read-only and only returns aliases whose exact maintenance
request closed their originally owned room; managed desired state and
reservations remain unchanged until the existing stopped-host reconciler runs.
"""
import copy
import math
import os
import re
import stat
import time
import uuid
from pathlib import Path

from .config import ConfigError
from .layout import atomic_json, file_lease, load, read_json
from .network.contracts import NetworkError, fingerprint, require
from .operator_maintenance import NAME as MAINTENANCE_NAME, RECEIPT as MAINTENANCE_RECEIPT
from .operator_maintenance import validate as validate_maintenance
from .provisioning import _control, _write

RECORD='controller_update_drain'
PAUSE='controller_update_drain_pause'
HISTORY='controller_update_drain_history'
GATE_BASELINE='controller_update_drain_gate_baseline'
RECORD_FIELDS={'format','job_id','request_id','installation_id','generation','parent_sha256',
               'mode','rooms','created_at','expires_at','configuration_sha256',
               'projection_sha256','eligibility_sha256','projection','initial','round'}
IDENTITY_FIELDS=('pid','generation','room_id','release')
TERMINAL_MAINTENANCE=('complete','cancelled','expired','requires_review')
REQUEST_TTL=86400
OBSERVATION_TTL=8


def _uuid(value):
    try:return type(value) is str and str(uuid.UUID(value))==value
    except (ValueError,TypeError,AttributeError):return False


def _round(job):
    value=job.get('controller_update_drain_round',0)
    require(type(value)is int and 0<=value<=2**31-1,'update_drain_round_requires_review')
    return value


def _request_id(job_id,round_number):
    # Preserve the original first-round UUID for jobs already in flight.
    tag=('poolrotater-empty-room-drain-v1' if round_number==0 else
         'poolrotater-empty-room-drain-v1-round-'+str(round_number))
    return str(uuid.uuid5(uuid.UUID(job_id),tag))


def _time_value(now):
    value=time.time() if now is None else now() if callable(now) else now
    require(type(value) in (int,float) and math.isfinite(value),'update_drain_clock_invalid')
    return value


def _clean_ownership(value):
    return (type(value) is dict and value.get('ownership_unknown') is False
            and not value.get('error') and not value.get('target')
            and value.get('creation_candidate') is None)


def _process_matches(job):
    process=job.get('process')
    if type(process) is not dict or type(process.get('pid')) is not int or process['pid']<=0:return False
    from .controller_update import _process
    try:return _process(process['pid'])==process
    except (ConfigError,OSError,ValueError,IndexError):return False


def _managed_snapshot(root,uid):
    from .ratings.operations import _identity
    from .managed_host.policy import state as managed_state
    from .diagnostic_logs.common import transaction
    with _identity(uid):
        marker,host=load(root)
        store=managed_state(root)
        snapshot=store.snapshot()
        from .managed_host.projection import read as read_projection
        projection=read_projection(root,marker)
        if projection is None:raise ConfigError('update_drain_requires_managed_projection')
        with transaction(store.path) as db:
            reservations={row['alias']:dict(row) for row in db.execute('SELECT * FROM reservations')}
    rooms={row['alias']:row for row in snapshot['rooms']}
    host_aliases={row['alias'] for row in host['rooms']}
    if set(rooms)!=host_aliases or set(projection['rooms'])!=set(rooms):
        raise ConfigError('update_drain_room_topology_changed')
    for alias,row in rooms.items():
        projected=projection['rooms'][alias]
        if (projected['desired']!=row['desired']
                or projected['settings'].get('preset')!=row['preset']
                or projected['revision']!=row['revision']):
            raise ConfigError('update_drain_managed_projection_changed')
    return marker,host,store,rooms,reservations,projection


def _configuration_sha(root):
    from .controller_update import _config_hashes
    return fingerprint(_config_hashes(root))


def _eligibility_sha(root,host,uid,projection):
    from .ratings.operations import _identity
    from .bookings.policy import enabled_aliases
    with _identity(uid):booking_aliases=sorted(enabled_aliases(root))
    policies={alias:{'visibility':projection['rooms'][alias]['settings'].get('visibility','public')}
              for alias in projection['rooms']}
    return fingerprint({'rooms':policies,'booking_aliases':booking_aliases})


def _pending_configuration_work(root,uid,drain,*,now=None):
    """Pin native accepted config requests and their pre-edit ownership proof."""
    from .ratings.operations import _identity
    from .managed_host.policy import state as managed_state
    from .managed_host.contracts import request as validate_request,digest
    from .managed_host.settings import validate as validate_settings,application
    from .diagnostic_logs.common import transaction
    from .reconciliation import _journal
    root=Path(root);at=time.time()if now is None else now
    with _identity(uid):
        marker,host=load(root);store=managed_state(root)
        rooms={row['alias']:row for row in store.snapshot()['rooms']}
        with transaction(store.path)as db:
            reservations={row['alias']:dict(row)for row in db.execute('SELECT * FROM reservations')}
        if not any(room.get('pending_operation')is not None for room in rooms.values()):
            return []
        status=_control(root/'state/rolling/status.json',uid)
        journal=_journal(_control(root/'state/rolling/ownership.json',uid,private=True),set(rooms))['rooms']
        generated=status.get('generated_at');status_rows=status.get('rooms')
        require(type(generated)in(int,float)and math.isfinite(generated)
                and 0<=at-generated<=OBSERVATION_TTL and type(status_rows)is dict
                and set(status_rows)==set(rooms),'update_drain_pause_status_stale')
        progress=_control(root/MAINTENANCE_RECEIPT,uid,private=True,optional=True)
        operations=[]
        for alias,room in rooms.items():
            operation_id=room.get('pending_operation')
            if operation_id is None:continue
            with transaction(store.path)as db:
                row=db.execute('SELECT digest,payload,phase FROM operations WHERE id=?',(operation_id,)).fetchone()
            if row is None:raise ConfigError('update_drain_managed_operation_requires_review')
            try:payload=__import__('json').loads(row['payload'])
            except (TypeError,ValueError):raise ConfigError('update_drain_managed_operation_requires_review')from None
            validate_request(payload)
            require(row['digest']==digest(payload)and payload['id']==operation_id
                    and payload['alias']==alias and payload['action']=='configure'
                    and all(payload.get(k)==store.binding[k]for k in store.binding)
                    and row['phase']in('received','validated','waiting_boundary'),
                    'update_drain_managed_operation_requires_review')
            base=drain['projection']['rooms'].get(alias)
            require(type(base)is dict and payload['expected_revision']==base['revision']
                    and payload['expected_room_generation']==room['room_generation'],
                    'update_drain_managed_operation_requires_review')
            validate_settings(payload['patch'],current=base['settings'])
            action=application(payload['patch']);identity=None;reservation=reservations.get(alias)
            if room['desired']=='stopped':
                require(room['room_id']is None and room['room_generation']is None
                        and reservation is None and payload['expected_room_generation']is None,
                        'update_drain_managed_operation_requires_review')
            else:
                already_closed=(alias in drain['rooms'] and type(progress)is dict
                    and progress.get('id')==drain['request_id']
                    and progress.get('rooms',{}).get(alias)=='closed')
                if already_closed:
                    prior=drain['initial'][alias];observed=status_rows[alias];owned=journal[alias]
                    closure=progress.get('closure_evidence',{}).get(alias,{})
                    require(type(closure)is dict and closure.get('room_id')==prior['identity']['room_id']
                            and closure.get('mode')=='empty' and closure.get('require_empty')is True
                            and room['desired']=='running' and room.get('revision')==prior['managed']['revision']
                            and room.get('room_id')==prior['managed']['room_id']
                            and room.get('room_generation')==prior['managed']['room_generation']
                            and room.get('pending_operation')==operation_id
                            and type(reservation)is dict
                            and {k:reservation.get(k)for k in('generation','room_id','phase','operation')}==prior['reservation']
                            and _clean_ownership(observed)and _clean_ownership(owned)
                            and observed.get('phase')=='parked'and owned.get('phase')=='parked'
                            and observed.get('operation')=='closed'and owned.get('operation')=='closed'
                            and observed.get('room_id')is None and owned.get('room_id')is None
                            and observed.get('generation')==prior['identity']['generation']
                            and owned.get('generation')==prior['identity']['generation']
                            and observed.get('release')==prior['identity']['release']
                            and owned.get('release')==prior['identity']['release'],
                            'update_drain_managed_operation_generation_closed')
                    identity=prior['identity']
                else:
                    observed=status_rows[alias];owned=journal[alias]
                    observed_status=observed.get('status');age=observed.get('heartbeat_age')
                    require(room['desired']=='running'and _clean_ownership(observed)and _clean_ownership(owned)
                            and observed.get('phase')=='running'and owned.get('phase')=='running'
                            and observed.get('operation')=='owned'and owned.get('operation')=='owned'
                            and observed.get('healthy')is True and observed.get('room_id')==room['room_id']
                            and owned.get('room_id')==room['room_id']
                            and type(age)in(int,float)and math.isfinite(age)and 0<=age<=OBSERVATION_TTL
                            and type(observed_status)is dict and observed_status.get('ready')is True
                            and observed_status.get('running')is True
                            and observed_status.get('room_id')==room['room_id']
                            and tuple(observed.get(k)for k in IDENTITY_FIELDS)==tuple(owned.get(k)for k in IDENTITY_FIELDS),
                            'update_drain_managed_operation_requires_review')
                    if alias in drain['rooms']:
                        require((progress is None or progress.get('id')==drain['request_id'])
                                and tuple(observed.get(k)for k in IDENTITY_FIELDS)==tuple(
                                    drain['initial'][alias]['identity'][k]for k in IDENTITY_FIELDS),
                                'update_drain_managed_operation_generation_closed')
                    require(type(reservation)is dict and reservation.get('phase')=='owned'
                            and reservation.get('room_id')==room['room_id']
                            and reservation.get('generation')==owned.get('generation'),
                            'update_drain_managed_operation_reservation_changed')
                    identity={k:owned.get(k)for k in IDENTITY_FIELDS}
            operations.append({'id':operation_id,'alias':alias,
                'payload_sha256':digest(payload),'expected_revision':payload['expected_revision'],
                'expected_room_generation':payload['expected_room_generation'],'application':action,
                'managed_before':{k:room.get(k)for k in('desired','revision','room_id','room_generation','preset')},
                'reservation_before':({k:reservation.get(k)for k in('generation','room_id','phase','operation')}
                    if reservation else None),'identity':identity})
    return operations


def managed_operations_pending(root,uid):
    """Cheap no-gate guard for any already accepted managed operation."""
    from .ratings.operations import _identity
    from .managed_host.policy import state as managed_state
    with _identity(uid):
        return any(row.get('pending_operation') is not None
                   for row in managed_state(root).snapshot()['rooms'])


def _paused_configuration_projection(root,uid,drain,pause,projection,managed,reservations,
                                     status_rows=None,journal=None,*,now=None):
    """Validate exact configure requests against settings and current ownership."""
    from .ratings.operations import _identity
    from .managed_host.policy import state as managed_state
    from .managed_host.contracts import request as validate_request,digest
    from .managed_host.settings import merge,validate as validate_settings,application
    from .managed_host.projection import read as read_projection
    from .diagnostic_logs.common import transaction
    import json
    operations=pause.get('operations')if type(pause)is dict else None
    require(type(operations)is list and 1<=len(operations)<=4,'update_drain_pause_receipt_requires_review')
    expected=copy.deepcopy(drain['projection']);at=time.time()if now is None else now
    status=_control(Path(root)/'state/rolling/status.json',uid);generated=status.get('generated_at')
    require(type(generated)in(int,float)and math.isfinite(generated)
            and 0<=at-generated<=OBSERVATION_TTL,'update_drain_pause_status_stale')
    if status_rows is None:status_rows=status.get('rooms')
    if journal is None:
        from .reconciliation import _journal
        journal=_journal(_control(Path(root)/'state/rolling/ownership.json',uid,private=True),
            set(managed))['rooms']
    with _identity(uid):
        store=managed_state(root);marker,_=load(root)
        require(read_projection(root,marker)==projection,'update_drain_pause_projection_changed')
        with transaction(store.path)as db:
            for item in operations:
                keys={'id','alias','payload_sha256','expected_revision','expected_room_generation',
                    'application','managed_before','reservation_before','identity'}
                require(type(item)is dict and set(item)==keys,'update_drain_pause_receipt_requires_review')
                row=db.execute('SELECT digest,payload,phase FROM operations WHERE id=?',(item['id'],)).fetchone()
                require(row is not None,'update_drain_pause_operation_missing')
                try:payload=json.loads(row['payload'])
                except (TypeError,ValueError):raise ConfigError('update_drain_pause_operation_invalid')from None
                validate_request(payload);alias=item['alias'];base=drain['projection']['rooms'].get(alias)
                require(row['digest']==digest(payload)==item['payload_sha256']
                        and payload['id']==item['id']and payload['alias']==alias
                        and payload['action']=='configure'and payload['expected_revision']==item['expected_revision']
                        and payload['expected_room_generation']==item['expected_room_generation']
                        and all(payload.get(k)==store.binding[k]for k in store.binding)
                        and type(base)is dict and application(payload['patch'])==item['application'],
                        'update_drain_pause_operation_changed')
                validate_settings(payload['patch'],current=base['settings'])
                phase=row['phase'];require(phase in('received','validated','waiting_boundary','applying',
                    'applied','refused','cancelled','expired'),'update_drain_pause_operation_uncertain')
                room=managed[alias];reservation=reservations.get(alias);before=item['managed_before']
                require(type(before)is dict and set(before)=={'desired','revision','room_id',
                        'room_generation','preset'} and before.get('desired')==base['desired']
                        and before.get('revision')==base['revision']
                        and before.get('preset')==base['settings'].get('preset'),
                        'update_drain_pause_managed_intent_changed')
                prior=drain.get('initial',{}).get(alias,{}).get('managed')
                if prior is not None:
                    require(before['room_id']==prior['room_id']
                            and before['room_generation']==prior['room_generation'],
                            'update_drain_pause_managed_intent_changed')
                elif before['desired']=='stopped':
                    require(before['room_id']is None and before['room_generation']is None,
                            'update_drain_pause_managed_intent_changed')
                else:
                    require(type(before['room_id'])is int and type(before['room_generation'])is str,
                            'update_drain_pause_managed_intent_changed')
                applied=phase in('applying','applied')
                updated=merge(base['settings'],payload['patch'])if applied else base['settings']
                updated=dict(updated);updated.pop('password',None)
                revision=base['revision']+(1 if applied else 0)
                require(room.get('desired')==before['desired']and room.get('revision')==revision
                        and room.get('preset')==updated.get('preset',base['settings'].get('preset'))
                        and projection['rooms'][alias]=={'revision':revision,'desired':base['desired'],'settings':updated},
                        'update_drain_pause_projection_changed')
                if phase in('received','validated','waiting_boundary','applying'):
                    require(room.get('pending_operation')==item['id'],'update_drain_pause_pending_operation_changed')
                else:require(room.get('pending_operation')is None,'update_drain_pause_operation_changed')
                if before['desired']=='stopped':
                    require(room.get('room_id')is None and room.get('room_generation')is None
                            and reservation is None and item['identity']is None,
                            'update_drain_pause_stopped_intent_changed')
                else:
                    require(type(item['identity'])is dict,
                            'update_drain_pause_identity_missing')
                    observed=status_rows.get(alias);owned=journal.get(alias)
                    receipt=_control(Path(root)/MAINTENANCE_RECEIPT,uid,private=True,optional=True)
                    closed_after_cancel=(alias in drain['rooms'] and type(receipt)is dict
                        and receipt.get('id')==drain['request_id']
                        and receipt.get('rooms',{}).get(alias)=='closed')
                    original=drain.get('initial',{}).get(alias,{}).get('identity',item['identity'])
                    closure_state=(closed_after_cancel and type(observed)is dict and type(owned)is dict
                        and _clean_ownership(observed)and _clean_ownership(owned)
                        and observed.get('phase')=='parked'and owned.get('phase')=='parked'
                        and observed.get('operation')=='closed'and owned.get('operation')=='closed'
                        and observed.get('room_id')is None and owned.get('room_id')is None
                        and observed.get('generation')==original['generation']
                        and owned.get('generation')==original['generation']
                        and observed.get('release')==original['release']
                        and owned.get('release')==original['release'])
                    stale_closed=(closure_state and phase in ('refused','cancelled','expired')
                        and room.get('room_id')is None and room.get('room_generation')is None
                        and reservation is None and room.get('revision')==base['revision']
                        and room.get('pending_operation')is None)
                    closure_pending=(closure_state and phase in ('received','validated','waiting_boundary')
                        and room.get('room_id')==before['room_id']
                        and room.get('room_generation')==before['room_generation']
                        and room.get('revision')==base['revision']
                        and room.get('pending_operation')==item['id']
                        and type(reservation)is dict
                        and {k:reservation.get(k)for k in ('generation','room_id','phase','operation')}
                            ==item['reservation_before'])
                    if stale_closed:
                        expected['rooms'][alias]={'revision':revision,'desired':base['desired'],'settings':updated}
                        continue
                    if closure_pending:
                        expected['rooms'][alias]={'revision':revision,'desired':base['desired'],'settings':updated}
                        continue
                    require(type(observed)is dict and type(owned)is dict and _clean_ownership(observed)
                            and _clean_ownership(owned)and observed.get('phase')=='running'
                            and owned.get('phase')=='running'and observed.get('operation')=='owned'
                            and owned.get('operation')=='owned'and observed.get('room_id')==room.get('room_id')
                            and owned.get('room_id')==room.get('room_id')
                            and observed.get('healthy')is True and observed.get('roster_known')is True
                            and type(observed.get('heartbeat_age'))in(int,float)
                            and math.isfinite(observed['heartbeat_age'])
                            and 0<=observed['heartbeat_age']+at-generated<=OBSERVATION_TTL
                            and type(observed.get('status'))is dict
                            and observed['status'].get('ready')is True
                            and observed['status'].get('running')is True
                            and observed['status'].get('room_id')==room.get('room_id')
                            and tuple(observed.get(k)for k in IDENTITY_FIELDS)==tuple(owned.get(k)for k in IDENTITY_FIELDS),
                            'update_drain_pause_worker_identity_changed')
                    identity_changed=tuple(owned.get(k)for k in IDENTITY_FIELDS)!=tuple(
                        item['identity'].get(k)for k in IDENTITY_FIELDS)
                    recreated=(identity_changed and phase=='applied'and item['application']=='recreate'
                        and owned.get('release')==item['identity'].get('release'))
                    require(not identity_changed or recreated,
                            'update_drain_pause_worker_identity_changed')
                    require(type(reservation)is dict and reservation.get('phase')=='owned'
                            and reservation.get('room_id')==room.get('room_id')
                            and reservation.get('generation')==owned.get('generation')
                            and (recreated or {k:reservation.get(k)for k in
                                ('generation','room_id','phase','operation')}==item['reservation_before']),
                            'update_drain_pause_reservation_changed')
                expected['rooms'][alias]={'revision':revision,'desired':base['desired'],'settings':updated}
    return expected


def _configuration_pause_pending(root,uid,pause):
    from .ratings.operations import _identity
    from .managed_host.policy import state as managed_state
    with _identity(uid):
        store=managed_state(root);pending=False
        for item in pause['operations']:
            phase=store.operation(item['id'])['phase']
            if phase=='uncertain':raise ConfigError('update_drain_pause_operation_uncertain')
            pending|=phase in ('received','validated','waiting_boundary','applying')
        return pending


def _collect(target,parent,job,*,now):
    """Read a whole-host safety snapshot and select public, running, clean slots."""
    root=Path(target['root']);uid=parent['service_uid']
    require(job.get('before')==parent and job.get('phase')=='waiting_empty'
            and _uuid(job.get('id')) and _process_matches(job),'update_drain_parent_or_process_changed')
    require(_configuration_sha(root)==fingerprint(job['configuration']),'update_drain_configuration_changed')
    marker,host,store,managed,reservations,projection=_managed_snapshot(root,uid)
    require(all(marker[k]==parent[k] for k in ('installation_id','generation')),'update_drain_binding_changed')
    aliases={row['alias'] for row in host['rooms']}
    require(set(job['rooms'])==aliases,'update_drain_room_topology_changed')
    # A pending native operation can become permanently blocked by the
    # maintenance gate below. Let the ordinary managed engine settle it first,
    # then collect a fresh snapshot and publish the gate.
    if any(row.get('pending_operation') is not None for row in managed.values()):
        from .bookings.policy import holds_updates
        from .ratings.operations import _identity
        with _identity(uid):
            active_booking=holds_updates(root)
        if active_booking:
            return [],{},fingerprint(projection),_eligibility_sha(root,host,uid,projection),projection
        return [],{},fingerprint(projection),_eligibility_sha(root,host,uid,projection),projection
    status=_control(root/'state/rolling/status.json',uid)
    generated=status.get('generated_at')
    require(type(generated) in (int,float) and math.isfinite(generated)
            and 0<=now-generated<=OBSERVATION_TTL
            and status.get('configuration_error') is None
            and set(status.get('rooms',{}))==aliases,'update_drain_status_stale_or_incomplete')
    from .reconciliation import _journal
    journal=_journal(_control(root/'state/rolling/ownership.json',uid,private=True),aliases)['rooms']
    require(set(journal)==aliases,'update_drain_journal_incomplete')
    from .bookings.policy import holds_updates, enabled_aliases
    from .bookings.update_guard import holds_alias
    from .ratings.operations import _identity
    with _identity(uid):
        active_booking=holds_updates(root)
        booking_aliases=enabled_aliases(root)
        held_aliases={alias for alias in aliases if holds_alias(root,alias)}
    eligibility_sha=_eligibility_sha(root,host,uid,projection)
    initial={};selected=[];blocked_slot=active_booking
    for alias in sorted(aliases):
        room=managed[alias];owned=journal[alias];observed=status['rooms'][alias]
        require(_clean_ownership(owned) and _clean_ownership(observed),
                'update_drain_ownership_requires_review')
        require(not observed.get('admission_locked') and not observed.get('creation_candidate')
                and not observed.get('target'),'update_drain_admission_requires_review')
        settings=projection['rooms'][alias]['settings']
        require(type(settings) is dict,'update_drain_managed_settings_invalid')
        desired=room['desired'];visibility=settings.get('visibility','public')
        reservation=reservations.get(alias)
        if desired=='stopped':
            require(room['room_id'] is None and room['room_generation'] is None and reservation is None
                    and owned.get('operation')=='closed' and owned.get('room_id') is None
                    and owned.get('phase') in ('parked','stopped') and owned.get('pid') is None
                    and observed.get('operation')=='closed' and observed.get('room_id') is None
                    and observed.get('phase') in ('parked','stopped') and observed.get('pid') is None,
                    'update_drain_stopped_slot_not_closed')
            continue
        require(desired=='running','update_drain_desired_state_invalid')
        # Private/booking-designated slots remain allocated and are not included.
        if visibility=='private' or alias in booking_aliases:
            blocked_slot=True
            continue
        row_status=observed.get('status')
        require(observed.get('phase')=='running' and observed.get('operation')=='owned'
                and observed.get('healthy') is True and observed.get('room_id') is not None
                and type(row_status) is dict and row_status.get('ready') is True
                and row_status.get('running') is True
                and row_status.get('room_id')==observed.get('room_id')
                and row_status.get('state') in ('IDLE','PLAYING','SCORING','STARTING')
                and type(row_status.get('players')) is int and row_status['players']>=0
                and observed.get('roster_known') is True
                and type(observed.get('heartbeat_age')) in (int,float)
                and math.isfinite(observed['heartbeat_age']) and 0<=observed['heartbeat_age']<=OBSERVATION_TTL
                and observed.get('room_id')==owned.get('room_id')
                and tuple(owned.get(k) for k in IDENTITY_FIELDS)==tuple(observed.get(k) for k in IDENTITY_FIELDS),
                'update_drain_worker_identity_requires_review')
        if alias in held_aliases:
            blocked_slot=True
            continue
        require(room['phase']=='running' and room['pending_operation'] is None
                and room['room_id']==observed['room_id'] and room['room_generation'] is not None
                and type(reservation) is dict and reservation.get('phase')=='owned'
                and reservation.get('room_id')==observed['room_id']
                and reservation.get('generation')==owned.get('generation'),
                'update_drain_managed_reservation_requires_review')
        selected.append(alias)
        initial[alias]={'identity':{k:owned.get(k) for k in IDENTITY_FIELDS},
            'managed':{k:room[k] for k in ('desired','revision','phase','room_id','room_generation',
                                            'pending_operation','preset','visibility')},
            'reservation':{k:reservation.get(k) for k in ('generation','room_id','phase','operation')},
            'closure_started_at':None}
    if blocked_slot:return [],{},fingerprint(projection),eligibility_sha,projection
    return selected,initial,fingerprint(projection),eligibility_sha,projection


def _validate_drain(value,parent,job):
    fields=set(value) if type(value)is dict else set()
    require(type(value) is dict and fields in (RECORD_FIELDS,RECORD_FIELDS-{'round'}) and type(value['format']) is int
            and value['format']==1 and value['job_id']==job['id']
            and value['request_id']==_request_id(job['id'],value.get('round',0))
            and ('round' not in value or type(value['round'])is int and 0<=value['round']<=2**31-1)
            and all(value[k]==parent[k] for k in ('installation_id','generation'))
            and value['parent_sha256']==parent['bundle_sha256'] and value['mode']=='empty'
            and type(value['rooms']) is list and value['rooms'] and len(value['rooms'])==len(set(value['rooms']))
            and type(value['initial']) is dict and set(value['initial'])==set(value['rooms'])
            and type(value['projection']) is dict and fingerprint(value['projection'])==value['projection_sha256'],
            'update_drain_request_binding_changed')
    for key in ('created_at','expires_at'):
        require(type(value[key]) in (int,float) and math.isfinite(value[key]),'update_drain_request_binding_changed')
    require(0<value['expires_at']-value['created_at']<=86400,'update_drain_request_binding_changed')
    from .managed_host.projection import validate_document
    projection=validate_document(value['projection'],{'format':1,
        'installation_id':parent['installation_id'],'generation':parent['generation']})
    require(set(projection['rooms'])==set(job['rooms']) and set(value['rooms'])<=set(projection['rooms']),
            'update_drain_request_binding_changed')
    return value


def _maintenance_paths(target,parent,job):
    path=Path(target['prefix'])/MAINTENANCE_NAME
    receipt=Path(target['root'])/MAINTENANCE_RECEIPT
    existing=_control(path,optional=True)
    progress=_control(receipt,parent['service_uid'],private=True,optional=True)
    if existing is not None:
        try:
            validate_maintenance(existing,parent)
        except ConfigError:
            require(_validate_pre_gate_terminal_pair(existing,progress,parent),
                    'update_drain_unrelated_maintenance_pending')
        drain=job.get(RECORD)
        previous_terminal=False
        pinned_terminal=False
        if progress is not None and progress.get('id')==existing.get('id'):
            _validate_maintenance_receipt(progress,parent,existing)
        elif progress is not None:
            # On a fresh deterministic round the single native receipt file
            # still contains the previous round's terminal proof until the
            # predecessor polls the new request. Reuse it only from our exact
            # immutable archive; never treat it as progress for this round.
            history_terminal=(progress.get('phase') in ('complete','cancelled','expired')
                    and _history_matches(target,parent,job,progress.get('id'),progress))
            pinned_terminal=_pre_gate_terminal_matches(target,parent,job,existing,progress)
            require(history_terminal or pinned_terminal,
                    'update_drain_unrelated_maintenance_pending')
            previous_terminal=(drain is not None
                and existing.get('id')==drain.get('request_id')
                and existing.get('phase') in ('requested','cancelled')
                and existing.get('mode')=='empty'
                and existing.get('rooms')==drain.get('rooms'))
            require(previous_terminal or pinned_terminal,'update_drain_unrelated_maintenance_pending')
            progress=None
        if (drain is None or existing['id']!=drain.get('request_id')
                or existing['mode']!='empty' or existing['rooms']!=drain.get('rooms')):
            require(previous_terminal or pinned_terminal or progress is not None and progress.get('id')==existing['id']
                    and progress.get('phase') in ('complete','cancelled','expired'),
                    'update_drain_unrelated_maintenance_pending')
    return path,receipt,existing,progress


def _history_file(target,job_id,round_number):
    prefix=Path(target['prefix']);base=prefix/'controller-update-drain-history'
    folder=base/job_id
    for path in (base,folder):
        if os.path.lexists(path):
            st=path.lstat()
            require(stat.S_ISDIR(st.st_mode) and st.st_uid==0
                    and stat.S_IMODE(st.st_mode)==0o700,
                    'update_drain_history_path_requires_review')
        else:
            path.mkdir(mode=0o700)
            os.chown(path,0,0);os.chmod(path,0o700)
    return folder/(str(round_number)+'.json')


def _history_record(target,parent,job,request_id):
    current_round=job.get('controller_update_drain_round',0)
    if type(current_round)is not int or current_round<1:return None
    expected_round=current_round-1
    if request_id!=_request_id(job['id'],expected_round):return None
    path=_history_file(target,job['id'],expected_round)
    if not os.path.lexists(path):return None
    value=_control(path,private=True)
    if (type(value)is not dict or set(value)!={'format','job_id','round','request_id','drain',
            'drain_sha256','terminal_receipt','terminal_receipt_sha256'}
            or type(value.get('format'))is not int or value['format']!=1
            or value.get('job_id')!=job['id'] or value.get('round')!=expected_round
            or value.get('request_id')!=request_id or type(value.get('drain'))is not dict
            or value['drain'].get('request_id')!=request_id
            or value.get('drain_sha256')!=fingerprint(value['drain'])
            or type(value.get('terminal_receipt'))is not dict
            or value.get('terminal_receipt_sha256')!=fingerprint(value['terminal_receipt'])):
        return None
    try:
        _validate_drain(value['drain'],parent,job)
    except (ConfigError,NetworkError,ValueError,TypeError,KeyError,AttributeError,IndexError):
        return None
    return value


def _terminal_receipt_refresh_matches(drain,archived,current,parent):
    """Prove a receipt changed only by a bounded monotonic timestamp refresh."""
    if type(drain)is not dict or type(archived)is not dict or type(current)is not dict:return False
    request={'format':1,'id':drain.get('request_id'),'installation_id':drain.get('installation_id'),
        'generation':drain.get('generation'),'parent_sha256':drain.get('parent_sha256'),
        'phase':'requested','mode':drain.get('mode'),'rooms':drain.get('rooms'),
        'created_at':drain.get('created_at'),'expires_at':drain.get('expires_at')}
    try:
        validate_maintenance(request,parent)
        _validate_maintenance_receipt(archived,parent,request)
        _validate_maintenance_receipt(current,parent,request)
    except (ConfigError,NetworkError,ValueError,TypeError,KeyError):
        return False
    if (archived.get('phase')not in ('complete','cancelled','expired')
            or current.get('phase')not in ('complete','cancelled','expired')
            or archived.get('failures') or current.get('failures')):
        return False
    old_at=archived.get('updated_at');new_at=current.get('updated_at')
    if (type(old_at)not in (int,float) or type(new_at)not in (int,float)
            or not math.isfinite(old_at) or not math.isfinite(new_at)
            or old_at<drain['created_at'] or new_at<old_at or new_at>time.time()):
        return False
    old={key:value for key,value in archived.items()if key!='updated_at'}
    new={key:value for key,value in current.items()if key!='updated_at'}
    return old==new


def _history_receipt_pin(target,parent,job,request_id,current):
    record=_history_record(target,parent,job,request_id)
    if record is None or not _terminal_receipt_refresh_matches(
            record['drain'],record['terminal_receipt'],current,parent):
        return None
    return record['terminal_receipt_sha256']


def _history_matches(target,parent,job,request_id,progress):
    return _history_receipt_pin(target,parent,job,request_id,progress) is not None


def _terminal_receipt_semantic_sha(receipt):
    return fingerprint({key:value for key,value in receipt.items() if key!='updated_at'})


def _round_zero_receipt_matches(baseline,progress):
    """Match a round-zero receipt to its gate pin, allowing timestamp refresh only."""
    legacy={'format','job_id','round','nonce','parent_sha256','mailbox_id',
            'mailbox_sha256','receipt_id','receipt_sha256'}
    current=set(baseline) if type(baseline)is dict else set()
    if current==legacy:
        return type(progress)is dict and baseline.get('receipt_sha256')==fingerprint(progress)
    extended=legacy|{'receipt_semantic_sha256','receipt_updated_at'}
    if current!=extended or type(progress)is not dict:return False
    original=baseline.get('receipt_updated_at');observed=progress.get('updated_at')
    if (type(original)not in (int,float) or not math.isfinite(original)
            or type(observed)not in (int,float) or not math.isfinite(observed)
            or original>time.time() or observed<original or observed>time.time()
            or type(baseline.get('receipt_sha256'))is not str
            or re.fullmatch('[a-f0-9]{64}',baseline['receipt_sha256'])is None
            or type(baseline.get('receipt_semantic_sha256'))is not str
            or re.fullmatch('[a-f0-9]{64}',baseline['receipt_semantic_sha256'])is None
            or _terminal_receipt_semantic_sha(progress)!=baseline.get('receipt_semantic_sha256')):
        return False
    if observed==original and fingerprint(progress)!=baseline.get('receipt_sha256'):
        return False
    return True


def _pre_gate_terminal_matches(target,parent,job,existing,progress):
    """Accept only the terminal receipt pinned before this round's gate.

    Round zero can start with a terminal mailbox from an older job, so there is
    no local round archive to consult. The gate baseline binds both old
    documents by digest before the locking gate. After the new request replaces
    the mailbox, it authorizes that exact old receipt only while the mailbox is
    the current round's request.
    """
    drain=job.get(RECORD);baseline=job.get(GATE_BASELINE)
    fields={'format','job_id','round','nonce','parent_sha256','mailbox_id',
            'mailbox_sha256','receipt_id','receipt_sha256'}
    if (type(drain)is not dict or type(baseline)is not dict
            or set(baseline)not in(fields,fields|{'receipt_semantic_sha256','receipt_updated_at'})
            or baseline.get('format')!=1 or baseline.get('job_id')!=job.get('id')
            or baseline.get('round')!=drain.get('round',0)
            or baseline.get('nonce')!=job.get('nonce')
            or baseline.get('parent_sha256')!=parent.get('bundle_sha256')
            or type(progress)is not dict or progress.get('id')!=baseline.get('receipt_id')
            or progress.get('phase') not in ('complete','cancelled','expired')
            or progress.get('failures')):
        return False
    if drain.get('round',0)>0:
        if set(baseline)!=fields:return False
        receipt_pin=_history_receipt_pin(target,parent,job,baseline['receipt_id'],progress)
        if receipt_pin is None or baseline.get('receipt_sha256')!=receipt_pin:return False
    elif not _round_zero_receipt_matches(baseline,progress):
        return False
    current_request=(type(existing)is dict and existing.get('id')==drain.get('request_id')
        and existing.get('mode')=='empty' and existing.get('rooms')==drain.get('rooms')
        and existing.get('phase') in ('requested','cancelled','expired'))
    old_mailbox=(existing is None and baseline.get('mailbox_id') is None
        or type(existing)is dict and existing.get('id')==baseline.get('mailbox_id')
        and baseline.get('mailbox_sha256')==fingerprint(existing))
    if not (current_request or old_mailbox):return False
    if old_mailbox and existing is not None:
        return (_validate_pre_gate_terminal_pair(existing,progress,parent)
                and (drain.get('round',0)==0 or _history_matches(
                    target,parent,job,baseline['receipt_id'],progress)))
    return True


def _pre_gate_baseline(target,parent,job,nonce):
    """Bind the pre-existing terminal maintenance documents before gate write."""
    mailbox=_control(Path(target['prefix'])/MAINTENANCE_NAME,optional=True)
    progress=_control(Path(target['root'])/MAINTENANCE_RECEIPT,parent['service_uid'],
                      private=True,optional=True)
    round_number=job.get('controller_update_drain_round',0)
    if type(round_number)is not int or not 0<=round_number<=2**31-1:return None
    if mailbox is None and progress is None:
        mailbox_id=receipt_id=mailbox_sha=receipt_sha=None
    elif mailbox is not None and progress is not None:
        try:
            try:validate_maintenance(mailbox,parent)
            except ConfigError:
                require(_validate_pre_gate_terminal_pair(mailbox,progress,parent),
                        'update_drain_pre_gate_maintenance_requires_review')
            else:_validate_maintenance_receipt(progress,parent,mailbox)
        except (ConfigError,NetworkError,ValueError,TypeError):return None
        if progress.get('phase')not in ('complete','cancelled','expired') or progress.get('failures'):
            return None
        if round_number==0 and progress.get('updated_at')>time.time():return None
        if round_number>0:
            previous_id=_request_id(job['id'],round_number-1)
            if (mailbox.get('id')!=previous_id or progress.get('id')!=previous_id
                or not _history_matches(target,parent,job,previous_id,progress)):
                return None
        mailbox_id=mailbox.get('id');receipt_id=progress.get('id')
        mailbox_sha=fingerprint(mailbox)
        receipt_sha=(_history_receipt_pin(target,parent,job,previous_id,progress)
                     if round_number>0 else fingerprint(progress))
        if receipt_sha is None:return None
    elif mailbox is None and progress is not None and round_number>0:
        previous_id=_request_id(job['id'],round_number-1)
        if (progress.get('id')!=previous_id or progress.get('phase')not in ('complete','cancelled','expired')
                or not _history_matches(target,parent,job,previous_id,progress)):
            return None
        mailbox_id=mailbox_sha=None;receipt_id=progress.get('id')
        receipt_sha=_history_receipt_pin(target,parent,job,previous_id,progress)
        if receipt_sha is None:return None
    else:
        return None
    result={'format':1,'job_id':job['id'],'round':round_number,'nonce':nonce,
        'parent_sha256':parent['bundle_sha256'],'mailbox_id':mailbox_id,
        'mailbox_sha256':mailbox_sha,'receipt_id':receipt_id,'receipt_sha256':receipt_sha}
    if round_number==0:
        result['receipt_semantic_sha256']=(_terminal_receipt_semantic_sha(progress)
                                          if progress is not None else None)
        result['receipt_updated_at']=progress.get('updated_at') if progress is not None else None
        pinned=job.get(GATE_BASELINE)
        if type(pinned)is dict and all(pinned.get(key)==result.get(key) for key in
                ('format','job_id','round','nonce','parent_sha256','mailbox_id','mailbox_sha256','receipt_id')):
            legacy={'format','job_id','round','nonce','parent_sha256','mailbox_id',
                    'mailbox_sha256','receipt_id','receipt_sha256'}
            extended=legacy|{'receipt_semantic_sha256','receipt_updated_at'}
            if set(pinned)==legacy:
                if (pinned.get('receipt_sha256')==result['receipt_sha256']
                        and (progress is None or _round_zero_receipt_matches(pinned,progress))):
                    return pinned
                return None
            if (set(pinned)==extended and
                    (progress is None and pinned.get('receipt_sha256') is None
                     and pinned.get('receipt_semantic_sha256') is None
                     and pinned.get('receipt_updated_at') is None
                     or progress is not None and _round_zero_receipt_matches(pinned,progress))):
                return pinned
            return None
    return result


def release_unpublished_gate(target,parent,job):
    """Release only a proven own locking gate left before request persistence."""
    if job.get(RECORD)is not None:return None
    from .controller_update import GATE,_configuration_unprepared,_gate
    gate=_control(Path(target['root'])/GATE,parent['service_uid'],private=True,optional=True)
    if gate is None:return None
    if (job.get('before')!=parent or job.get('phase')!='waiting_empty'
            or not _uuid(job.get('id')) or not _process_matches(job)
            or any(job.get(k)is not None for k in
                ('commit_started_at','commit_admitted_at','services_stopped_at','upgrade_transition'))):
        return False
    if not _configuration_unprepared(target,job):return False
    nonce=job.get('nonce')
    if type(nonce)is not str or re.fullmatch('[a-f0-9]{32}',nonce)is None:return False
    if (type(gate)is not dict or set(gate)!={'format','phase','nonce','expires_at','bridge_release'}
            or gate.get('format')!=1 or gate.get('nonce')!=nonce or gate.get('phase')!='locking'
            or gate.get('bridge_release')!=parent['release']
            or type(gate.get('expires_at'))not in(int,float) or not math.isfinite(gate['expires_at'])):
        return False
    baseline=job.get(GATE_BASELINE)
    baseline_fields={'format','job_id','round','nonce','parent_sha256',
            'mailbox_id','mailbox_sha256','receipt_id','receipt_sha256'}
    if (type(baseline)is not dict or set(baseline) not in
            (baseline_fields,baseline_fields|{'receipt_semantic_sha256','receipt_updated_at'})
            or baseline.get('format')!=1 or baseline.get('job_id')!=job['id']
            or baseline.get('nonce')!=nonce or baseline.get('parent_sha256')!=parent['bundle_sha256']
            or baseline.get('round')==0 and set(baseline) not in
                (baseline_fields,baseline_fields|{'receipt_semantic_sha256','receipt_updated_at'})
            or baseline.get('round')!=0 and set(baseline)!=baseline_fields):
        return False
    if _pre_gate_baseline(target,parent,job,nonce)!=baseline:return False
    return _gate(target,parent,job,None)


def _validate_maintenance_receipt(value,parent,request_value):
    base={'format','id','phase','rooms','mode','controller_pid','updated_at'}
    allowed=(base,base|{'closure_evidence'},base|{'failures'},base|{'closure_evidence','failures'})
    require(type(value)is dict and set(value) in allowed and type(value.get('format'))is int
            and value['format']==1 and value.get('id')==request_value['id']
            and value.get('mode')==request_value['mode']
            and type(value.get('controller_pid'))is int and value['controller_pid']>0
            and type(value.get('updated_at'))in(int,float) and math.isfinite(value['updated_at'])
            and type(value.get('rooms'))is dict and set(value['rooms'])==set(request_value['rooms']),
            'update_drain_receipt_requires_review')
    require(value['phase'] in ('draining','complete','cancelling','cancelled','expired','requires_review'),
            'update_drain_receipt_requires_review')
    require(all(type(phase)is str and phase in ('closed','waiting_for_fresh_worker',
            'waiting_for_worker_acknowledgement','request_delivered','requested','ready','waiting_empty',
            'waiting_for_notice_and_match_evidence','waiting_map','waiting_for_fresh_empty_roster',
            'waiting_for_fresh_roster','cancelling','cancelled','notice_uncertain','notice_failed',
            'room_changed','worker_control_refused','closure_requires_review')
            for phase in value['rooms'].values()),'update_drain_receipt_requires_review')
    if 'closure_evidence' in value:
        evidence=value['closure_evidence']
        require(type(evidence)is dict and set(evidence)<=set(request_value['rooms']),
                'update_drain_receipt_requires_review')
        for row in evidence.values():
            require(type(row)is dict and set(row)=={'room_id','mode','notice_confirmed',
                    'active_map_observed','require_empty','closure_started_at'}
                    and type(row['room_id'])is int and row['room_id']>0
                    and row['mode'] in ('empty','one-map')
                    and type(row['notice_confirmed'])is bool and type(row['active_map_observed'])is bool
                    and type(row['require_empty'])is bool
                    and type(row['closure_started_at'])in(int,float)
                    and math.isfinite(row['closure_started_at']),'update_drain_receipt_requires_review')
    if 'failures' in value:
        require(type(value['failures'])is dict and set(value['failures'])<=set(request_value['rooms']),
                'update_drain_receipt_requires_review')
    return value


def _validate_pre_gate_terminal_pair(mailbox,progress,parent):
    """Validate a same-host predecessor pair, including an older release hash."""
    fields={'format','id','installation_id','generation','parent_sha256','phase','mode',
            'rooms','created_at','expires_at'}
    if (type(mailbox)is not dict or set(mailbox)!=fields
            or mailbox.get('installation_id')!=parent.get('installation_id')
            or mailbox.get('generation')!=parent.get('generation')
            or type(mailbox.get('parent_sha256'))is not str
            or re.fullmatch('[a-f0-9]{64}',mailbox['parent_sha256']) is None
            or type(progress)is not dict):
        return False
    predecessor={**parent,'bundle_sha256':mailbox['parent_sha256']}
    try:
        validate_maintenance(mailbox,predecessor)
        _validate_maintenance_receipt(progress,parent,mailbox)
    except (ConfigError,NetworkError,ValueError,TypeError):
        return False
    return progress.get('phase') in ('complete','cancelled','expired') and not progress.get('failures')


def _rebase_configuration(target,parent,job,drain,progress,*,now):
    """Adopt a refreshed, valid room-file snapshot without changing drain authority."""
    root=Path(target['root']);uid=parent['service_uid']
    actual=_configuration_sha(root)
    require(actual==fingerprint(job['configuration']),'update_drain_configuration_not_refreshed')
    require(progress is None or progress.get('controller_pid')==job['process']['pid'],
            'update_drain_receipt_process_changed')
    marker,host,store,managed,reservations,projection=_managed_snapshot(root,uid)
    require(all(marker[k]==parent[k] for k in ('installation_id','generation'))
            and {row['alias'] for row in host['rooms']}==set(job['rooms'])
            and fingerprint(projection)==drain['projection_sha256']
            and _eligibility_sha(root,host,uid,projection)==drain['eligibility_sha256'],
            'update_drain_managed_or_booking_policy_changed')
    from .ratings.operations import _identity
    from .bookings.policy import holds_updates
    from .bookings.update_guard import holds_alias
    with _identity(uid):
        require(not holds_updates(root) and not any(holds_alias(root,a) for a in drain['rooms']),
                'update_drain_booking_active')
        status=_control(root/'state/rolling/status.json',uid)
        journal_control=_control(root/'state/rolling/ownership.json',uid,private=True)
    from .reconciliation import _journal
    journal=_journal(journal_control,set(job['rooms']))['rooms']
    at=now
    require(type(status.get('generated_at')) in (int,float) and math.isfinite(status['generated_at'])
            and 0<=at-status['generated_at']<=OBSERVATION_TTL
            and set(status.get('rooms',{}))==set(job['rooms']),
            'update_drain_status_stale_or_incomplete')
    for alias in drain['rooms']:
        prior=drain['initial'][alias];observed=status['rooms'][alias];owned=journal[alias]
        intent=managed[alias];reservation=reservations.get(alias)
        require(intent.get('desired')=='running'
                and intent.get('revision')==prior['managed']['revision']
                and intent.get('room_id')==prior['managed']['room_id']
                and intent.get('room_generation')==prior['managed']['room_generation']
                and intent.get('pending_operation') is None
                and intent.get('phase')=='running'
                and type(reservation)is dict
                and {k:reservation.get(k) for k in ('generation','room_id','phase','operation')}==prior['reservation'],
                'update_drain_managed_reservation_changed')
        if observed.get('phase')=='running' and observed.get('operation')=='owned':
            require(_clean_ownership(observed) and _clean_ownership(owned)
                    and tuple(observed.get(k) for k in IDENTITY_FIELDS)==tuple(prior['identity'][k] for k in IDENTITY_FIELDS)
                    and tuple(owned.get(k) for k in IDENTITY_FIELDS)==tuple(prior['identity'][k] for k in IDENTITY_FIELDS),
                    'update_drain_worker_identity_changed')
        else:
            # A partially completed request may already have parked this alias.
            # Only exact native receipt evidence permits preserving that closure.
            require(type(progress)is dict and progress.get('id')==drain['request_id']
                    and progress.get('rooms',{}).get(alias)=='closed'
                    and _clean_ownership(observed) and _clean_ownership(owned)
                    and observed.get('phase')=='parked' and owned.get('phase')=='parked'
                    and observed.get('operation')=='closed' and owned.get('operation')=='closed'
                    and observed.get('room_id') is None and owned.get('room_id') is None
                    and observed.get('pid') is None and owned.get('pid') is None
                    and observed.get('generation')==prior['identity']['generation']
                    and owned.get('generation')==prior['identity']['generation']
                    and observed.get('release')==prior['identity']['release']
                    and owned.get('release')==prior['identity']['release'],
                    'update_drain_partial_closure_requires_review')
            evidence=progress.get('closure_evidence',{}).get(alias,{})
            require(type(evidence)is dict and evidence.get('room_id')==prior['identity']['room_id']
                    and evidence.get('mode')=='empty' and evidence.get('require_empty') is True,
                    'update_drain_partial_closure_requires_review')
    drain['configuration_sha256']=actual
    return drain


def request(target,parent,job,save,*,now=time.time):
    """Create/replay the exact empty-room mailbox request; caller holds deployment admission.

    This helper never closes rooms. Existing controllers execute the mailbox.
    A matching request is reused, unrelated active maintenance is a hard stop,
    and the deployment gate blocks new booking installs while the drain runs.
    """
    at=_time_value(now)
    require(job.get('before')==parent and job.get('phase')=='waiting_empty'
            and _uuid(job.get('id')) and _process_matches(job),'update_drain_parent_or_process_changed')
    path,receipt_path,existing,progress=_maintenance_paths(target,parent,job)
    drain=job.get(RECORD)
    if drain is not None:
        _validate_drain(drain,parent,job)
        current=_configuration_sha(target['root'])
        if current!=drain['configuration_sha256']:
            _rebase_configuration(target,parent,job,drain,progress,now=at)
            save(**{RECORD:drain})
        drain_id=drain['request_id'];rooms=drain['rooms']
        if existing is not None and existing['id']!=drain_id:
            raise ConfigError('update_drain_unrelated_maintenance_pending')
    else:
        rooms,initial,projection_sha,eligibility_sha,projection=_collect(target,parent,job,now=at)
        if not rooms:return {'phase':'no_eligible_rooms','request_id':None,'rooms':[]}
        round_number=_round(job)
        drain_id=_request_id(job['id'],round_number)
        drain={'format':1,'job_id':job['id'],'request_id':drain_id,
            'installation_id':parent['installation_id'],'generation':parent['generation'],
            'parent_sha256':parent['bundle_sha256'],'mode':'empty','rooms':rooms,
            'created_at':at,'expires_at':at+REQUEST_TTL,
            'configuration_sha256':fingerprint(job['configuration']),
            'projection_sha256':projection_sha,'eligibility_sha256':eligibility_sha,
            'projection':projection,'initial':initial,'round':round_number}
        _validate_drain(drain,parent,job)
        nonce=job.get('nonce') or uuid.uuid5(uuid.UUID(job['id']),'controller-update-drain-gate-v1').hex
        if job.get('nonce')!=nonce:save(nonce=nonce);job['nonce']=nonce
        from .controller_update import GATE,_gate
        baseline=_pre_gate_baseline(target,parent,job,nonce)
        require(baseline is not None,'update_drain_pre_gate_maintenance_requires_review')
        current_gate=_control(Path(target['root'])/GATE,parent['service_uid'],private=True,optional=True)
        if current_gate is None:
            save(**{GATE_BASELINE:baseline});job[GATE_BASELINE]=baseline
        else:
            require(type(current_gate)is dict and current_gate.get('format')==1
                    and current_gate.get('phase')=='locking' and current_gate.get('nonce')==nonce
                    and current_gate.get('bridge_release')==parent['release']
                    and job.get(GATE_BASELINE)==baseline,
                    'update_drain_foreign_gate_pending')
        if not _gate(target,parent,job,'locking'):
            return {'phase':'waiting_for_bookings','request_id':None,'rooms':[]}
        try:
            checked,checked_initial,checked_projection,checked_eligibility,checked_document=_collect(target,parent,job,now=at)
            require(checked==rooms and checked_initial==initial and checked_projection==projection_sha
                    and checked_eligibility==eligibility_sha
                    and checked_document==projection
                    and _configuration_sha(target['root'])==drain['configuration_sha256'],
                    'update_drain_snapshot_changed')
        except Exception:
            _gate(target,parent,job,None)
            raise
        # Keep the pre-gate pair digest through mailbox replacement. If the
        # process dies after writing the request but before native receipt
        # refresh, replay can recognize only this exact stale terminal receipt.
        save(**{RECORD:drain,GATE_BASELINE:baseline});job[RECORD]=drain;job[GATE_BASELINE]=baseline
    if existing is not None:
        try:validate_maintenance(existing,parent)
        except ConfigError:
            require(_validate_pre_gate_terminal_pair(existing,progress,parent),
                    'update_drain_unrelated_maintenance_pending')
        if progress is not None:_validate_maintenance_receipt(progress,parent,existing)
        if existing.get('id')!=drain['request_id']:
            require(existing.get('phase') in ('requested','cancelled')
                    and (_history_matches(target,parent,job,existing['id'],progress)
                         or _pre_gate_terminal_matches(target,parent,job,existing,progress)),
                    'update_drain_unrelated_maintenance_pending')
            existing=None;progress=None
    if existing is not None:
        if (existing.get('id')==drain['request_id'] and existing.get('phase')=='cancelled'):
            return {'phase':'cancelled','request_id':drain['request_id'],'rooms':drain['rooms']}
        if (progress is not None and progress.get('id')==drain['request_id']
                and progress.get('phase') in ('cancelled','expired','requires_review')):
            return {'phase':progress['phase'],'request_id':drain['request_id'],'rooms':drain['rooms']}
        require(existing['id']==drain['request_id'] and existing['phase']=='requested'
                and existing['mode']=='empty' and existing['rooms']==drain['rooms'],
                'update_drain_request_changed')
        from .controller_update import _gate
        if not _gate(target,parent,job,'locking'):
            return {'phase':'waiting_for_bookings','request_id':drain['request_id'],'rooms':drain['rooms']}
        return {'phase':progress.get('phase') if progress and progress.get('id')==drain['request_id']
                else 'requested','request_id':drain['request_id'],'rooms':drain['rooms']}
    if progress is not None and progress.get('id')!=drain['request_id']:
        require(progress.get('phase') in ('complete','cancelled','expired')
                and _history_matches(target,parent,job,progress['id'],progress),
                'update_drain_unrelated_maintenance_pending')
        progress=None
    if progress is not None and progress.get('id')==drain['request_id']:
        expected_request={'id':drain['request_id'],'mode':'empty','rooms':drain['rooms']}
        _validate_maintenance_receipt(progress,parent,expected_request)
        if progress.get('phase') in ('cancelled','expired','requires_review'):
            return {'phase':progress['phase'],'request_id':drain['request_id'],'rooms':drain['rooms']}
        require(progress.get('phase') in ('complete','draining'), 'update_drain_receipt_requires_review')
        from .controller_update import _gate
        if not _gate(target,parent,job,'locking'):
            return {'phase':'waiting_for_bookings','request_id':drain['request_id'],'rooms':drain['rooms']}
        return {'phase':progress['phase'],'request_id':drain['request_id'],'rooms':drain['rooms']}
    from .controller_update import _gate
    if not _gate(target,parent,job,'locking'):
        return {'phase':'waiting_for_bookings','request_id':drain['request_id'],'rooms':drain['rooms']}
    value={'format':1,'id':drain['request_id'],'installation_id':parent['installation_id'],
        'generation':parent['generation'],'parent_sha256':parent['bundle_sha256'],
        'phase':'requested','mode':'empty','rooms':drain['rooms'],
        'created_at':drain['created_at'],'expires_at':drain['expires_at']}
    validate_maintenance(value,parent)
    _write(path,value)
    return {'phase':'requested','request_id':drain['request_id'],'rooms':drain['rooms']}


def cancel(target,parent,job):
    """Cancel future closures for this exact helper request; never clear intent."""
    drain=job.get(RECORD)
    if drain is None:return {'phase':'complete','request_id':None}
    _validate_drain(drain,parent,job)
    require(job.get('before')==parent and _uuid(job.get('id')) and _process_matches(job)
            and not any(job.get(k) is not None for k in
                ('commit_started_at','commit_admitted_at','services_stopped_at','upgrade_transition')),
            'update_drain_cancellation_requires_review')
    path,_,existing,progress=_maintenance_paths(target,parent,job)
    request_id=drain['request_id']
    if existing is None:
        if progress is None:
            from .controller_update import GATE,_gate
            gate=_control(Path(target['root'])/GATE,parent['service_uid'],private=True,optional=True)
            if gate is not None:
                require(type(gate)is dict and gate.get('nonce')==job.get('nonce')
                        and gate.get('phase')=='locking','update_drain_foreign_gate_pending')
                _gate(target,parent,job,None)
            return {'phase':'complete','request_id':request_id,'rooms':[]}
        require(progress.get('id')==request_id,'update_drain_unrelated_maintenance_pending')
        if progress.get('phase')=='requires_review':
            return {'phase':'requires_review','request_id':request_id}
        if progress.get('phase') in ('complete','cancelled','expired'):
            return {'phase':'complete','request_id':request_id}
        return {'phase':'waiting','request_id':request_id}
    validate_maintenance(existing,parent)
    require(existing.get('id')==request_id and existing.get('mode')=='empty'
            and existing.get('rooms')==drain['rooms'],'update_drain_unrelated_maintenance_pending')
    if existing['phase']=='requested':
        _write(path,{**existing,'phase':'cancelled'})
        return {'phase':'waiting','request_id':request_id}
    require(existing['phase']=='cancelled','update_drain_cancellation_requires_review')
    if progress is not None and progress.get('phase')=='requires_review':
        return {'phase':'requires_review','request_id':request_id}
    if progress is not None and progress.get('phase') in ('complete','cancelled','expired'):
        return {'phase':'complete','request_id':request_id}
    return {'phase':'waiting','request_id':request_id}


def _archive_configuration_pause(target,parent,job,*,now):
    drain=job.get(RECORD);pause=job.get(PAUSE)
    require(type(drain)is dict and type(pause)is dict and pause.get('request_id')==drain['request_id'],
            'update_drain_pause_binding_changed')
    require(_configuration_sha(target['root'])==fingerprint(job['configuration']),
            'update_drain_pause_configuration_not_refreshed')
    operations=pause.get('operations')
    require(type(operations)is list and 1<=len(operations)<=4,
            'update_drain_pause_receipt_requires_review')
    from .ratings.operations import _identity
    from .managed_host.policy import state as managed_state
    from .diagnostic_logs.common import transaction
    with _identity(parent['service_uid']):
        store=managed_state(target['root'])
        rooms={row['alias']:row for row in store.snapshot()['rooms']}
        with transaction(store.path) as db:
            reservations={row['alias']:dict(row) for row in db.execute('SELECT * FROM reservations')}
        marker,_=load(target['root'])
        from .managed_host.projection import read as read_projection
        projection=read_projection(target['root'],marker)
        for item in operations:
            operation=store.operation(item['id'])
            require(operation['alias']==item['alias']
                    and operation['phase'] in ('applied','refused','cancelled','expired'),
                    'update_drain_pause_operation_pending')
            require(rooms[item['alias']].get('pending_operation') is None,
                    'update_drain_pause_operation_pending')
    require(all(row.get('pending_operation') is None for row in rooms.values()),
            'update_drain_pause_other_operation_pending')
    _paused_configuration_projection(target['root'],parent['service_uid'],drain,pause,
                                     projection,rooms,reservations)
    path=Path(target['prefix'])/MAINTENANCE_NAME
    receipt_path=Path(target['root'])/MAINTENANCE_RECEIPT
    mailbox=_control(path,optional=True)
    receipt=_control(receipt_path,parent['service_uid'],private=True,optional=True)
    receipt_at=_time_value(now)
    if mailbox is not None:
        validate_maintenance(mailbox,parent)
        _validate_maintenance_receipt(receipt,parent,mailbox)
        require(mailbox.get('id')==drain['request_id']
                and mailbox.get('phase')=='cancelled'
                and receipt.get('id')==drain['request_id']
                and receipt.get('phase') in ('cancelled','complete','expired')
                and receipt.get('controller_pid')==job['process']['pid']
                and drain['created_at']<=receipt['updated_at']<=receipt_at,
                'update_drain_pause_terminal_receipt_missing')
        terminal_receipt=receipt
    else:
        require(receipt is None,'update_drain_pause_request_missing')
        terminal_receipt=None
    history=job.get(HISTORY,[])
    require(type(history)is list,'update_drain_history_requires_review')
    round_number=drain.get('round',0)
    archived={'format':1,'job_id':job['id'],'round':round_number,
        'request_id':drain['request_id'],'drain':copy.deepcopy(drain),
        'drain_sha256':fingerprint(drain),'terminal_receipt':terminal_receipt,
        'terminal_receipt_sha256':fingerprint(terminal_receipt) if terminal_receipt else None}
    archive_path=_history_file(target,job['id'],round_number)
    if os.path.lexists(archive_path):
        require(_control(archive_path,private=True)==archived,
                'update_drain_history_conflict')
    else:
        _write(archive_path,archived,private=True)
    entry={key:archived[key] for key in ('round','request_id','drain','drain_sha256',
        'terminal_receipt','terminal_receipt_sha256')}
    entry['operations']=copy.deepcopy(operations)
    prior=[row for row in history if type(row)is dict and row.get('round')!=round_number]
    return [*prior,entry][-16:]


def advance_configuration_pause(target,parent,job,save,*,now=None):
    """Cancel/restore one exact drain so accepted offslot config can settle."""
    at=_time_value(now);pause=job.get(PAUSE);drain=job.get(RECORD)
    require(type(pause)is dict and type(drain)is dict and pause.get('format')==1
            and pause.get('request_id')==drain['request_id']
            and pause.get('phase') in ('cancelling','restoring','waiting_configuration'),
            'update_drain_pause_binding_changed')
    _validate_drain(drain,parent,job)
    require(job.get('before')==parent and job.get('phase')=='waiting_empty'
            and _process_matches(job)
            and all(job.get(k)is None for k in ('commit_started_at','commit_admitted_at',
                'services_stopped_at','upgrade_transition')),'update_drain_pause_requires_review')
    from .controller_update import _configuration_unprepared
    require(_configuration_unprepared(target,job),'update_drain_pause_prepared_migration_requires_review')
    if pause['phase']=='waiting_configuration' and _configuration_pause_pending(
            target['root'],parent['service_uid'],pause):
        save(reason='waiting_for_native_configuration_operation')
        return {'phase':'waiting'}
    cancellation=cancel(target,parent,job)
    if cancellation['phase']=='waiting':
        save(reason='waiting_for_exact_drain_cancellation_receipt')
        return {'phase':'waiting'}
    if cancellation['phase']=='requires_review':
        return {'phase':'requires_review'}
    if cancellation.get('rooms')==[]:
        # The gate was released before any mailbox or native closure existed.
        pause={**pause,'phase':'waiting_configuration'};save(**{PAUSE:pause})
    else:
        pause={**pause,'phase':'restoring'};save(**{PAUSE:pause})
        result=restore(target,parent,job,now=now)
        if result['phase']=='waiting':
            from .controller_update import GATE
            gate=_control(Path(target['root'])/GATE,parent['service_uid'],private=True,optional=True)
            pause={**pause,'phase':'restoring' if gate is not None else 'waiting_configuration'}
            save(**{PAUSE:pause})
            save(reason='restoring_exactly_closed_siblings_after_config_pause')
            return {'phase':'waiting'}
        if result['phase']!='complete':
            return {'phase':'requires_review'}
        pause={**pause,'phase':'waiting_configuration'};save(**{PAUSE:pause})
    from .ratings.operations import _identity
    from .bookings.policy import holds_updates
    from .bookings.update_guard import holds_alias
    with _identity(parent['service_uid']):
        require(not holds_updates(target['root'])
                and not any(holds_alias(target['root'],alias) for alias in job['rooms']),
                'update_drain_booking_active')
    try:
        history=_archive_configuration_pause(target,parent,job,now=now)
    except NetworkError as error:
        if error.code=='update_drain_pause_configuration_not_refreshed':
            save(reason='waiting_for_stable_config_after_native_edit')
            return {'phase':'waiting'}
        if error.code in ('update_drain_pause_operation_pending',
                          'update_drain_pause_other_operation_pending'):
            save(reason='waiting_for_native_config_operation')
            return {'phase':'waiting'}
        if error.code=='update_drain_pause_status_stale':
            save(reason='waiting_for_fresh_configuration_pause_evidence')
            return {'phase':'waiting'}
        raise
    next_round=drain.get('round',0)+1
    require(next_round<=2**31-1,'update_drain_round_requires_review')
    save(**{HISTORY:history,'controller_update_drain_round':next_round,
            RECORD:None,PAUSE:None,'empty_since':None,'locked_since':None,
            'reason':'managed_configuration_settled_requalifying_drain'})
    job[RECORD]=None;job[PAUSE]=None;job['controller_update_drain_round']=next_round
    return {'phase':'requalified'}


def rearm(target,parent,job,save):
    """Re-establish this exact interrupted drain gate after fresh proof checks."""
    at=time.time();drain=job.get(RECORD)
    require(drain is not None and job.get('before')==parent and _uuid(job.get('id'))
            and job.get('phase') in ('waiting_empty','locking','committing','recovering','retrying')
            and _process_matches(job),'update_drain_rearm_requires_review')
    _validate_drain(drain,parent,job)
    from .updates.recovery import _approval
    _approval(target,job,recovering=job.get('phase') in ('committing','recovering','retrying'))
    path,_,existing,progress=_maintenance_paths(target,parent,job)
    require(existing is not None and existing.get('id')==drain['request_id']
            and existing.get('phase')=='requested' and existing.get('mode')=='empty'
            and existing.get('rooms')==drain['rooms'],'update_drain_request_requires_review')
    require(progress is not None and progress.get('id')==drain['request_id']
            and progress.get('phase') in ('draining','complete'),
            'update_drain_receipt_requires_review')
    if progress['phase']=='draining':
        require(0<=at-progress['updated_at']<=OBSERVATION_TTL,
                'update_drain_receipt_stale')
    old_hash=drain['configuration_sha256']
    _rebase_configuration(target,parent,job,drain,progress,now=at)
    if old_hash!=drain['configuration_sha256']:save(**{RECORD:drain})
    from .controller_update import _gate
    require(_gate(target,parent,job,'locking'),'update_drain_booking_active')
    return {'phase':'waiting','request_id':drain['request_id'],'rooms':drain['rooms']}


def _restore_operation(store,drain,alias,uid):
    from .ratings.operations import _identity
    from .diagnostic_logs.common import transaction
    operation_id=str(uuid.uuid5(uuid.UUID(drain['request_id']),'restore-open:'+alias))
    with _identity(uid):
        with transaction(store.path) as db:
            row=db.execute('SELECT payload,phase,error FROM operations WHERE id=?',(operation_id,)).fetchone()
    if row is None:return operation_id,None,None
    try:value=__import__('json').loads(row['payload'])
    except (TypeError,ValueError):raise ConfigError('update_drain_restore_operation_requires_review') from None
    require(type(value)is dict and value.get('id')==operation_id and value.get('alias')==alias
            and value.get('action')=='open' and value.get('actor')=='controller-update-drain-restore'
            and value.get('expected_revision')==drain['initial'][alias]['managed']['revision']
            and value.get('expected_room_generation') is None and value.get('patch')=={}
            and value.get('timing')=='when_empty'
            and all(value.get(k)==store.binding[k] for k in store.binding),
            'update_drain_restore_operation_requires_review')
    return operation_id,value,row['phase']


def restore(target,parent,job,*,now=None):
    """Resume only exact native OPEN operations after a terminal drain outcome.

    This never starts a service or calls a provider. The installed controller's
    normal managed operation engine applies deterministic OPEN requests.
    """
    at=_time_value(now);drain=job.get(RECORD)
    if drain is None:return {'phase':'complete','request_id':None,'rooms':{}}
    try:
        pause=job.get(PAUSE)
        pause_restore=(job.get('phase') in ('waiting_empty','cancelled','held') and type(pause)is dict
                       and pause.get('phase') in ('restoring','waiting_configuration')
                       and pause.get('request_id')==drain['request_id'])
        _validate_drain(drain,parent,job)
        require(job.get('before')==parent and (job.get('phase') in ('cancelled','held') or pause_restore)
                and _process_matches(job)
                and not any(job.get(k) is not None for k in
                    ('commit_started_at','commit_admitted_at','services_stopped_at','upgrade_transition')),
                'update_drain_restore_requires_review')
        require(_configuration_sha(target['root'])==fingerprint(job['configuration']),
                'update_drain_configuration_not_refreshed')
        path=Path(target['prefix'])/MAINTENANCE_NAME
        request_value=_control(path,optional=True)
        if request_value is not None:
            validate_maintenance(request_value,parent)
            require(request_value.get('id')==drain['request_id']
                    and request_value.get('mode')=='empty' and request_value.get('rooms')==drain['rooms'],
                    'update_drain_unrelated_maintenance_pending')
        receipt=_control(Path(target['root'])/MAINTENANCE_RECEIPT,parent['service_uid'],private=True)
        receipt_at=_time_value(now)
        expected={'id':drain['request_id'],'mode':'empty','rooms':drain['rooms']}
        _validate_maintenance_receipt(receipt,parent,expected)
        require(receipt.get('phase') in ('complete','cancelled','expired')
                and not receipt.get('failures') and receipt.get('controller_pid')==job['process']['pid']
                and drain['created_at']<=receipt['updated_at']<=receipt_at,
                'update_drain_restore_requires_terminal_receipt')
        outcomes=receipt['rooms']
        require(all(value in ('closed','cancelled') for value in outcomes.values())
                and set(outcomes)==set(drain['rooms'])
                and (receipt['phase']!='complete' or all(value=='closed' for value in outcomes.values())),
                'update_drain_restore_requires_terminal_receipt')

        uid=parent['service_uid'];root=Path(target['root'])
        from .ratings.operations import _identity
        from .managed_host.policy import state as managed_state
        from .diagnostic_logs.common import transaction
        from .bookings.policy import holds_updates
        from .bookings.update_guard import holds_alias
        with _identity(uid):
            marker,host=load(root)
            store=managed_state(root);managed={row['alias']:row for row in store.snapshot()['rooms']}
            with transaction(store.path) as db:
                reservations={row['alias']:dict(row) for row in db.execute('SELECT * FROM reservations')}
            from .managed_host.projection import read as read_projection
            projection=read_projection(root,marker)
            require(not holds_updates(root),'update_drain_booking_active')
            require(not any(holds_alias(root,a) for a in drain['rooms']),'update_drain_booking_active')
        require(all(marker[k]==parent[k] for k in ('installation_id','generation'))
                and {row['alias'] for row in host['rooms']}==set(job['rooms']),
                'update_drain_restore_binding_changed')
        allowed_projection=(_paused_configuration_projection(root,uid,drain,pause,projection,managed,reservations)
                            if pause_restore else drain['projection'])
        require(all(projection['rooms'].get(alias)==allowed_projection['rooms'].get(alias)
                    for alias in set(job['rooms'])-set(drain['rooms']))
                and (pause_restore or _eligibility_sha(root,host,uid,projection)==drain['eligibility_sha256']),
                'update_drain_restore_binding_changed')
        status=_control(root/'state/rolling/status.json',uid)
        status_at=_time_value(now)
        generated=status.get('generated_at');status_rows=status.get('rooms')
        require(type(generated) in (int,float) and math.isfinite(generated)
                and 0<=status_at-generated<=OBSERVATION_TTL and type(status_rows)is dict
                and set(status_rows)==set(job['rooms']),'update_drain_status_stale_or_incomplete')
        from .reconciliation import _journal
        journal=_journal(_control(root/'state/rolling/ownership.json',uid,private=True),
                         set(job['rooms']))['rooms']

        operations={};deferred_config_open=set();deferred_open_evidence=set();changed_projection={'format':projection['format'],
            'installation_id':projection['installation_id'],'generation':projection['generation'],'rooms':{}}
        for alias,baseline in drain['projection']['rooms'].items():
            prior=drain['initial'].get(alias)
            operation_id,operation,phase=_restore_operation(store,drain,alias,uid) if prior else (None,None,None)
            paused_item=next((row for row in (pause.get('operations',[])if pause_restore else [])
                              if row.get('alias')==alias),None)
            if paused_item:
                with _identity(uid):paused_phase=store.operation(paused_item['id'])['phase']
            else:paused_phase=None
            paused_pending=paused_phase in ('received','validated','waiting_boundary','applying')
            if alias in drain['rooms']:
                room_status=status_rows[alias];stored=journal[alias]
                closure=receipt.get('closure_evidence',{}).get(alias)
                if outcomes[alias]=='closed':
                    require(type(closure)is dict and closure.get('room_id')==prior['identity']['room_id']
                            and closure.get('mode')=='empty' and closure.get('require_empty') is True,
                            'update_drain_restore_closure_requires_review')
                if operation is None:
                    if outcomes[alias]=='closed':
                        require(_clean_ownership(room_status) and _clean_ownership(stored)
                                and room_status.get('phase')=='parked' and stored.get('phase')=='parked'
                                and room_status.get('operation')=='closed' and stored.get('operation')=='closed'
                                and room_status.get('room_id') is None and stored.get('room_id') is None
                                and room_status.get('pid') is None and stored.get('pid') is None
                                and room_status.get('generation')==prior['identity']['generation']
                                and stored.get('generation')==prior['identity']['generation']
                                and room_status.get('release')==prior['identity']['release']
                                and stored.get('release')==prior['identity']['release'],
                                'update_drain_restore_closure_requires_review')
                        intent=managed[alias];reservation=reservations.get(alias)
                        pause_pending=(paused_item is not None and paused_pending
                            and intent.get('pending_operation')==paused_item['id'])
                        still_reserved=(intent.get('desired')=='running'
                            and intent.get('revision')==prior['managed']['revision']
                            and intent.get('room_id')==prior['managed']['room_id']
                            and intent.get('room_generation')==prior['managed']['room_generation']
                            and (intent.get('pending_operation') is None and intent.get('phase')=='running'
                                 or pause_pending and intent.get('revision')==prior['managed']['revision'])
                            and type(reservation)is dict
                            and {k:reservation.get(k) for k in ('generation','room_id','phase','operation')}==prior['reservation'])
                        already_settled=(intent.get('desired')=='running'
                            and intent.get('revision')==prior['managed']['revision']
                            and intent.get('room_id') is None and intent.get('room_generation') is None
                            and intent.get('pending_operation') is None and intent.get('phase')=='stopped'
                            and reservation is None)
                        require(still_reserved or already_settled,
                                'update_drain_restore_managed_intent_changed')
                        if pause_pending:
                            deferred_config_open.add(alias)
                    else:
                        pause_waiting=(paused_item is not None and paused_pending
                            and managed[alias].get('pending_operation')==paused_item['id'])
                        pause_applied=(paused_item is not None and paused_phase=='applied')
                        require(_clean_ownership(room_status) and _clean_ownership(stored)
                                and room_status.get('phase')=='running' and stored.get('phase')=='running'
                                and room_status.get('operation')=='owned' and stored.get('operation')=='owned'
                                and tuple(room_status.get(k) for k in IDENTITY_FIELDS)==tuple(stored.get(k) for k in IDENTITY_FIELDS)
                                and (tuple(stored.get(k) for k in IDENTITY_FIELDS)==tuple(prior['identity'][k] for k in IDENTITY_FIELDS)
                                     or pause_applied and paused_item['application']=='recreate'
                                     and stored.get('release')==prior['identity']['release']),
                                'update_drain_restore_running_identity_changed')
                        intent=managed[alias];reservation=reservations.get(alias)
                        unchanged=(intent.get('revision')==prior['managed']['revision']
                            and intent.get('room_id')==prior['managed']['room_id']
                            and intent.get('room_generation')==prior['managed']['room_generation']
                            and reservation==reservations.get(alias) and reservation is not None
                            and {k:reservation.get(k) for k in ('generation','room_id','phase','operation')}==prior['reservation'])
                        applied_valid=(pause_applied and intent.get('revision')==prior['managed']['revision']+1
                            and intent.get('pending_operation') is None and intent.get('room_id')==room_status.get('room_id')
                            and type(reservation)is dict and reservation.get('phase')=='owned'
                            and reservation.get('room_id')==intent.get('room_id')
                            and reservation.get('generation')==stored.get('generation'))
                        require(intent.get('desired')=='running'
                                and (unchanged and (intent.get('pending_operation') is None or pause_waiting)
                                     or applied_valid),
                                'update_drain_restore_managed_intent_changed')
                    changed_projection['rooms'][alias]=allowed_projection['rooms'][alias]
                else:
                    require(operation.get('expected_revision')==prior['managed']['revision'],
                            'update_drain_restore_operation_requires_review')
                    intent=managed[alias]
                    if phase in ('received','validated','waiting_boundary'):
                        require(intent.get('desired')=='running'
                                and intent.get('revision')==prior['managed']['revision']
                                and intent.get('pending_operation')==operation_id
                                and intent.get('room_id') is None,
                                'update_drain_restore_operation_requires_review')
                        changed_projection['rooms'][alias]=baseline
                    elif phase in ('applying','applied'):
                        require(intent.get('desired')=='running'
                                and intent.get('revision')==prior['managed']['revision']+1
                                and (phase=='applied' or intent.get('pending_operation')==operation_id),
                                'update_drain_restore_operation_requires_review')
                        changed_projection['rooms'][alias]={**baseline,'revision':prior['managed']['revision']+1,
                            'desired':'running'}
                        if phase=='applied':
                            require(intent.get('pending_operation') is None and intent.get('phase')=='running'
                                    and intent.get('room_id') is not None
                                    and type(intent.get('room_generation'))is str,
                                    'update_drain_restore_operation_requires_review')
                            current_reservation=reservations.get(alias)
                            current_status=status_rows[alias];current_journal=journal[alias]
                            require(type(current_reservation)is dict and current_reservation.get('phase')=='owned'
                                    and current_reservation.get('room_id')==intent['room_id']
                                    and current_reservation.get('generation')==current_journal.get('generation')
                                    and _clean_ownership(current_journal)
                                    and current_journal.get('phase')=='running'
                                    and current_journal.get('operation')=='owned'
                                    and current_journal.get('room_id')==intent['room_id']
                                    and current_journal.get('release')==prior['identity']['release'],
                                    'update_drain_restore_open_authority_requires_review')
                            current_age=current_status.get('heartbeat_age')
                            current_worker_status=current_status.get('status')
                            status_identity=tuple(current_status.get(k) for k in IDENTITY_FIELDS)
                            journal_identity=tuple(current_journal.get(k) for k in IDENTITY_FIELDS)
                            fresh_ready=(_clean_ownership(current_status)
                                and current_status.get('phase')=='running'
                                and current_status.get('operation')=='owned'
                                and status_identity==journal_identity
                                and current_status.get('healthy') is True
                                and current_status.get('roster_known') is True
                                and type(current_age) in (int,float) and math.isfinite(current_age)
                                and 0<=current_age+status_at-generated<=OBSERVATION_TTL
                                and type(current_worker_status)is dict
                                and current_worker_status.get('ready') is True
                                and current_worker_status.get('running') is True
                                and current_worker_status.get('room_id')==intent['room_id'])
                            if not fresh_ready:
                                require(_clean_ownership(current_status),
                                        'update_drain_restore_open_evidence_requires_review')
                                # Before the new worker reports health, status
                                # may still show this exact request's cleanly
                                # closed pre-open identity. Durable managed
                                # intent, reservation, operation, and journal
                                # above already prove who owns the new room.
                                stale_closed=(current_status.get('phase')=='parked'
                                    and current_status.get('operation')=='closed'
                                    and current_status.get('room_id') is None
                                    and current_status.get('pid') is None
                                    and current_status.get('generation')==prior['identity']['generation']
                                    and current_status.get('release')==prior['identity']['release'])
                                same_new_identity=(current_status.get('phase')=='running'
                                    and current_status.get('operation')=='owned'
                                    and status_identity==journal_identity)
                                require(stale_closed or same_new_identity,
                                        'update_drain_restore_open_evidence_requires_review')
                                deferred_open_evidence.add(alias)
                    else:
                        raise ConfigError('update_drain_restore_operation_requires_review')
                if operation is None and outcomes[alias]=='closed':
                    operations[alias]=(operation_id,'paused_configuration_pending' if alias in deferred_config_open else None)
                elif operation is not None:
                    operations[alias]=(operation_id,
                        'startup_observation_pending' if alias in deferred_open_evidence else phase)
            else:
                expected_room=allowed_projection['rooms'].get(alias)
                require(projection['rooms'].get(alias)==expected_room,
                        'update_drain_restore_projection_changed')
                changed_projection['rooms'][alias]=expected_room
        require(fingerprint(changed_projection)==fingerprint(projection),
                'update_drain_restore_projection_changed')

        from .controller_update import GATE, _gate
        gate=_control(root/GATE,uid,private=True,optional=True)
        clear_gate=False
        if gate is not None:
            require(type(gate)is dict and gate.get('nonce')==job.get('nonce')
                    and gate.get('phase') in ('locking','committing'),
                    'update_drain_foreign_gate_pending')
            require(not job.get('commit_started_at'),'update_drain_restore_commit_already_started')
            clear_gate=True

        # Release only exact reservations proven closed. The existing journal
        # reconciler is used when every reservation is part of this closure set;
        # partial cancellation uses the per-room generation CAS.
        closed_pending=[alias for alias,(_operation_id,phase) in operations.items()
                        if outcomes[alias]=='closed' and phase in (None,'paused_configuration_pending')
                        and alias in reservations]
        if closed_pending and set(reservations)==set(closed_pending):
            with _identity(uid):store.reconcile_closed_journal({'format':1,'rooms':journal})
        else:
            for alias in closed_pending:
                prior=drain['initial'][alias]
                reservation=reservations[alias]
                require(reservation.get('generation')==prior['reservation']['generation']
                        and reservation.get('room_id')==prior['reservation']['room_id'],
                        'update_drain_restore_reservation_changed')
                with _identity(uid):
                    store.confirmed_closed(alias,reservation['generation'],reservation['room_id'])

        results={}
        for alias,(operation_id,existing_phase) in operations.items():
            if existing_phase=='applied':results[alias]='complete';continue
            if existing_phase=='startup_observation_pending':
                results[alias]='waiting_for_startup_observation';continue
            if existing_phase=='paused_configuration_pending':
                results[alias]='waiting_for_configuration_operation';continue
            if existing_phase in ('received','validated','waiting_boundary','applying'):
                results[alias]='waiting_for_native_open';continue
            if existing_phase is not None:raise ConfigError('update_drain_restore_operation_requires_review')
            with _identity(uid):
                receipt_value=store.local_lifecycle(alias,'open',operation_id=operation_id,
                    actor='controller-update-drain-restore')
            results[alias]='complete' if receipt_value['phase']=='applied' else 'waiting_for_native_open'
        if clear_gate:_gate(target,parent,job,None)
        phase='complete' if all(value=='complete' for value in results.values()) else 'waiting'
        return {'phase':phase,'request_id':drain['request_id'],'rooms':results,
                'error':'update_drain_restore_open_startup_pending' if deferred_open_evidence else None}
    except NetworkError as error:
        if error.code in ('update_drain_status_stale_or_incomplete','update_drain_pause_status_stale',
                          'update_drain_receipt_stale'):
            return {'phase':'waiting','request_id':drain.get('request_id'),'rooms':{}}
        return {'phase':'requires_review','request_id':drain.get('request_id'),'rooms':{}}
    except (ConfigError,OSError,ValueError,TypeError,KeyError,AttributeError,IndexError):
        return {'phase':'requires_review','request_id':drain.get('request_id') if type(drain)is dict else None,
                'rooms':{}}


def proof(target,parent,job,*,now=None):
    """Return positively closed aliases only for this exact request and baseline."""
    at=_time_value(now);drain=job.get(RECORD)
    if drain is None or not _uuid(job.get('id')):return None
    try:
        drain=_validate_drain(drain,parent,job)
        if (job.get('before')!=parent or job.get('phase') not in
                ('waiting_empty','locking','committing','recovering','retrying')
                or not _process_matches(job) or at<0 or not math.isfinite(at)
                or _configuration_sha(target['root'])!=drain['configuration_sha256']):return None
        from .controller_update import GATE
        gate=_control(Path(target['root'])/GATE,parent['service_uid'],private=True,optional=True)
        if (type(gate)is not dict or gate.get('format')!=1 or gate.get('nonce')!=job.get('nonce')
                or gate.get('phase') not in ('locking','committing')
                or type(gate.get('expires_at')) not in (int,float) or gate['expires_at']<=at):return None
        path=Path(target['prefix'])/MAINTENANCE_NAME
        request_value=_control(path,optional=True)
        validate_maintenance(request_value,parent)
        if (request_value.get('id')!=drain['request_id'] or request_value.get('parent_sha256')!=drain['parent_sha256']
                or request_value.get('phase')!='requested' or request_value.get('mode')!='empty'
                or request_value.get('rooms')!=drain['rooms']):return None
        progress=_control(Path(target['root'])/MAINTENANCE_RECEIPT,parent['service_uid'],private=True)
        _validate_maintenance_receipt(progress,parent,request_value)
        if (type(progress)is not dict or progress.get('format')!=1 or progress.get('id')!=drain['request_id']
                or progress.get('mode')!='empty' or progress.get('controller_pid')!=job['process']['pid']
                or progress.get('phase') not in ('complete','draining')
                or type(progress.get('updated_at')) not in (int,float)
                or not math.isfinite(progress['updated_at'])
                or not drain['created_at']<=progress['updated_at']<=drain['expires_at']
                or progress.get('failures')
                or progress['phase']=='draining' and not 0<=at-progress['updated_at']<=OBSERVATION_TTL):return None
        if type(progress.get('rooms')) is not dict or any(progress['rooms'].get(a)!='closed' for a in drain['rooms']):return None
        closures=progress.get('closure_evidence')
        if type(closures)is not dict or any(a not in closures for a in drain['rooms']):return None
        if progress['phase']=='complete' and any(progress['rooms'].get(a)!='closed' for a in request_value['rooms']):return None

        uid=parent['service_uid'];root=Path(target['root'])
        status=_control(root/'state/rolling/status.json',uid)
        generated=status.get('generated_at');rows=status.get('rooms')
        if (type(generated) not in (int,float) or not math.isfinite(generated)
                or not 0<=at-generated<=OBSERVATION_TTL or status.get('configuration_error') is not None
                or type(rows)is not dict or not set(drain['rooms'])<=set(rows)):return None
        from .reconciliation import _journal
        from .ratings.operations import _identity
        from .managed_host.policy import state as managed_state
        from .diagnostic_logs.common import transaction
        with _identity(uid):
            marker,host=load(root)
            from .managed_host.projection import read as read_projection
            projection=read_projection(root,marker)
            if (fingerprint(projection)!=drain['projection_sha256']
                    or {r['alias'] for r in host['rooms']}!=set(job['rooms'])):return None
            store=managed_state(root)
            managed={r['alias']:r for r in store.snapshot()['rooms']}
            with transaction(store.path) as db:
                reservations={r['alias']:dict(r) for r in db.execute('SELECT * FROM reservations')}
            from .bookings.policy import holds_updates
            from .bookings.update_guard import holds_alias
            booking_hold=holds_updates(root)
            held_aliases={alias for alias in drain['rooms'] if holds_alias(root,alias)}
        # _eligibility_sha owns its own service-UID scope for the booking
        # policy read. Keep it outside the preceding scope to avoid nested
        # euid changes (root -> service UID -> service UID).
        if _eligibility_sha(root,host,uid,projection)!=drain['eligibility_sha256']:return None
        journal=_journal(_control(root/'state/rolling/ownership.json',uid,private=True),set(job['rooms']))['rooms']
        result={}
        for alias in drain['rooms']:
            prior=drain['initial'][alias];old=prior['identity'];row=rows[alias];owned=journal[alias]
            closure=closures[alias]
            started=closure.get('closure_started_at') if type(closure)is dict else None
            if (closure.get('room_id')!=old['room_id'] or closure.get('mode')!='empty'
                    or closure.get('require_empty') is not True
                    or type(closure.get('notice_confirmed')) is not bool
                    or type(closure.get('active_map_observed')) is not bool
                    or type(started) not in (int,float) or not math.isfinite(started) or started>at):return None
            for evidence in (row,owned):
                if (not _clean_ownership(evidence) or evidence.get('phase')!='parked'
                        or evidence.get('operation')!='closed' or evidence.get('room_id') is not None
                        or evidence.get('pid') is not None or evidence.get('generation')!=old['generation']
                        or evidence.get('release')!=old['release']):return None
            intent=managed.get(alias);reservation=reservations.get(alias)
            if type(intent)is not dict:return None
            still_reserved=(intent.get('desired')=='running'
                and intent.get('revision')==prior['managed']['revision']
                and intent.get('room_id')==prior['managed']['room_id']
                and intent.get('room_generation')==prior['managed']['room_generation']
                and intent.get('pending_operation') is None and intent.get('phase')=='running'
                and intent.get('failure_code') is None
                and type(reservation)is dict
                and {k:reservation.get(k) for k in ('generation','room_id','phase','operation')}==prior['reservation']
                and reservation.get('generation')==old['generation']
                and reservation.get('room_id')==old['room_id'])
            already_settled=(intent.get('desired')=='running'
                and intent.get('revision')==prior['managed']['revision']
                and intent.get('room_id') is None and intent.get('room_generation') is None
                and intent.get('pending_operation') is None and intent.get('phase')=='stopped'
                and intent.get('failure_code') is None
                and reservation is None)
            if not (still_reserved or already_settled):return None
            if alias in held_aliases:return None
            result[alias]={'managed_closed':True,'room_id':None,
                'empty_seconds':max(0,at-started),'drain_request_id':drain['request_id'],
                'managed_reservation_pending_reconcile':still_reserved}
        if booking_hold:return None
        return result
    except (ConfigError,NetworkError,OSError,ValueError,TypeError,KeyError,AttributeError,IndexError):
        return None
