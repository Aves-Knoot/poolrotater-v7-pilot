"""Explicit local replacement of one committed, closed, failed-start update.

This is not rollback or automatic admission. A checksum-pinned local installer
authorizes a reviewed successor, archives the failed operation, and hands a new
stopped transaction to the ordinary protected native recovery implementation.
"""
import json
import os
from pathlib import Path
import time
import uuid

from .layout import file_lease
from .provisioning import _control, _write, protected
from .updates.contracts import fingerprint, require

VERSION='v7.1.16'
PREDECESSOR='98bc2e7a9a127062d7bbff451dbef743395dfe3dfb3c8453a4b53e268fa3010c'
INTENT='stopped-update-repair.json'


def configuration_version(target,job):
    """Recover the original config version from preserved exact signed intent."""
    from .runtime_lifecycle import validate_runtime
    evidence=_control(protected(target['prefix']/INTENT))
    old=evidence['predecessor_job']
    require(job['id']==evidence['operation_id'] and job['before']==old['after']
        and job['after']==evidence['candidate'] and job['configuration']==old['configuration']
        and job.get('repair_predecessor_operation_id')==old['id'],
        'repair_configuration_evidence_changed')
    return validate_runtime(old['before'],target['prefix'])['manifest']['version']


def qualify(current, job, upgrade, candidate, configuration, journal, aliases):
    """All positive evidence precedes changing the retained native job."""
    require(current['bundle_sha256']==PREDECESSOR and job is not None
        and job.get('after')==current and job.get('phase')=='held'
        and job.get('failure_stage')=='controller_start'
        and job.get('startup_attempted_at') is not None
        and job.get('release_approval_required') is True,
        'repair_committed_failed_start_required')
    require(upgrade is not None and upgrade.get('phase')=='committed'
        and upgrade.get('before')==job.get('before') and upgrade.get('after')==current,
        'repair_committed_registration_required')
    require(configuration==job.get('configuration'),'repair_configuration_changed')
    require(set(job.get('rooms',[]))==set(aliases)==set(journal.get('rooms',{}))
        and bool(aliases),'repair_room_topology_changed')
    for row in journal['rooms'].values():
        require(row.get('phase') in ('stopped','parked')
            and row.get('operation')=='closed' and row.get('ownership_unknown') is False
            and all(row.get(k) is None for k in ('room_id','channel_id','pid','target','creation_candidate'))
            and not row.get('error'),'repair_closed_ownership_required')
    for key in ('installation_id','generation','prefix','host_root','service_uid','execution',
                'environment','environment_id'):
        require(candidate.get(key)==current.get(key),'repair_candidate_binding_changed')
    require(candidate['bundle_sha256']!=current['bundle_sha256'], 'repair_candidate_required')


