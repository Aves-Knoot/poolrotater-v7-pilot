"""Explicit queue-only bridge for the first managed-host successor.

Run only from a separately hash-verified release SDK as administrator. Existing
trust verifies the selected artifact again. No stop/start, provider call, token
change, ownership clear or root launcher replacement is performed here.
"""
import json
import time
from pathlib import Path

from .config import ConfigError
from .layout import file_lease, atomic_json
from .provisioning import _control
from .network.contracts import require


def unsubmitted(operation, parent, job):
    """Only an older terminal job can prove this scheduler intent was not queued."""
    require(operation['release']['kind']=='controller'
            and operation['before_sha256']==parent['bundle_sha256']
            and operation.get('effective_before_sha256',operation['before_sha256'])==parent['bundle_sha256'],
            'queue_activation_baseline_changed')
    if job is None:return
    require(job.get('id')!=operation['id'] and job.get('phase')in('complete','cancelled')
            and job.get('queued_at',float('inf'))<operation['queued_at']
            and job.get('after'if job['phase']=='complete'else'before')==parent,
            'queue_activation_native_review_required')


def command(instance, expected_sha256):
    from .pilot_installation import administrative_binding
    from .runtime_lifecycle import validate_runtime
    from .updates.operations import client, publish_report
    from .updates.native import Backend, require_idle_worker_deployment
    from .updates.agent import Agent, policy
    from .updates.client import read
    from .updates.contracts import digest
    from .operator_maintenance import require_idle
    digest(expected_sha256)
    target,marker,parent=administrative_binding(instance)
    runtime=validate_runtime(parent,target['prefix'])
    require(runtime['manifest']['version']in('v7.2.9','v7.2.10','v7.2.11'),
            'queue_activation_runtime_requires_review')
    if parent['bundle_sha256']==expected_sha256:
        return {'ok':True,'phase':'already_installed','queue_only':True,'rooms_stopped':False,
                'credentials_changed':False,'ownership_cleared':False}
    configured=client(instance);backend=Backend(instance,configured);agent=Agent(configured,backend)
    # The existing agent lease prevents a concurrent timer from dispatching this
    # same scheduler operation while its preflight evidence is reviewed.
    with file_lease(configured.root/'agent.lock'):
        state=agent.state();choices=policy(read(configured.root/'policy.json'),configured.binding)
        require(not choices['paused']and choices['automatic_install']and choices['controller_updates'],
                'queue_activation_policy_refused')
        current=backend.current()
        native=_control(target['prefix']/'controller-update.json',optional=True)
        pending=state['operation']
        same_queued=(pending and native and native.get('id')==pending['id']
                     and native.get('after',{}).get('bundle_sha256')==expected_sha256
                     and native.get('phase')in('waiting_empty','locking'))
        require((not current['deployment_busy']or same_queued)and current['controller_sha256']==parent['bundle_sha256']
                and current['effective_sha256']==parent['bundle_sha256'],
                'queue_activation_baseline_changed')
        checked=configured.check(choices['channel'],parent['bundle_sha256'])
        require(checked['available_version'] is not None,'queue_activation_no_successor')
        item=configured.state()['available']
        require(item['artifact']['sha256']==expected_sha256 and item['kind']=='controller',
                'queue_activation_signed_target_changed')
        operation=state['operation']
        if operation and operation['release']['artifact']['sha256']!=expected_sha256:
            require(state['phase']=='held','queue_activation_native_review_required')
            with file_lease(target['prefix']/'deployment-admission.lock'),file_lease(target['prefix']/'controller-update-admin.lock'):
                require(administrative_binding(instance)[2]==parent,'queue_activation_baseline_changed')
                require_idle(target,parent);require_idle_worker_deployment(target,parent)
                job=_control(target['prefix']/'controller-update.json',optional=True)
                unsubmitted(operation,parent,job)
                # Preserve explicit evidence before retiring only scheduler
                # intent. No native intent/journal is cancelled or rewritten.
                proof={'operation_id':operation['id'],'before_sha256':parent['bundle_sha256'],
                       'native_job_id':job.get('id')if job else None,
                       'native_phase':job.get('phase')if job else None,
                       'decision':'cancel_unsubmitted_preflight','at':time.time()}
                folder=configured.root/'history';folder.mkdir(mode=0o700,exist_ok=True)
                receipt=folder/(operation['id']+'.queue-preflight-review.json')
                require(not receipt.exists(),'queue_activation_prior_review_requires_inspection')
                atomic_json(receipt,proof)
                agent._reviewed(state,operation,time.time(),choices,decision='cancel')
            operation=None
        if operation:
            # Reuse an already-staged target's SAME UUID; never create a second
            # operation to get around an uncertain native outcome.
            with file_lease(target['prefix']/'deployment-admission.lock'),file_lease(target['prefix']/'controller-update-admin.lock'):
                job=_control(target['prefix']/'controller-update.json',optional=True)
                if job and job.get('id')==operation['id']:
                    require(job.get('after',{}).get('bundle_sha256')==expected_sha256
                            and job.get('phase')in('waiting_empty','locking'),
                            'queue_activation_native_review_required')
                    result={'phase':job['phase']}
                else:unsubmitted(operation,parent,job);result=None
            if result is None:
                path,selected=configured.artifact(operation['release'])
                backend.stage(path,selected)
                result=backend.queue(path,selected,operation['id'])
            require(result['phase']in('waiting_empty','locking'),'queue_activation_native_review_required')
            agent._save(state,phase='waiting_empty',error=None,resolution=None)
    # No existing operation: ordinary signed apply creates the durable UUID,
    # stages the artifact and queues it through this SDK's fixed native code.
    if operation is None:result=agent.step('apply')
    else:result=agent.summary()
    require(result['phase']=='waiting_empty'and result.get('pending_version')==item['version'],
            'queue_activation_native_review_required')
    report={'ok':True,'phase':'waiting_empty','version':item['version'],
            'operation_id':result['operation_id'],'queue_only':True,
            'rooms_stopped':False,'credentials_changed':False,'ownership_cleared':False,
            'next_action':'leave_normal_updater_running; wait_for_verified_empty_room_boundary'}
    report['hub_report']=publish_report(instance,configured,result)
    return report
