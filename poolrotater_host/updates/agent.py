"""Independent durable update scheduler. Never blindly replays an installation.

The backend is the installed native lifecycle adapter, not a shell command from
the hub. Only verified runtime archives cross that boundary. Failed operations
are held until explicit operator recovery, while checks use bounded backoff.
"""
import random
import time
import uuid

from ..config import ConfigError
from ..layout import atomic_json, file_lease
from .client import read
from .contracts import CHANNELS, fields, integer, require, timestamp, version, identifier, digest, release

PHASES = ('idle','available','downloaded','staging','dispatching','waiting_empty',
          'installed','held','retrying','paused','controller_consent_required')


def policy(value, binding):
    fields(value, ('format','binding','channel','automatic_download','automatic_install',
                   'controller_updates','paused','check_seconds','report_to_hub'))
    require(type(value['format']) is int and value['format'] == 1 and value['binding'] == binding,
            'update_policy_binding_refused')
    require(value['channel'] in CHANNELS, 'update_channel_refused')
    for name in ('automatic_download','automatic_install','controller_updates','paused','report_to_hub'):
        require(type(value[name]) is bool, 'update_consent_required')
    require(not value['automatic_install'] or value['automatic_download'], 'update_download_consent_required')
    integer(value['check_seconds'], 300, 86400)
    return value


def configure(client, *, channel='pilot', automatic_download=True, automatic_install=True,
              controller_updates=True, paused=False, check_seconds=1800, report_to_hub=True):
    value = policy({'format':1,'binding':client.binding,'channel':channel,
        'automatic_download':automatic_download,'automatic_install':automatic_install,
        'controller_updates':controller_updates,'paused':paused,'check_seconds':check_seconds,
        'report_to_hub':report_to_hub},client.binding)
    with file_lease(client.root/'agent.lock'):
        atomic_json(client.root/'policy.json',value)
    return value