def prepare(instance,candidate,bundle):
    from .pilot_installation import _admin, administrative_binding, require_no_service_processes, _quiescent
    from .pilot_operations import _service_state
    from .network.operations import _state
    from .runtime_lifecycle import validate_runtime,read_transition
    from .pilot_upgrade import _record,_transition
    from .controller_update import NAME,_config_hashes,_gate
    from .updates.approval import require_approved,require_upgrade
    from .ratings.operations import _identity
    from .layout import load
    from .reconciliation import _journal
    _admin();target,_,current=administrative_binding(instance)
    with file_lease(target['prefix']/'deployment-admission.lock'), \
         file_lease(target['prefix']/'controller-update-admin.lock'), \
         _quiescent(target['root'],current['service_uid']):
        _,_,again=administrative_binding(instance)
        require(again==current,'repair_registration_changed')
        for state in (_service_state(instance),_state(instance)):
            require(state['MainPID']==0 and state['ActiveState'] in ('inactive','failed'),
                    'repair_requires_stopped_services')
        require_no_service_processes(current['service_uid'])
        runtime=validate_runtime(candidate,target['prefix'])
        require(runtime['manifest']['version']==VERSION,'repair_candidate_version_refused')
        job=_control(protected(target['prefix']/NAME))
        upgrade=_control(protected(target['prefix']/'pilot-upgrade.json'),maximum=512*1024)
        _record(upgrade,target['prefix'],current['installation_id'])
        require(read_transition(target['prefix'],current['installation_id'])==_transition(upgrade,'committed'),
                'repair_runtime_transition_changed')
        with _identity(current['service_uid']):_,host=load(target['root'])
        aliases={r['alias']for r in host['rooms']}
        journal=_journal(_control(target['root']/'state/rolling/ownership.json',current['service_uid'],private=True),aliases)
        qualify(current,job,upgrade,candidate,_config_hashes(target['root']),journal,aliases)
        require_approved(validate_runtime(job['before'],target['prefix'])['manifest'],
            validate_runtime(current,target['prefix'])['manifest'],job['before'],current,recovering=True)
        require_upgrade(validate_runtime(current,target['prefix'])['manifest'],runtime['manifest'],current,candidate)
        require(Path(bundle).is_file(),'repair_archive_required')
        from .runtime_package import digest
        require(digest(bundle)==candidate['bundle_sha256'],'repair_archive_changed')
        import shutil
        archive=target['prefix']/('controller-'+candidate['bundle_sha256']+'.zip')
        if not archive.exists():
            with archive.open('xb')as output,Path(bundle).open('rb')as source:
                shutil.copyfileobj(source,output);output.flush();os.fsync(output.fileno())
            archive.chmod(0o444)
        require(digest(protected(archive))==candidate['bundle_sha256'],'repair_archive_changed')
        now=time.time();operation=str(uuid.uuid4())
        successor={'format':1,'id':operation,'phase':'held','before':current,'after':candidate,
            'archive':str(archive),'rooms':sorted(aliases),'configuration':job['configuration'],
            'service_enabled':job['service_enabled'],'network_active':job['network_active'],
            'network_enabled':job['network_enabled'],'queued_at':now,'empty_since':None,
            'reason':'explicit_stopped_failed_update_replacement','automatic_recovery':{'format':1,'attempts':0},
            'commit_admitted_at':now,'services_stopped_at':now,'nonce':uuid.uuid4().hex,
            'repair_predecessor_operation_id':job['id']}
        evidence={'format':1,'operation_id':operation,'predecessor_job':job,
            'predecessor_upgrade':upgrade,'predecessor_journal_sha256':fingerprint(journal),
            'candidate':candidate,'successor_job':successor,'created_at':now}
        history=target['prefix']/'controller-recovery-history';history.mkdir(mode=0o700,exist_ok=True);protected(history)
        _write(history/(operation+'.stopped-repair.json'),evidence)
        # The durable intent precedes retiring the old gate/job. Its exact
        # successor bytes allow a subsequent invocation to recognize dispatch.
        path=target['prefix']/INTENT
        require(not path.exists(),'repair_existing_intent_requires_review')
        _write(path,evidence)
        _gate(target,current,job,None)
        _write(target['prefix']/NAME,successor)
        return successor


def reconcile(instance,job):
    """Retire old scheduler intent only after actual healthy successor proof."""
    from .updates.operations import client as configured_client
    from .pilot_installation import administrative_binding
    from .updates.agent import Agent
    from .updates.native import Backend
    from .provisioning import _control
    target,_,current=administrative_binding(instance)
    # Existing scheduler state is root-owned and uses the original binding.
    directory=target['prefix']/'updates/cache'
    if not (directory/'agent.json').exists():
        return {'phase':'absent'}
    client=configured_client(instance)
    agent=Agent(client,Backend(instance,client))
    state=agent.state()
    if state['operation'] is None:return agent.step('status')
    old=_control(protected(target['prefix']/INTENT))['predecessor_job']
    require(state['operation']['id']==old['id'],'repair_scheduler_operation_changed')
    result=agent.resolve(old['id'],'supersede')
    require(agent.state()['operation'] is None,'repair_scheduler_reconciliation_refused')
    return result


