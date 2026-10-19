"""Adapters to retained native deployments. No provider or token operations."""
import time
import os
from pathlib import Path

from ..config import ConfigError
from ..provisioning import _control, _write, protected
from .contracts import canonical, fingerprint, require


def require_idle_worker_deployment(target, parent):
    from ..worker_deployment import read_job, result_path
    job=read_job(parent)
    if job is None:return None
    receipt=_control(result_path(target['root'],job['id']),parent['service_uid'],private=True,optional=True)
    require(receipt is not None and receipt.get('phase') in ('complete','cancelled'),
            'update_worker_deployment_pending_or_failed')
    return job


def require_registered_workers(parent, rows):
    from ..worker_releases import validate_candidate
    directory=Path(parent['prefix'])/'worker-releases'/parent['installation_id']
    approved={parent['release']}
    for row in rows.values():
        release=row.get('release')
        if release in approved:continue
        found=False
        if directory.exists():
            protected(directory)
            for path in directory.glob('*.json'):
                value=_control(protected(path))
                if value.get('candidate',{}).get('release')==release:
                    validate_candidate(parent,value);found=True;break
        require(found,'update_unregistered_worker_release')
        approved.add(release)


def retire_worker_job(target, parent):
    """Reuse the accepted stopped, positively-closed core retirement guard."""
    from ..worker_deployment import retire_after_controller_upgrade
    return retire_after_controller_upgrade(target,parent)


