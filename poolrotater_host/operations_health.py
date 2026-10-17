"""Credential-free projection shared by local support and operations reports."""
import math

STAGES=frozenset(('admission','approval','network_stop','controller_stop','cleanup','upgrade',
    'configuration','controller_start','readiness','network_restore','completion','recovery_validation'))


def project(service,job,reporting,*,now):
    """Observations only: this classification never authorizes a lifecycle action."""
    running=service.get('ActiveState')in('active','activating') and bool(service.get('MainPID'))
    result={'hosting':'hosting' if running else 'stopped','reporting':'not_observed',
            'update_stage':None,'action':'observe_host_health' if running else 'inspect_stopped_ownership_before_start'}
    if type(job)is dict and job.get('phase')not in('complete','cancelled'):
        phase=job.get('phase')
        if job.get('stage')in STAGES:result['update_stage']=job['stage']
        admitted=type(job.get('commit_admitted_at'))in(int,float) and math.isfinite(job['commit_admitted_at'])
        at=job.get('stage_at',job.get('commit_admitted_at'))
        fresh=type(at)in(int,float) and math.isfinite(at) and 0<=now-at<=1200
        if phase=='held':
            result.update(hosting='update_outage' if admitted and not running else 'update_requires_review',
                action='review_controller_update_with_operation_id')
        elif phase in('committing','recovering','retrying'):
            result.update(hosting='updating' if fresh else 'update_stalled',
                action='observe_bounded_controller_recovery' if fresh else 'inspect_update_process_and_transaction')
        elif phase in('waiting_empty','locking'):
            result.update(hosting='waiting_for_empty_rooms' if running else 'update_outage',
                action='wait_for_natural_empty_window' if running else 'inspect_update_process_and_transaction')
        else:result.update(hosting='update_requires_review',action='review_controller_update_with_operation_id')
    if type(reporting)is dict:
        at=reporting.get('at')
        if type(at)not in(int,float) or not math.isfinite(at) or not 0<=now-at<=120:
            result['reporting']='stalled'
        elif reporting.get('phase')=='disabled':result['reporting']='disabled_by_policy'
        elif reporting.get('capture_errors'):result['reporting']='capture_blocked'
        elif reporting.get('phase')in('retrying','not-enrolled','consent-pending'):result['reporting']='transfer_pending'
        elif reporting.get('phase')=='synchronized':result['reporting']='current'
        else:result['reporting']='requires_review'
    return result