def run(instance,candidate,bundle,*,start,resume_agents,report):
    """One checked local invocation; uncertain startup is never retried."""
    import subprocess
    from .pilot_installation import administrative_binding
    from .pilot_operations import _service_state,_systemctl,support
    from .runtime_lifecycle import validate_runtime
    from .updates.executor import controller
    from .updates.recovery import _fresh_running
    target,_,current=administrative_binding(instance)
    require(instance!='owner','repair_owner_instance_excluded')
    require(start is True,'repair_explicit_start_required')
    require(type(resume_agents) is bool,'repair_invalid_agent_choice')
    report=Path(report)
    def save(label,value):_write(report/(label+'.json'),value,private=True)
    def state(unit):
        result=subprocess.run(['/usr/bin/systemctl','show',unit,
            '--property=LoadState,ActiveState,MainPID,UnitFileState'],
            capture_output=True,text=True,timeout=30)
        require(result.returncode==0,'repair_service_state_refused')
        values=dict(line.split('=',1)for line in result.stdout.splitlines()if line)
        if unit.endswith('.timer'):values.setdefault('MainPID','0')
        require(set(values)=={'LoadState','ActiveState','MainPID','UnitFileState'}
            and values['MainPID'].isdigit(),'repair_service_state_refused')
        values['MainPID']=int(values['MainPID']);return values
    with file_lease(target['prefix']/'stopped-repair.lock'):
        service=_service_state(instance);save('service-before',service)
        intent=_control(target['prefix']/INTENT,optional=True)
        if current['bundle_sha256']==candidate['bundle_sha256'] and service['MainPID']:
            require(intent is not None,'repair_existing_intent_requires_review')
            job=_control(target['prefix']/'controller-update.json')
            require(job.get('id')==intent['operation_id'] and job.get('phase')=='complete'
                and job.get('after')==current,'repair_existing_intent_requires_review')
            _fresh_running(target,current)
            return {'ok':True,'phase':'already_running','version':VERSION,
                'start_attempts':0,'service_restarted':False,'state_preserved':True}
        require(service['MainPID']==0 and service['ActiveState'] in ('inactive','failed'),
                'repair_requires_stopped_bot')
        names={key:'poolrotater-v7-'+middle+'-'+instance+suffix for key,middle,suffix in (
            ('updates','updates','.timer'),('recovery','recovery','.timer'),
            ('controller','controller-update','.timer'))}
        timers={key:state(name)for key,name in names.items()};save('timers-before',timers)
        for key,name in names.items():
            if timers[key]['LoadState']!='not-found':_systemctl('stop',name)
        try:
            # Never terminate a committing updater or a dispatched rescue.
            # With the timers stopped, an existing bounded tick may finish.
            until=time.monotonic()+125
            for middle in ('updates','recovery','controller-update'):
                unit='poolrotater-v7-'+middle+'-'+instance+'.service'
                while True:
                    observed=state(unit)
                    if observed['MainPID']==0 and observed['ActiveState'] in ('inactive','failed'):
                        break
                    require(time.monotonic()<until,'repair_background_operation_busy')
                    time.sleep(.5)
            from .network.operations import _state,unit as network_unit
            if _state(instance)['MainPID']:network_unit(instance,'stop')
            intent=_control(target['prefix']/INTENT,optional=True)
            if intent is None:
                job=prepare(instance,candidate,bundle)
            else:
                job=_control(protected(target['prefix']/'controller-update.json'))
                require(intent.get('candidate')==candidate and job.get('id')==intent.get('operation_id')
                    and job.get('before')==intent['successor_job']['before']
                    and job.get('after')==candidate,'repair_existing_intent_requires_review')
                require(job.get('phase')!='complete','repair_completed_stopped_requires_review')
                require(job.get('startup_attempted_at') is None,'repair_start_outcome_requires_review')
            save('admitted-job',job)
            controller(instance,target,job,decision='complete')
            job=_control(protected(target['prefix']/'controller-update.json'))
            require(job.get('phase')=='complete' and job.get('after')==candidate,
                    'repair_completion_requires_review')
            _,_,installed=administrative_binding(instance)
            require(installed==candidate,'repair_registration_changed')
            rows=_fresh_running(target,installed)
            scheduler=reconcile(instance,job);save('scheduler-after',scheduler)
            value=support(instance);save('support-after',value)
            require(value.get('service_state')=='active' and value.get('room_phase')=='running',
                    'repair_readiness_requires_review')
            return {'ok':True,'phase':'started','version':VERSION,'start_attempts':1,
                'state_preserved':True,'previous_update_archived':True,'updater_reconciled':True,
                'rooms':{alias:{'room_id':row.get('room_id'),'version':row.get('version')}
                    for alias,row in rows.items()},'service_enabled':job['service_enabled']}
        finally:
            # Preserve enabled/disabled choices. Failure retains diagnostics;
            # only explicitly requested polling is resumed, never the bot.
            for key,name in names.items():
                if timers[key]['LoadState']!='not-found' and timers[key]['ActiveState']=='active':
                    if key=='recovery' or (key=='updates' and resume_agents):
                        _systemctl('start',name)
