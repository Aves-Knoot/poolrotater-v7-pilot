"""Bounded local cleanup and closed diagnostics for authorized controller jobs.

These checks grant no provider authority and never terminate a process. The
native stopped-ownership and process guards are rerun on every observation.
"""
import time
from .config import ConfigError

CLEANUP_SECONDS = 20
RECOVERY_ATTEMPTS = 3
RECOVERY_SECONDS = 1800
STAGES = frozenset(('admission','approval','network_stop','controller_stop','cleanup',
    'upgrade','configuration','controller_start','readiness','network_restore',
    'completion','recovery_validation'))
CODES = frozenset(('update_owner_lock_busy','update_children_still_running',
    'update_cleanup_timeout','update_interrupted_commit','update_recovery_limit',
    'update_start_outcome_requires_review','update_guard_requires_review',
    'update_local_io_failed','update_service_operation_failed','update_maintenance_outcome_requires_review',
    'update_recovery_configuration_changed','update_recovery_registration_changed',
    'update_recovery_archive_changed','update_recovery_binding_changed',
    'update_recovery_operation_changed','update_upgrade_registration_requires_review',
    'update_foreign_upgrade_recovery_required','update_recovery_fresh_workers_required',
    'update_recovery_room_topology_changed','update_existing_controller_requires_review',
    'update_upgrade_pending_while_service_running','update_existing_network_agent_requires_review',
    'update_recovery_start_requires_review','update_recovery_committed_upgrade_required'))


def diagnostic(error):
    from .pilot_installation import CleanupPending
    if isinstance(error,CleanupPending) and error.code in CODES:return error.code
    from .network.contracts import NetworkError
    if isinstance(error,NetworkError) and error.code in CODES:return error.code
    from .service_diagnostics import failure_code
    code=failure_code(error)
    if code!='service_failed':return code
    if isinstance(error,OSError):return 'update_local_io_failed'
    if isinstance(error,ConfigError):
        if len(error.args)==1 and type(error.args[0])is str and error.args[0] in CODES:return error.args[0]
        return 'update_guard_requires_review'
    return 'update_service_operation_failed'


def checkpoint(save,stage,**values):
    if stage not in STAGES:raise ConfigError('update_guard_requires_review')
    save(stage=stage,stage_at=time.time(),**values)


def wait_stopped(root,uid,*,save=lambda **values:None,seconds=CLEANUP_SECONDS,
                 monotonic=time.monotonic,sleep=time.sleep):
    from .pilot_installation import _stopped,require_no_service_processes,CleanupPending
    deadline=monotonic()+seconds
    attempts=0
    while True:
        attempts+=1
        try:
            with _stopped(root,uid):require_no_service_processes(uid)
            save(cleanup_observations=attempts,cleanup_complete_at=time.time())
            return
        except CleanupPending as error:
            save(cleanup_observations=attempts,cleanup_wait_code=error.code)
            if monotonic()>=deadline:
                raise ConfigError('update_cleanup_timeout') from error
            sleep(min(.5,max(0,deadline-monotonic())))


def automatic_eligible(job):
    authority=job.get('automatic_recovery')
    return (type(authority)is dict and authority.get('format')==1
        and type(authority.get('attempts'))is int and 0<=authority['attempts']<RECOVERY_ATTEMPTS
        and type(job.get('commit_admitted_at'))in(int,float)
        and 0<=time.time()-job['commit_admitted_at']<=RECOVERY_SECONDS
        and job.get('phase')in('committing','recovering','retrying')
        and job.get('failure_code')in(None,'update_interrupted_commit','update_cleanup_timeout'))


def failed(save,job,error):
    code=diagnostic(error)
    retry=(code=='update_cleanup_timeout' and automatic_eligible({**job,'phase':'retrying','failure_code':code}))
    save(phase='retrying' if retry else 'held',
         reason='bounded_cleanup_recovery_pending' if retry else 'commit_requires_review_retain_all_state',
         failure_stage=job.get('stage','recovery_validation'),failure_code=code,
         failure_at=time.time(),next_action='observe_bounded_recovery' if retry else 'review_controller_update_with_operation_id')

