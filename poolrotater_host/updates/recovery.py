"""Explicit root recovery of the existing native controller transaction.

This never stops a running room service. An old controller still running is
returned to fresh native empty-room admission. A started successor is observed,
not started again. Unknown ownership and unsupported rollback remain fenced.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

from ..config import ConfigError
from ..layout import file_lease
from ..provisioning import _control, _write, protected
from ..runtime_lifecycle import validate_runtime
from .contracts import identifier, require


def _configuration_matches(target,job,*,before_version=None,after_version=None):
    """Prove original config bytes, allowing only native rendered version text.

    A started successor materializes config/rooms/<alias>.json with its package
    version. Reconstructing the predecessor's canonical bytes must match the
    original queued SHA256 exactly; settings/staff/paths/keys cannot change.
    No new approval or trust in an arbitrary current config is introduced, and
    existing transactions need no schema migration or added snapshots.
    """
    from ..controller_update import _config_hashes
    actual=_config_hashes(target['root']);expected=job['configuration']
    if actual==expected:return True
    if (type(before_version)is not str or type(after_version)is not str
            or not before_version or not after_version or set(actual)!=set(expected)):return False
    managed={f'config/rooms/{alias}.json'for alias in job['rooms']}
    for name,sha in expected.items():
        if actual[name]==sha:continue
        if name not in managed:return False
        value=_control(target['root']/name,job['before']['service_uid'],private=True)
        if type(value)is not dict or value.get('version')not in(after_version,after_version.removeprefix('v')):
            return False
        matched=False
        for old in(before_version,before_version.removeprefix('v')):
            restored={**value,'version':old}
            raw=(json.dumps(restored,indent=2,allow_nan=False)+'\n').encode()
            if hashlib.sha256(raw).hexdigest()==sha:matched=True;break
        if not matched:return False
    return True


def _upgrade(target, job, current):
    value=_control(target['prefix']/'pilot-upgrade.json',optional=True,maximum=512*1024)
    if value is None:return None
    from ..pilot_upgrade import _record
    _record(value,target['prefix'],current['installation_id'])
    if value['before']==job['before'] and value['after']==job['after']:return value
    require(value['phase']in('committed','aborted') and current==value[
        'after'if value['phase']=='committed'else'before'],'update_foreign_upgrade_recovery_required')
    return None


def _archive(target,job):
    # Small root evidence is append-only; no current transaction is erased.
    folder=target['prefix']/'controller-recovery-history'
    folder.mkdir(mode=0o755,exist_ok=True);protected(folder)
    _write(folder/(job['id']+'-'+uuid.uuid4().hex+'.json'),job)


def _fresh_running(target,parent):
    from ..ratings.operations import _identity
    from ..layout import load
    with _identity(parent['service_uid']):_,host=load(target['root'])
    observation=_control(target['root']/'state/rolling/status.json',parent['service_uid'],optional=True)or{}
    now=time.time();generated=observation.get('generated_at')
    require(type(generated)in(int,float) and math.isfinite(generated) and 0<=now-generated<=8,
            'update_recovery_fresh_workers_required')
    rows=observation.get('rooms',{})
    require(set(rows)=={r['alias']for r in host['rooms']},'update_recovery_room_topology_changed')
    for row in rows.values():
        age=row.get('heartbeat_age');status=row.get('status',{})
        require(row.get('phase')=='running' and row.get('healthy')is True
            and row.get('operation')=='owned' and row.get('ownership_unknown')is False and not row.get('error')
            and type(age)in(int,float) and math.isfinite(age) and 0<=age+now-generated<=8
            and status.get('running')is True and status.get('ready')is True
            and not status.get('maintenance',{}).get('uncertain'), 'update_recovery_fresh_workers_required')
    from .native import require_registered_workers
    require_registered_workers(parent,rows)
    return rows


def wait_for_workers(instance,target,current,*,seconds=30):
    """Observe the same successor while its first independent status is published.

    Provider readiness precedes the first rolling-status write during cold full
    catalog startup. This only waits for fresh local evidence; it cannot start,
    stop, replace or adopt a process, and every other refusal remains immediate.
    """
    from .. import pilot_operations as pilot
    from ..controller_update import _process
    from .contracts import UpdateError
    service=pilot._service_state(instance)
    require(bool(service['MainPID'])and service['ActiveState']in('active','activating'),
        'update_existing_controller_requires_review')
    identity=_process(service['MainPID']);deadline=time.monotonic()+seconds
    while True:
        observed=pilot._service_state(instance)
        require(observed['MainPID']==service['MainPID'] and observed['ActiveState']in('active','activating')
            and _process(observed['MainPID'])==identity,'update_existing_controller_requires_review')
        try:_fresh_running(target,current)
        except UpdateError as error:
            if str(error)!='update_recovery_fresh_workers_required' or time.monotonic()>=deadline:raise
            time.sleep(min(.5,max(0,deadline-time.monotonic())))
        else:
            final=pilot._service_state(instance)
            require(final['MainPID']==service['MainPID']and final['ActiveState']in('active','activating')
                and _process(final['MainPID'])==identity,'update_existing_controller_requires_review')
            return


def _approval(target,job,*,recovering=False):
    from .approval import require_upgrade,require_approved
    before=validate_runtime(job['before'],target['prefix'])['manifest']
    after=validate_runtime(job['after'],target['prefix'])['manifest']
    check=require_approved if job.get('release_approval_required') else require_upgrade
    check(before,after,job['before'],job['after'],recovering=recovering)


def _restore_services(instance,target,current,job,*,save=None,automatic=False):
    from .. import pilot_operations as pilot
    from ..network import operations as network
    state=pilot._service_state(instance)
    if state['MainPID']:
        require(state['ActiveState']in('active','activating'),'update_existing_controller_requires_review')
        pilot._wait_ready(target,current)  # observation only, no duplicate start
    else:
        require(state['ActiveState']in('inactive','failed'),'update_existing_controller_requires_review')
        if network._state(instance)['MainPID']:network.unit(instance,'stop')
        if automatic and job.get('startup_attempted_at'):
            raise ConfigError('update_start_outcome_requires_review')
        from ..update_lifecycle import wait_stopped,checkpoint
        wait_stopped(target['root'],current['service_uid'],save=save or (lambda **values:None))
        if save:checkpoint(save,'controller_start',startup_attempted_at=time.time())
        started=pilot.lifecycle(instance,'start')
        require(started.get('ok')is True,'update_recovery_start_requires_review')
    wait_for_workers(instance,target,current)
    _restore_integrations(instance,target,job)


def _restore_integrations(instance,target,job):
    """Restore saved service intent without starting/stopping the bot."""
    from .. import pilot_operations as pilot
    from ..network import operations as network
    pilot._systemctl('enable'if job['service_enabled']else'disable',target['unit'])
    if job['network_active']:
        state=network._state(instance)
        if not state['MainPID']:
            require(state['ActiveState']in('inactive','failed'),'update_existing_network_agent_requires_review')
            network.unit(instance,'start')
        else:require(state['ActiveState']in('active','activating'),'update_existing_network_agent_requires_review')
    # Inactive network services are never stopped as a side effect of recovery.
    # A separately manually started agent is retained; only original enablement
    # is restored for the checked unit when that unit exists.
    if network._checked_unit(instance,optional=not job['network_active'])is not None:
        pilot._systemctl('enable'if job['network_enabled']else'disable',network.name(instance))


def controller(instance,operation_id,decision='review',*,automatic=False):
    try:return _controller(instance,operation_id,decision,automatic=automatic)
    except Exception as error:
        # No arbitrary exception text, request parameters or credentials survive.
        # Rebind/re-read while locked before annotating this exact transaction.
        try:
            from ..pilot_upgrade import _binding
            from .. import controller_update as native
            from ..update_lifecycle import failed
            target,_,_,_=_binding(instance)
            with file_lease(target['prefix']/'controller-update-admin.lock'):
                path=target['prefix']/native.NAME;job=_control(protected(path))
                if job.get('id')==operation_id and job.get('phase')not in('complete','cancelled') and decision=='complete':
                    def save(**values):job.update(values);_write(path,job)
                    failed(save,job,error)
                    if automatic:return {'ok':False,'phase':job['phase'],'failure_code':job['failure_code'],
                        'failure_stage':job['failure_stage'],'reason':job['reason']}
        except Exception:pass  # Never replace the original refusal with reporting failure.
        raise


def _controller(instance,operation_id,decision='review',*,automatic=False):
    identifier(operation_id)
    require(type(automatic)is bool and (not automatic or decision=='complete'),'update_recovery_decision_refused')
    require(decision in('review','complete','cancel','rollback'),'update_recovery_decision_refused')
    from ..pilot_installation import _admin
    from ..pilot_upgrade import _binding
    from .. import controller_update as native
    from .. import pilot_operations as pilot
    _admin();target,_,_,current=_binding(instance)
    with file_lease(target['prefix']/'deployment-admission.lock'),file_lease(target['prefix']/'controller-update-admin.lock'):
        target,_,_,current=_binding(instance)
        job=_control(protected(target['prefix']/native.NAME))
        require(job.get('id')==operation_id,'update_recovery_operation_changed')
        require(current in(job.get('before'),job.get('after')),'update_recovery_registration_changed')
        for runtime in(job['before'],job['after']):
            for key in('installation_id','generation','prefix','host_root','service_uid','execution'):
                require(runtime.get(key)==current[key],'update_recovery_binding_changed')
            validate_runtime(runtime,target['prefix'])
        upgrade=_upgrade(target,job,current)
        service=pilot._service_state(instance)
        phase=(upgrade or{}).get('phase','absent')
        require(phase=='prepared' or phase not in('committed','aborted') or current==job['after'if phase=='committed'else'before'],
                'update_upgrade_registration_requires_review')
        if decision=='review':
            return {'ok':True,'operation_id':operation_id,'phase':job['phase'],'upgrade_phase':phase,
                'registered_successor':current==job['after'],'service_state':service['ActiveState'],
                'service_pid_present':bool(service['MainPID']),'services_restarted':False,
                'next_action':'explicit_complete_or_cancel; retain_unknown_ownership; rollback_not_automatic'}
        if job['phase']=='complete':
            require(decision=='complete','update_completed_controller_cannot_be_rolled_back')
            require(current==job['after'] and phase=='committed','update_completed_controller_registration_changed')
            return {'ok':True,'phase':'complete','services_restarted':False,'replayed':True}
        if job['phase']=='cancelled' and decision=='rollback':
            require(phase=='aborted' and current==job['before'],'update_controller_rollback_refused')
            return {'ok':True,'phase':'cancelled','services_restarted':False,'replayed':True}
        require(job['phase']!='cancelled' or decision=='cancel','update_cancelled_controller_requires_new_operation')
        if automatic:
            from ..update_lifecycle import automatic_eligible
            require(automatic_eligible(job),'update_recovery_limit')
        archive=protected(Path(job['archive']))
        require(archive==target['prefix']/('controller-'+job['after']['bundle_sha256']+'.zip')
            and hashlib.sha256(archive.read_bytes()).hexdigest()==job['after']['bundle_sha256'],
            'update_recovery_archive_changed')
        if decision=='rollback':
            # Only abort an unfinished, stopped native registration transition.
            # Never reverse a committed controller/data contract automatically.
            require(upgrade is not None and phase=='prepared' and not service['MainPID']
                and service['ActiveState']in('inactive','failed'),'update_controller_rollback_refused')
            _approval(target,job,recovering=True)
            _archive(target,job)
            from ..pilot_upgrade import upgrade as finish
            finish(instance,archive,job['after']['bundle_sha256'],from_sha256=job['before']['bundle_sha256'],
                   recover='abort',transaction_id=upgrade['transition_id'])
            native._gate(target,job['before'],job,None)
            _write(target['prefix']/native.NAME,{**job,'phase':'cancelled','reason':'operator_aborted_prepared_controller_upgrade'})
            native.disarm(instance)
            return {'ok':True,'phase':'cancelled','services_restarted':False,'known_working_registration_restored':True}
        if decision=='cancel':
            require(current==job['before'] and phase not in('prepared','committed'),
                    'update_controller_commit_requires_completion_or_prepared_abort')
            _archive(target,job)
            native._gate(target,job['before'],job,None)
            _write(target['prefix']/native.NAME,{**job,'phase':'cancelled','reason':'operator_cancelled_before_registration_change'})
            native.disarm(instance)
            return {'ok':True,'phase':'cancelled','services_restarted':False,'service_start_is_explicit':not bool(service['MainPID'])}
        before_manifest=validate_runtime(job['before'],target['prefix'])['manifest']
        after_manifest=validate_runtime(job['after'],target['prefix'])['manifest']
        def configuration_matches():
            return _configuration_matches(target,job,before_version=before_manifest.get('version'),
                after_version=after_manifest.get('version')if current==job['after']and phase=='committed'else None)
        require(configuration_matches(),'update_recovery_configuration_changed')
        _archive(target,job)
        def save(**values):job.update(values);_write(target['prefix']/native.NAME,job)
        from ..update_lifecycle import checkpoint
        if automatic:
            save(phase='recovering',automatic_recovery={**job['automatic_recovery'],
                'attempts':job['automatic_recovery']['attempts']+1,'last_attempt_at':time.time()})
        checkpoint(save,'recovery_validation')
        if current==job['before'] and service['MainPID']:
            # A prior stop wasn't dispatched/completed. Never inherit old empty
            # observations or a boot's process identity, and never stop players.
            require(phase!='prepared','update_upgrade_pending_while_service_running')
            require(service['ActiveState']=='active','update_existing_controller_requires_review')
            _fresh_running(target,current);_approval(target,job)
            checkpoint(save,'network_restore')
            _restore_integrations(instance,target,job)
            native._gate(target,current,job,None)
            reset={k:v for k,v in job.items()if k not in('commit_started_at','commit_admitted_at','services_stopped_at',
                'upgrade_transition','nonce','lock_started','locked_since','room_ids','worker_identity')}
            reset.update(phase='waiting_empty',process=native._process(service['MainPID']),empty_since=None,
                         reason='operator_recovery_requires_new_empty_room_admission')
            _write(target['prefix']/native.NAME,reset)
            # Held manual jobs disarm their owned timer. Re-arm only that
            # receipt-bound launcher; independent updater jobs have no timer.
            if job.get('timer_sha256'):native._arm(instance)
            return {'ok':True,'phase':'waiting_empty','services_restarted':False,'fresh_empty_window_required':True}
        if current==job['before'] or phase=='prepared':
            require(not service['MainPID'] and service['ActiveState']in('inactive','failed'),
                    'update_existing_controller_requires_review')
            from ..network import operations as network
            if network._state(instance)['MainPID']:network.unit(instance,'stop')
            from ..update_lifecycle import wait_stopped
            checkpoint(save,'cleanup')
            wait_stopped(target['root'],current['service_uid'],save=save)
            native._gate(target,job['before'],job,None)
            from ..pilot_upgrade import upgrade as finish
            checkpoint(save,'upgrade')
            if phase=='prepared':
                result=finish(instance,archive,job['after']['bundle_sha256'],from_sha256=job['before']['bundle_sha256'],
                              recover='complete',transaction_id=upgrade['transition_id'])
            else:
                # No partially prepared upgrade exists. Starting a new stopped
                # switch still requires current unexpired approval.
                _approval(target,job)
                result=finish(instance,archive,job['after']['bundle_sha256'],from_sha256=job['before']['bundle_sha256'])
            job={**job,'upgrade_transition':result['transition_id']}
            _write(target['prefix']/native.NAME,job)
            target,_,_,current=_binding(instance)
        require(current==job['after'],'update_recovery_registration_changed')
        upgrade=_upgrade(target,job,current)
        require(upgrade is not None and upgrade['phase']=='committed','update_recovery_committed_upgrade_required')
        _approval(target,job,recovering=True)
        checkpoint(save,'readiness')
        _restore_services(instance,target,current,job,save=save,automatic=automatic)
        # The successor may have materialized its own version after a previous
        # interrupted start or the start just observed above. Prove every old
        # queued byte again, rather than treating that expected stamp as a user
        # edit or accepting unrelated configuration changes.
        require(_configuration_matches(target,job,before_version=before_manifest.get('version'),
            after_version=after_manifest.get('version')),'update_recovery_configuration_changed')
        _write(target['prefix']/native.NAME,{**job,'phase':'complete','completed_at':time.time(),
                                           'stage':'completion','reason':'automatic_recovered_controller_commit' if automatic else 'operator_recovered_controller_commit'})
        native.disarm(instance)
        return {'ok':True,'phase':'complete','existing_service_not_restarted':bool(service['MainPID']),
                'configuration_preserved':True}
