"""Adapters to retained native deployments. No provider or token operations."""
import time
import os
import math
import re
import shutil
from pathlib import Path

from ..config import ConfigError
from ..provisioning import _control, _write, protected
from .contracts import canonical, fingerprint, require, digest, identifier


def _terminal_unrelated_controller_job(job, operation_id, target, marker, parent):
    """Accept only a positively cancelled predecessor with no open native work."""
    try:
        if (type(job)is not dict or job.get('format')!=1 or job.get('phase')!='cancelled'
                or job.get('id')==operation_id
                or job.get('reason') not in ('operator_cancelled_before_registration_change',
                                             'operator_cancelled_before_commit')
                or job.get('before')!=parent):return False
        identifier(job.get('id'))
        after=job.get('after')
        if type(after)is not dict:return False
        identity=('installation_id','generation','prefix','host_root','service_uid','execution')
        if any(after.get(key)!=parent.get(key) for key in identity):return False
        digest(after.get('bundle_sha256'))
        if (after['bundle_sha256']==parent['bundle_sha256']
                or Path(after.get('release','')).parent!=Path(target['prefix'])/'releases'):
            return False
        from ..runtime_lifecycle import validate_runtime
        validate_runtime(parent,target['prefix']);validate_runtime(after,target['prefix'])
        aliases={r['alias'] for r in _host_configuration(target,parent)['rooms']}
        rooms=job.get('rooms')
        if type(rooms)is not list or rooms!=sorted(aliases):return False
        for key in ('commit_started_at','commit_admitted_at','services_stopped_at',
                    'startup_attempted_at','upgrade_transition','completed_at'):
            if job.get(key)is not None:return False
        if job.get('drain_restoration_phase') not in (None,'complete'):return False
        if job.get('controller_update_drain') is not None and job.get('drain_restoration_phase')!='complete':
            return False
        from ..controller_update import GATE
        if os.path.lexists(Path(target['root'])/GATE):return False
        from ..runtime_lifecycle import require_no_pending
        require_no_pending(target['prefix'],marker['installation_id'])
        return True
    except Exception:
        return False