class Agent:
    def __init__(self, client, backend, *, clock=time.time, jitter=random.uniform):
        self.client=client;self.backend=backend;self.clock=clock;self.jitter=jitter
        self.path=client.root/'agent.json'

    def state(self):
        value=read(self.path,optional=True)
        if value is None:
            return {'format':1,'binding':self.client.binding,'phase':'idle','operation':None,
                'error':None,'failures':0,'next_check_at':0,'clock_high_water':0,'last_check_at':None,
                'installed_version':None,'waiting_rooms':[],'last_completed':None,
                'last_reviewed':None,'reviewed_failure_sequence':0,'resolution':None}
        # Earlier format1 states have no terminal-review extension. Defaults
        # preserve their accepted transaction and never manufacture a decision.
        value.setdefault('last_reviewed',None);value.setdefault('reviewed_failure_sequence',0)
        value.setdefault('resolution',None)
        fields(value,('format','binding','phase','operation','error','failures','next_check_at',
                      'clock_high_water','last_check_at','installed_version','waiting_rooms','last_completed',
                      'last_reviewed','reviewed_failure_sequence','resolution'))
        require(type(value['format'])is int and value['format']==1 and value['binding']==self.client.binding
                and value['phase'] in PHASES, 'update_agent_state_refused')
        integer(value['failures'],0,10);timestamp(value['next_check_at']);timestamp(value['clock_high_water'])
        if value['last_check_at']is not None:timestamp(value['last_check_at'])
        if value['installed_version']is not None:version(value['installed_version'])
        require(type(value['waiting_rooms'])is list and len(value['waiting_rooms'])<=50
                and all(type(a)is str and len(a)<=64 for a in value['waiting_rooms']), 'update_agent_state_refused')
        integer(value['reviewed_failure_sequence'],0)
        if value['resolution']is not None:
            fields(value['resolution'],('operation_id','decision'))
            identifier(value['resolution']['operation_id'])
            require(value['resolution']['decision']in('retry','cancel','complete','rollback'), 'update_resolution_refused')
            require(value['operation']is not None and value['resolution']['operation_id']==value['operation']['id'],
                    'update_resolution_operation_changed')
        for operation in (value['operation'],value['last_completed'],value['last_reviewed']):
            if operation is None:continue
            require(type(operation)is dict and set(operation)in(
                {'id','release','queued_at','before_sha256'},
                {'id','release','queued_at','before_sha256','effective_before_sha256'}), 'update_operation_refused')
            identifier(operation['id']);release(operation['release']);timestamp(operation['queued_at'])
            digest(operation['before_sha256'])
            if 'effective_before_sha256'in operation:digest(operation['effective_before_sha256'])
        return value

    def _save(self, state, **values):
        state.update(values);atomic_json(self.path,state)

    def step(self, action='tick'):
        require(action in ('tick','check','download','apply','status','pause','resume','recover'),
                'update_action_refused')
        with file_lease(self.client.root/'agent.lock'):
            state=self.state();now=timestamp(self.clock())
            require(now>=state['clock_high_water'],'update_clock_rollback')
            consent=policy(read(self.client.root/'policy.json'),self.client.binding)
            if action=='status':return self.summary(state,consent)
            self._save(state,clock_high_water=now)
            if action in ('pause','resume'):
                consent['paused']=action=='pause';atomic_json(self.client.root/'policy.json',consent)
                return self.summary(state,consent)
            # A recorded accepted operation is reconciled before any new feed
            # selection. Pause stops future submissions, not an in-flight commit.
            if state['operation']:
                operation=state['operation']
                if action in('check','download'):
                    try:
                        current=self.backend.current()
                        self.client.check(consent['channel'],current['effective_sha256'],
                                          baselines=[current['controller_sha256']])
                        if action=='download':self.client.download(operation['release'])
                        self._save(state,last_check_at=now,error=None,failures=0)
                    except Exception:
                        self._save(state,error='update_check_or_download_failed')
                    return self.summary(state,consent)
                try:
                    result=self.backend.observe(operation)
                    phase=result['phase']
                    if state['phase']=='held' and action=='recover' and phase=='absent':
                        require(self.backend.retryable(operation)is True,'update_native_recovery_required')
                        self._save(state,phase='staging',error=None)

                    if phase=='complete':
                        self.client.installed(operation['release'])
                        self._save(state,phase='installed',operation=None,error=None,waiting_rooms=[],
                            last_completed=operation,resolution=None,installed_version=operation['release']['version'],
                            next_check_at=now+consent['check_seconds'])
                        return self.summary(state,consent)
                    if (phase=='cancelled' and state['resolution']is not None
                            and state['resolution']['decision']in('cancel','rollback')):
                        self._reviewed(state,operation,now,consent)
                        return self.summary(state,consent)
                    if phase in ('failed','held','cancelled','conflict'):
                        self._save(state,phase='held',error='update_installation_requires_review')
                        return self.summary(state,consent)
                    if phase=='absent' and state['phase'] not in ('staging','dispatching'):
                        self._save(state,phase='held',error='update_deployment_receipt_missing')
                        return self.summary(state,consent)
                    if phase!='absent':
                        self._save(state,phase='waiting_empty',waiting_rooms=result.get('waiting_rooms',[]))
                        # Controller admission is one bounded native step per
                        # timer tick. Native interrupted commits are held.
                        if operation['release']['kind']=='controller':self.backend.advance(operation)
                        return self.summary(state,consent)
                    # Queue may have been interrupted before its durable write.
                    # Idempotent operation UUID is reused, never regenerated.
                    if state['phase']=='held':return self.summary(state,consent)
                    require(not consent['paused'], 'update_paused')
                    path,item=self.client.artifact(operation['release'])
                    require(item==operation['release'],'update_pending_artifact_changed')
                    self.backend.stage(path,item)
                    self._save(state,phase='dispatching')
                    self.backend.queue(path,item,operation['id'])
                    self._save(state,phase='waiting_empty')
                    return self.summary(state,consent)
                except Exception:
                    # A lifecycle exception can follow a durable mutation. Do
                    # not classify it as a harmless network retry or auto-clear.
                    self._save(state,phase='held',error='update_installation_requires_review')
                    return self.summary(state,consent)
            if state['phase']=='held' and action not in('recover','check','download','apply'):
                if state['error']!='update_release_requires_manual_retry' or now<state['next_check_at']:
                    return self.summary(state,consent)
            if action=='recover':
                require(not state['operation'],'update_native_recovery_required')
                self._save(state,phase='idle',error=None,next_check_at=0)
            if consent['paused'] and action=='tick':
                self._save(state,phase='paused');return self.summary(state,consent)
            if action=='tick' and now<state['next_check_at']:return self.summary(state,consent)
            try:
                current=self.backend.current()
                # Never overwrite an unrelated operator/native deployment.
                if current.get('deployment_busy'):
                    self._save(state,next_check_at=now+60,error='update_other_deployment_pending')
                    return self.summary(state,consent)
                checked=self.client.check(consent['channel'],current['effective_sha256'],
                                          baselines=[current['controller_sha256']])
                self._save(state,phase=checked['phase'] if checked['phase'] in PHASES else 'idle',
                    installed_version=current.get('version'),last_check_at=now,error=None,failures=0,
                    next_check_at=now+consent['check_seconds']+self.jitter(0,60))
                if checked['available_version'] is None or action=='check':return self.summary(state,consent)
                if action in ('download','apply') or consent['automatic_download']:
                    self.client.download();self._save(state,phase='downloaded')
                else:return self.summary(state,consent)
            except Exception:
                # Transport/signature failures have no lifecycle authority and
                # never reach the room service. Fixed codes avoid private leaks.
                failures=min(state['failures']+1,10)
                self._save(state,phase='retrying',error='update_check_or_download_failed',failures=failures,
                    next_check_at=now+min(3600,30*2**(failures-1))+self.jitter(0,30))
                return self.summary(state,consent)
            if action=='download' or not (action=='apply' or consent['automatic_install']):
                return self.summary(state,consent)
            if current.get('service_active')is False and current.get('managed_stopped') is not True:
                # Stopped hosts can still inspect/download. Applying a native
                # controller requires the running empty-room admission protocol;
                # this timer never starts rooms just to install a release.
                self._save(state,error='update_host_not_running',next_check_at=now+60)
                return self.summary(state,consent)
            if consent['paused']:
                self._save(state,phase='paused');return self.summary(state,consent)
            try:
                path,item=self.client.artifact()
                require(current.get('version')is None or version(item['version'])>=version(current['version']),
                        'update_installed_version_downgrade')
            except ConfigError:
                self._save(state,phase='held',error='update_installation_requires_review')
                return self.summary(state,consent)
            if current.get('service_active') is False and item['kind']!='controller':
                self._save(state,error='update_stopped_host_requires_controller_release',next_check_at=now+60)
                return self.summary(state,consent)
            if action!='apply' and item['sequence']<=state['reviewed_failure_sequence']:
                self._save(state,phase='held',error='update_release_requires_manual_retry')
                return self.summary(state,consent)
            if item['kind']=='controller' and not consent['controller_updates']:
                self._save(state,phase='controller_consent_required');return self.summary(state,consent)
            operation={'id':str(uuid.uuid4()),'release':item,'queued_at':now,
                       'before_sha256':current['controller_sha256'],
                       'effective_before_sha256':current['effective_sha256']}
            self._save(state,phase='staging',operation=operation)
            try:
                self.backend.stage(path,item)
                self._save(state,phase='dispatching')
                self.backend.queue(path,item,operation['id'])
                self._save(state,phase='waiting_empty')
            except Exception:
                self._save(state,phase='held',error='update_installation_requires_review')
            return self.summary(state,consent)

    def _reviewed(self,state,operation,now,consent,*,decision=None):
        # Keep operation history and suppress automatic retry of this release or
        # an older intermediate. A newer signed release remains eligible. The
        # operator can explicitly apply the same release again after review.
        history=self.client.root/'history'/(operation['id']+'.review.json')
        receipt={'operation':operation,'decision':decision or state['resolution']['decision'],'reviewed_at':now}
        previous=read(history,optional=True)
        if previous is None:atomic_json(history,receipt)
        else:require(previous['operation']==operation and previous['decision']==receipt['decision'],
                     'update_review_receipt_changed')
        self._save(state,phase='idle',operation=None,resolution=None,last_reviewed=operation,
            reviewed_failure_sequence=max(state['reviewed_failure_sequence'],operation['release']['sequence']),
            error=None,waiting_rooms=[],next_check_at=now+consent['check_seconds'])

    def resolve(self,operation_id,decision):
        identifier(operation_id)
        require(decision in('review','retry','cancel','complete','rollback','supersede'),'update_recovery_decision_refused')
        with file_lease(self.client.root/'agent.lock'):
            state=self.state();operation=state['operation'];now=timestamp(self.clock())
            require(now>=state['clock_high_water'],'update_clock_rollback')
            consent=policy(read(self.client.root/'policy.json'),self.client.binding)
            if operation is None:
                reviewed=state['last_reviewed'];completed=state['last_completed']
                if reviewed and reviewed['id']==operation_id:
                    receipt=read(self.client.root/'history'/(operation_id+'.review.json'))
                    require(receipt['decision']==decision,'update_recovery_decision_changed')
                    return {**self.summary(state,consent),'replayed':True}
                if completed and completed['id']==operation_id and decision=='complete':
                    return {**self.summary(state,consent),'replayed':True}
            require(operation is not None and operation['id']==operation_id,'update_recovery_operation_changed')
            if decision=='review':
                return {**self.summary(state,consent),'recovery':self.backend.recover(operation,'review')}
            if decision=='supersede':
                # No native mutation is dispatched. Do not persist a new
                # resolution enum an older installed scheduler cannot read.
                try:
                    result=self.backend.recover(operation,decision)
                    require(result['phase']=='superseded','update_superseded_receipt_required')
                    history=self.client.root/'history'/(operation_id+'.superseded.json')
                    evidence={'operation':operation,'result':result}
                    previous=read(history,optional=True)
                    if previous is None:atomic_json(history,evidence)
                    else:require(previous==evidence,'update_superseded_evidence_changed')
                    state['installed_version']=result['installed_version']
                    state['clock_high_water']=now
                    self._reviewed(state,operation,now,consent,decision=decision)
                except Exception:
                    self._save(state,phase='held',error='update_installation_requires_review')
                return self.summary(state,consent)
            self._save(state,clock_high_water=now,resolution={'operation_id':operation_id,'decision':decision})
            try:
                if decision=='retry':
                    require(self.backend.retryable(operation)is True,'update_native_recovery_required')
                    require(not consent['paused'],'update_paused')
                    path,item=self.client.artifact(operation['release']);require(item==operation['release'],'update_pending_artifact_changed')
                    self._save(state,phase='staging',error=None)
                    self.backend.stage(path,item);self._save(state,phase='dispatching')
                    self.backend.queue(path,item,operation_id);self._save(state,phase='waiting_empty',resolution=None)
                else:
                    result=self.backend.recover(operation,decision)
                    if result['phase']=='cancelled':self._reviewed(state,operation,now,consent)
                    elif result['phase']=='complete':
                        self.client.installed(operation['release'])
                        self._save(state,phase='installed',operation=None,resolution=None,last_completed=operation,
                            installed_version=operation['release']['version'],error=None,waiting_rooms=[],
                            next_check_at=now+consent['check_seconds'])
                    else:self._save(state,phase='waiting_empty',error=None)
            except Exception:
                self._save(state,phase='held',error='update_installation_requires_review')
            return self.summary(state,consent)

    def summary(self,state=None,consent=None):
        state=self.state() if state is None else state
        consent=policy(read(self.client.root/'policy.json'),self.client.binding) if consent is None else consent
        return {'ok':True,'phase':state['phase'],'channel':consent['channel'],
            'paused':consent['paused'],'automatic_download':consent['automatic_download'],
            'automatic_install':consent['automatic_install'],'controller_updates':consent['controller_updates'],
            'installed_version':state['installed_version'],'error':state['error'],
            'next_check_at':state['next_check_at'],'last_check_at':state['last_check_at'],
            'waiting_rooms':state['waiting_rooms'],'operation_id':(state['operation']or{}).get('id'),
            'pending_version':(state['operation']or{}).get('release',{}).get('version'),
            'resolution':state['resolution']['decision']if state['resolution'] else None,
            'last_reviewed_version':(state['last_reviewed']or{}).get('release',{}).get('version'),
            'next_action':('inspect_native_deployment_and_ownership_then_explicit_recovery' if state['phase']=='held'
                else 'grant_controller_update_consent_or_keep_current_release' if state['phase']=='controller_consent_required'
                else 'waiting_for_native_empty_room_admission' if state['phase']=='waiting_empty'
                else 'resume_when_ready' if consent['paused'] else 'scheduled_or_manual_check'),
            **{k:v for k,v in self.client.summary().items() if k not in ('ok','phase')}}
