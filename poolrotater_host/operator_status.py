"""Bounded local administration view; never performs provider/network requests."""
import re
import time
from pathlib import Path

from .provisioning import _control


def _version(path):
    value=Path(path).name if type(path) is str else ''
    match=re.fullmatch(r'(v7\.[0-9]+\.[0-9]+)-[a-f0-9]{16}',value)
    return match[1] if match else None


def summary(target, parent, service, *, current_observation=None):
    from .operator_maintenance import NAME,RECEIPT
    native=_control(target['root']/'state/rolling/status.json',parent['service_uid'],optional=True)
    result={'installed_controller_version':_version(parent['release']),
            'running_controller_version':None,'workers':{},'maintenance':{'phase':'absent'},
            'queued_update':None,'pacing_counter_scope':'installation_lifetime; uncertainty_is_historical_until_correlated'}
    if type(native) is dict:
        at=native.get('generated_at')
        fresh=(type(at) in (int,float) and 0<=time.time()-at<=15
            and service.get('ActiveState')=='active' and bool(service.get('MainPID'))
            and current_observation is not None and current_observation.get('phase')=='running'
            and current_observation.get('version')==native.get('supervisor_version'))
        if fresh:result['running_controller_version']=native.get('supervisor_version')
        for alias,row in list(native.get('rooms',{}).items())[:50]:
            if not re.fullmatch('[a-zA-Z0-9-]{1,32}',alias) or type(row) is not dict:continue
            result['workers'][alias]={k:row.get(k) for k in
                ('version','target_version','phase','operation','ownership_unknown','room_id')}
            result['workers'][alias].update(observation_current=fresh,
                healthy=bool(fresh and row.get('healthy')),players=row.get('status',{}).get('players') if fresh else None)
    maintenance=_control(target['prefix']/NAME,optional=True)
    if maintenance and maintenance.get('parent_sha256')==parent['bundle_sha256']:
        progress=_control(target['root']/RECEIPT,parent['service_uid'],private=True,optional=True)
        phase=progress.get('phase') if progress and progress.get('id')==maintenance.get('id') else 'awaiting_controller'
        result['maintenance']={'id':maintenance.get('id'),'mode':maintenance.get('mode'),'phase':phase,
            'cancellation_requested':maintenance.get('phase')=='cancelled',
            'updated_at':progress.get('updated_at') if progress else None,
            'progress_current':bool(progress and progress.get('controller_pid')==service.get('MainPID') and service.get('ActiveState')=='active'),
            'rooms':progress.get('rooms',{}) if progress else {},
            'failures':progress.get('failures',{}) if progress else {}}
    controller=_control(target['prefix']/'controller-update.json',optional=True)
    if controller and controller.get('phase') not in ('complete','cancelled'):
        result['queued_update']={'kind':'controller','target_version':_version(controller.get('after',{}).get('release')),
            'phase':controller.get('phase'),'blocking_reason':controller.get('reason'),
            'all_rooms_empty_required':True}
    else:
        from .worker_deployment import read_job,result_path
        job=read_job(parent)
        if job:
            receipt=_control(result_path(target['root'],job['id']),parent['service_uid'],private=True,optional=True)
            if not receipt or receipt.get('phase') not in ('complete','cancelled'):
                from .worker_releases import receipt_path
                approval=_control(receipt_path(parent,job['candidate_sha256']),optional=True,maximum=1024*1024)
                candidate=approval.get('candidate',{}) if approval else {}
                result['queued_update']={'kind':'worker','phase':receipt.get('phase') if receipt else 'queued',
                    'target_version':_version(candidate.get('release')),
                    'target_sha256':job['candidate_sha256'],
                    'blocking_reason':receipt.get('reason') if receipt else 'waiting_for_controller_observation',
                    'rooms':job['rooms'],'all_rooms_empty_required':False}
    if result['maintenance']['phase']=='requires_review':action='review_maintenance_status; cancel_retained_request_before_new_maintenance'
    elif result['maintenance']['phase']=='complete':action='selected_rooms_closed; stop_service_then_follow_supported_upgrade_or_start_procedure'
    elif result['maintenance']['phase'] not in ('absent','cancelled','expired'):action='observe_maintenance_status; cancellation_available'
    elif result['queued_update']:action='wait_for_safe_update_window; inspect_update_status_for_blocking_reason'
    elif service.get('ActiveState')!='active':action='inspect_support_and_stopped_ownership_review_before_local_start'
    elif any(r.get('phase') in ('parked','recovery_required') for r in result['workers'].values()):action='review_affected_alias; preserve_healthy_siblings'
    else:action='hosting; observe_health_and_player_feedback'
    result['operator_next_action']=action
    return result