def _host_configuration(target,parent):
    from ..ratings.operations import _identity
    from ..layout import load
    with _identity(parent['service_uid']):
        _,host=load(target['root'])
    return host


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
    from ..managed_host.admission import closed_aliases
    closed=closed_aliases({'root':Path(parent['host_root'])},parent,rows) if parent.get('host_root') else frozenset()
    for alias,row in rows.items():
        if alias in closed:continue
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
        managed_stopped=False
        if not active and service.get('MainPID')==0 and service.get('ActiveState') in ('inactive','failed'):
            from ..managed_host.admission import stopped_host
            try:managed_stopped=stopped_host(target,parent) is not None
            except Exception:pass  # Missing/uncertain evidence cannot authorize a stopped update.
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
                'version':version,'deployment_busy':busy,'service_active':active,'managed_stopped':managed_stopped}

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

    def staging_ready(self,path,item):
        """Read-only reserve check; provisioning repeats the authoritative guard."""
        if item.get('kind')!='controller':return True
        target,marker,parent=self.binding()
        current=self.current()
        require(not current['deployment_busy'],'update_native_deployment_busy')
        baseline=(parent['bundle_sha256']if parent['bundle_sha256']in item.get('from_sha256',())
                  else current['effective_sha256'])
        require(baseline in item.get('from_sha256',()),'update_native_baseline_changed')
        verified,selected=self.client.artifact(item)
        require(Path(verified)==Path(path) and selected==item,'update_native_artifact_changed')
        from ..runtime_lifecycle import require_no_pending
        require_no_pending(target['prefix'],marker['installation_id'])
        from ..controller_update import GATE
        require(not os.path.lexists(Path(target['root'])/GATE),'update_native_maintenance_pending')
        from ..runtime_package import inspect as inspect_runtime
        spec=inspect_runtime(path,item['artifact']['sha256'],execution=item['execution'])
        require(fingerprint(spec)==item['manifest_sha256'],'update_native_artifact_changed')
        try:input_bytes=Path(path).stat().st_size
        except OSError:raise ConfigError('update_native_artifact_changed')from None
        required=(sum(file['bytes']for file in spec['files'].values())
            +sum(wheel['expanded_bytes']for wheel in spec['wheels'])+32*1024**2+input_bytes)
        from ..pilot_installation import RESERVE
        return shutil.disk_usage(target['prefix']).free>=RESERVE+required

    def _unsubmitted_controller(self,operation):
        """Return exact current context only when no matching native dispatch exists."""
        try:
            require(operation['release']['kind']=='controller','update_native_recovery_required')
            identifier(operation['id']);digest(operation['before_sha256'])
            digest(operation['release']['artifact']['sha256'])
            target,marker,parent=self.binding()
            if parent['bundle_sha256']!=operation['before_sha256']:return None
            job=_control(target['prefix']/'controller-update.json',optional=True)
            if job is not None:
                if job.get('id')==operation['id'] or not _terminal_unrelated_controller_job(
                        job,operation['id'],target,marker,parent):return None
            from ..worker_deployment import result_path
            if _control(result_path(target['root'],operation['id']),parent['service_uid'],
                        private=True,optional=True)is not None:return None
            from ..controller_update import GATE
            if os.path.lexists(Path(target['root'])/GATE):return None
            from ..runtime_lifecycle import require_no_pending
            require_no_pending(target['prefix'],marker['installation_id'])
            current=self.current()
            if (current['deployment_busy']
                    or current['effective_sha256']!=operation.get(
                        'effective_before_sha256',operation['before_sha256'])):return None
            if not (current['service_active'] or current['managed_stopped']):return None
            return target,marker,parent,current
        except Exception:
            return None

    def _retryable_room_set(self,target,parent,snapshot):
        """Qualify healthy running rows or positively closed managed slots."""
        try:
            generated=snapshot.get('generated_at');now=time.time();rows=snapshot.get('rooms')
            if (type(generated)not in(int,float)or not math.isfinite(generated)
                    or not 0<=now-generated<=8 or snapshot.get('configuration_error')
                    or type(rows)is not dict):return False
            aliases={r['alias']for r in _host_configuration(target,parent)['rooms']}
            if set(rows)!=aliases:return False
            from ..reconciliation import _journal
            journal=_journal(_control(target['root']/'state/rolling/ownership.json',
                parent['service_uid'],private=True),aliases)['rooms']
            if set(journal)!=aliases:return False
            from ..managed_host.admission import closed_aliases
            closed=closed_aliases(target,parent,rows)
            if not closed<=aliases:return False
            require_registered_workers(parent,rows)
            identity_fields=('pid','generation','room_id','release')
            for alias,row in rows.items():
                if alias in closed:continue
                stored=journal[alias];status=row.get('status');age=row.get('heartbeat_age')
                if (type(status)is not dict or row.get('phase')!='running'
                        or row.get('healthy')is not True or row.get('operation')!='owned'
                        or row.get('ownership_unknown')is not False or row.get('error')
                        or row.get('admission_locked') or row.get('roster_known')is not True
                        or type(row.get('observed_players'))is not int or row['observed_players']<0
                        or type(age)not in(int,float)or not math.isfinite(age)or age<0
                        or age+now-generated>8 or status.get('ready')is not True
                        or status.get('running')is not True
                        or status.get('room_id')!=row.get('room_id')
                        or type(status.get('players'))is not int or status['players']<0
                        or status['players']!=row['observed_players']):return False
                if (stored.get('phase')!='running' or stored.get('operation')!='owned'
                        or stored.get('ownership_unknown')is not False or stored.get('error')
                        or any(row.get(k)!=stored.get(k)for k in identity_fields)
                        or type(row.get('pid'))is not int or row['pid']<=0
                        or type(row.get('room_id'))is not int or row['room_id']<=0
                        or type(row.get('generation'))is not str
                        or re.fullmatch('[a-f0-9]{32}',row['generation'])is None
                        or type(row.get('release'))is not str):return False
            return True
        except Exception:
            return False

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
            if job is None or job.get('id')!=operation['id']:
                return {'phase':'absent'} if self._unsubmitted_controller(operation) else {'phase':'conflict'}
            if job.get('after',{}).get('bundle_sha256')!=operation['release']['artifact']['sha256']:
                return {'phase':'conflict'}
            if job['phase']=='complete':
                require(parent==job['after'],'update_completed_controller_registration_changed')
                from ..runtime_lifecycle import validate_runtime
                validate_runtime(parent,target['prefix'])
            result={'phase':job['phase']}
            # A reviewed successor can revalidate a never-admitted settings
            # hold. Let the normal timer advance its protected entry; do not
            # clear a partially committed transaction or execute old code.
            from ..controller_update import configuration_retryable
            from .contracts import version as version_tuple
            if (configuration_retryable(job) and parent == job.get('before')
                    and version_tuple(_control(protected(Path(job['after']['release'])/'release.json'))['version'])
                    >= version_tuple('v7.3.1')):
                result['phase'] = 'waiting_empty'
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
        if operation['release']['kind']!='controller':
            target,marker,parent=self.binding()
            if parent['bundle_sha256']!=operation['before_sha256']:return False
            if self.observe(operation)['phase']!='absent':return False
            from ..worker_deployment import result_path
            if _control(result_path(target['root'],operation['id']),parent['service_uid'],
                        private=True,optional=True)is not None:return False
            current=self.current()
            if (current['deployment_busy'] or current['effective_sha256']!=operation.get(
                    'effective_before_sha256',operation['before_sha256'])):return False
            snapshot=_control(target['root']/'state/rolling/status.json',parent['service_uid'])
            from ..ratings.operations import _identity
            from ..layout import load
            with _identity(parent['service_uid']):_,config=load(target['root'])
            rows=snapshot.get('rooms',{})
            return (set(rows)=={r['alias']for r in config['rooms']}and all(
                r.get('phase')=='running'and r.get('healthy')is True and r.get('operation')=='owned'
                and r.get('ownership_unknown')is False and not r.get('error')for r in rows.values()))
        context=self._unsubmitted_controller(operation)
        if context is None:return False
        target,marker,parent,current=context
        snapshot=_control(target['root']/'state/rolling/status.json',parent['service_uid'],optional=True)
        return type(snapshot)is dict and self._retryable_room_set(target,parent,snapshot)

    def recover(self,operation,decision):
        from .contracts import identifier
        identifier(operation['id'])
        if decision == 'supersede':
            from .superseded import review
            return review(self.instance, operation)
        if operation['release']['kind']=='controller':
            if decision in ('review','cancel'):
                context=self._unsubmitted_controller(operation)
                if context is not None:
                    if decision=='review':
                        return {'ok':True,'phase':'absent','unsubmitted':True,
                                'upgrade_phase':'absent','registered_successor':False,
                                'services_restarted':False}
                    return {'ok':True,'phase':'cancelled','unsubmitted':True,
                            'upgrade_phase':'absent','registered_successor':False,
                            'services_restarted':False}
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