class Backend:
    def __init__(self, instance, client):
        self.instance=instance;self.client=client

    def binding(self):
        from ..pilot_installation import administrative_binding
        return administrative_binding(self.instance)

    def current(self):
        target,marker,parent=self.binding()
        require(marker['installation_id']==self.client.binding['installation_id']
                and marker['generation']==self.client.binding['generation'], 'update_native_binding_changed')
        # Enrollment is verified locally through the existing checked service
        # entry; this reads only network identity, never osu! credentials.
        from ..network.operations import command
        network=command(self.instance,'status')
        require(network.get('enrolled') is True and network.get('account_binding_matches') is True
                and network.get('host_id')==self.client.binding['host_id']
                and network.get('credential_expires_at',0)>time.time()
                and network.get('pending_operation') is None, 'update_enrollment_required')
        from ..pilot_operations import _service_state,_service_observation
        service=_service_state(self.instance)
        observation=_service_observation(target,parent,service)if service['ActiveState']=='active'else None
        # systemd marks a simple unit active before cold catalog loading and
        # worker startup finish. Do not submit new unattended work against old
        # status files during that interval. Native ownership/emptiness checks
        # still independently govern every queued mutation.
        active=bool(observation and observation.get('phase')=='running')
        snapshot=_control(target['root']/'state/rolling/status.json',parent['service_uid'],optional=True)or{}
        rows=snapshot.get('rooms',{})
        controller_job=_control(target['prefix']/'controller-update.json',optional=True)
        busy=bool(controller_job and controller_job.get('phase') not in ('complete','cancelled'))
        from ..private_pilot import PROFILE
        busy=busy or os.path.lexists(target['root']/PROFILE)
        try:require_idle_worker_deployment(target,parent)
        except ConfigError:busy=True
        releases={r.get('release')for r in rows.values()}
        effective=parent['bundle_sha256'];version=_control(protected(Path(parent['release'])/'release.json'))['version']
        if active and len(releases)==1 and releases!={parent['release']}:
            from ..worker_releases import validate_candidate
            folder=target['prefix']/'worker-releases'/marker['installation_id']
            if folder.exists():
                for path in folder.glob('*.json'):
                    value=_control(protected(path))
                    if value.get('candidate',{}).get('release') in releases:
                        validate_candidate(parent,value)
                        effective=value['candidate']['bundle_sha256'];version=next(iter(rows.values())).get('version');break
        if active:
            from .contracts import version as version_tuple
            versions=[version]+[r['version']for r in rows.values()if r.get('version')]
            version=max(versions,key=version_tuple)
        return {'controller_sha256':parent['bundle_sha256'],'effective_sha256':effective,
                'version':version,'deployment_busy':busy,'service_active':active}

    def stage(self, path, item):
        target,marker,parent=self.binding()
        current=self.current()
        baseline=(parent['bundle_sha256']if parent['bundle_sha256']in item['from_sha256']else current['effective_sha256'])
        require(baseline in item['from_sha256'],'update_native_baseline_changed')
        verified,expected=self.client.artifact(item)
        require(verified==path and expected==item,'update_native_artifact_changed')
        if item['kind']=='worker':
            from ..worker_operations import command
            return command(self.instance,'stage',bundle=path,sha256=item['artifact']['sha256'])
        from ..provisioning import _provision
        from ..pilot_installation import RESERVE
        staged=_provision(target['root'],target['prefix'],path,item['artifact']['sha256'],
            uid=parent['service_uid'],execution=parent['execution'],reserve=RESERVE,stage_bootstrap=True)
        candidate=_control(target['prefix']/'candidates'/marker['installation_id']/(item['artifact']['sha256']+'.json'))
        from ..runtime_lifecycle import validate_runtime
        from .approval import compatible
        compatible(validate_runtime(parent,target['prefix'])['manifest'],
                   validate_runtime(candidate,target['prefix'])['manifest'])
        state=self.client.state();envelope={'signed':state['index']}
        # Retain the original threshold signatures, not a reconstructed unsigned
        # document, for native queue/commit and interrupted recovery verification.
        from .client import read
        envelope=read(self.client.root/'verified-envelope.json')
        require(envelope.get('signed')==state['index'],'update_approval_envelope_changed')
        from .trust import verify
        verify(canonical(envelope),self.client.bootstrap,now=time.time(),previous=state['trust'])
        folder=target['prefix']/'updates/approvals';folder.mkdir(mode=0o700,exist_ok=True)
        protected(folder)
        from ..layout import atomic_json
        atomic_json(folder/(item['artifact']['sha256']+'.json'),{
            'installation_id':marker['installation_id'],'generation':marker['generation'],
            'before_sha256':parent['bundle_sha256'],'after_sha256':item['artifact']['sha256'],
            'baseline_sha256':baseline,
            'accepted_at':time.time(),'envelope':envelope})
        return staged

    def queue(self,path,item,operation_id):
        _,_,parent=self.binding()
        if item['kind']=='worker':
            from ..worker_operations import command
            return command(self.instance,'queue',sha256=item['artifact']['sha256'],deployment_id=operation_id)
        from ..controller_update import queue
        # Install independent diagnostics before any admitted bot/network stop.
        # Existing opt-outs and pins are preserved; a damaged coordinator is
        # a preflight refusal, never an excuse to stop the old rooms first.
        from ..rescue.operations import ensure
        ensure(self.instance,parent)
        return queue(self.instance,path,item['artifact']['sha256'],from_sha256=parent['bundle_sha256'],
                     deployment_id=operation_id,release_approval_required=True)

    def observe(self,operation):
        target,marker,parent=self.binding()
        if operation['release']['kind']=='controller':
            job=_control(target['prefix']/'controller-update.json',optional=True)
            if job is None:return {'phase':'absent'}
            if job.get('id')!=operation['id']:return {'phase':'conflict'}
            if job.get('after',{}).get('bundle_sha256')!=operation['release']['artifact']['sha256']:
                return {'phase':'conflict'}
            if job['phase']=='complete':
                require(parent==job['after'],'update_completed_controller_registration_changed')
                from ..runtime_lifecycle import validate_runtime
                validate_runtime(parent,target['prefix'])
            result={'phase':job['phase']}
        else:
            from ..worker_deployment import read_job,result_path
            job=read_job(parent)
            if job is None:return {'phase':'absent'}
            if job['id']!=operation['id'] or job['candidate_sha256']!=operation['release']['artifact']['sha256']:
                return {'phase':'conflict'}
            receipt=_control(result_path(target['root'],job['id']),parent['service_uid'],private=True,optional=True)
            if receipt is not None:
                require(all(receipt.get(k)==job.get(j)for k,j in (
                    ('id','id'),('parent_sha256','parent_sha256'),('candidate_sha256','candidate_sha256'),('rooms','rooms'))),
                    'update_worker_receipt_binding_refused')
                if receipt.get('phase')=='complete':
                    from ..worker_releases import receipt_path,validate_candidate
                    approved=_control(protected(receipt_path(parent,job['candidate_sha256'])))
                    candidate=validate_candidate(parent,approved)
                    journal=_control(target['root']/'state/rolling/ownership.json',parent['service_uid'],private=True)
                    require(all(journal.get('rooms',{}).get(a,{}).get('release')==str(candidate['release'])
                                for a in job['rooms']), 'update_completed_worker_registration_changed')
            result={'phase':receipt['phase'] if receipt else 'queued'}
        snapshot=_control(target['root']/'state/rolling/status.json',parent['service_uid'],optional=True)or{}
        if result['phase']in('complete','cancelled'):
            return {**result,'waiting_rooms':[]}
        rows=snapshot.get('rooms',{})
        if operation['release']['kind']=='controller':
            # A controller job has no worker target_version. Report every
            # configured room whose current observation cannot qualify as
            # empty/idle, including an unknown roster, while native admission
            # remains the sole authority for the actual maintenance window.
            result['waiting_rooms']=[alias for alias in job['rooms']
                if (row:=rows.get(alias,{})).get('phase')!='running'
                or row.get('roster_known')is not True or row.get('observed_players')!=0
                or row.get('status',{}).get('players')!=0
                or row.get('status',{}).get('state')!='IDLE']
        else:
            # Historical target labels can remain after an unrelated upgrade.
            # Only this operation's real pending worker targets are waiting.
            result['waiting_rooms']=[alias for alias in job['rooms']
                if (row:=rows.get(alias,{})).get('target')
                and row.get('target_version')==operation['release']['version']
                and row.get('phase')in('waiting_empty','draining')]
        return result

    def retryable(self,operation):
        """Explicit recovery of an unsubmitted stage; never infer lost dispatch.

        Any native intent/result or changed controller/worker baseline is held for
        native recovery. This check grants no room/credential operation itself.
        """
        target,marker,parent=self.binding()
        if parent['bundle_sha256']!=operation['before_sha256']:return False
        if self.observe(operation)['phase']!='absent':return False
        from ..worker_deployment import result_path
        if _control(result_path(target['root'],operation['id']),parent['service_uid'],private=True,optional=True)is not None:
            return False
        current=self.current()
        if current['deployment_busy']:return False
        if current['effective_sha256']!=operation.get('effective_before_sha256',operation['before_sha256']):return False
        snapshot=_control(target['root']/'state/rolling/status.json',parent['service_uid'])
        from ..ratings.operations import _identity
        from ..layout import load
        with _identity(parent['service_uid']):_,config=load(target['root'])
        rows=snapshot.get('rooms',{})
        return (set(rows)=={r['alias']for r in config['rooms']}and all(
            r.get('phase')=='running'and r.get('healthy')is True and r.get('operation')=='owned'
            and r.get('ownership_unknown')is False and not r.get('error')for r in rows.values()))

    def recover(self,operation,decision):
        from .contracts import identifier
        identifier(operation['id'])
        if operation['release']['kind']=='controller':
            from .recovery import controller
            return controller(self.instance,operation['id'],decision)
        observed=self.observe(operation)
        if decision=='review':return {'ok':True,**observed,'services_restarted':False}
        if observed['phase']=='complete':return {'ok':True,**observed,'services_restarted':False}
        require(decision=='cancel','update_worker_recovery_requires_cancel_or_native_rollback_review')
        target,marker,parent=self.binding()
        from ..worker_deployment import read_job,result_path
        from ..worker_operations import command
        job=read_job(parent)
        require(job is not None and job['id']==operation['id']
                and job['candidate_sha256']==operation['release']['artifact']['sha256'],
                'update_recovery_operation_changed')
        command(self.instance,'cancel',deployment_id=operation['id'],sha256=operation['release']['artifact']['sha256'])
        from ..pilot_operations import _service_state,_stopped
        state=_service_state(self.instance)
        if not state['MainPID']and state['ActiveState']in('inactive','failed'):
            # A stopped service cannot acknowledge its canceled intent. Native
            # stopped ownership guards, not observations, qualify local receipt.
            from ..ratings.operations import _identity
            from ..layout import atomic_json
            with _stopped(target['root'],parent['service_uid']),_identity(parent['service_uid']):
                path=result_path(target['root'],job['id']);path.parent.mkdir(mode=0o700,exist_ok=True)
                atomic_json(path,{'format':1,'id':job['id'],'phase':'cancelled',
                    'parent_sha256':job['parent_sha256'],'candidate_sha256':job['candidate_sha256'],
                    'rooms':job['rooms'],'at':time.time(),'reason':'operator_cancelled_with_confirmed_stopped_ownership'})
            return {'ok':True,'phase':'cancelled','services_restarted':False}
        require(state['ActiveState']in('active','activating')and state['MainPID'],
                'update_recovery_service_state_requires_review')
        return {'ok':True,'phase':'cancel_requested','services_restarted':False}

    def advance(self,operation):
        if operation['release']['kind']=='controller':
            # Dispatch the staged successor's checked entry, not this installed
            # predecessor's admission/commit implementation. The fixed entry
            # validates its complete source/environment before every import.
            target,_,parent=self.binding()
            job=_control(target['prefix']/'controller-update.json')
            require(job.get('id')==operation['id']
                    and job.get('after',{}).get('bundle_sha256')==operation['release']['artifact']['sha256'],
                    'update_recovery_operation_changed')
            from .executor import controller
            return controller(self.instance,target,job)

    def references(self):
        from .installed_retention import references
        target,marker,parent=self.binding()
        from ..private_pilot import PROFILE
        if os.path.lexists(target['root']/PROFILE):
            from .installed_retention import records
            return set(records(target,parent))
        return references(target,parent)[0]

    def cleanup_releases(self,*,pinned,keep):
        from .installed_retention import cleanup
        target,marker,parent=self.binding()
        return cleanup(target,parent,pinned=pinned,keep=keep)
