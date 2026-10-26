"""Only a fully proven terminal precommit failure can reuse closed-room admission."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from contextlib import ExitStack,nullcontext
from copy import deepcopy
import hashlib
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from poolrotater_host import controller_update as update
# Import before patching layout.load so dependent modules never capture a mock.
from poolrotater_host.managed_host import policy, projection  # noqa: F401
from poolrotater_host.network.contracts import fingerprint


class _Rows:
    def __init__(self,rows):self.rows=rows
    def fetchall(self):return list(self.rows)
    def __iter__(self):return iter(self.rows)


class _DB:
    def __init__(self,fixture):self.fixture=fixture
    def execute(self,sql,*args):
        if 'FROM reservations' in sql:return _Rows(self.fixture.reservations)
        if 'FROM operations' in sql:return _Rows(self.fixture.live_operations)
        raise AssertionError('unexpected SQL in failed-controller proof')


class _Store:
    path=Path('/invented/managed-host.sqlite3')
    binding={'host_id':str(uuid.UUID('10000000-0000-4000-8000-000000000001')),
             'installation_id':str(uuid.UUID('20000000-0000-4000-8000-000000000002')),
             'generation':str(uuid.UUID('30000000-0000-4000-8000-000000000003'))}
    def __init__(self,fixture):self.fixture=fixture
    def snapshot(self):return {'policy_version':1,'rooms':deepcopy(self.fixture.managed_rows)}
    def limits(self):return {'capacity_used':len(self.fixture.reservations)}
    def pending(self):return deepcopy(self.fixture.pending)


class ClosedFailedControllerProofTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name);self.root=base/'host';self.root.mkdir()
        self.prefix=base/'prefix';self.prefix.mkdir()
        (self.root/'state').mkdir();(self.root/'state/managed-host-policy.json').write_text('{}\n')
        self.installation=str(uuid.UUID('20000000-0000-4000-8000-000000000002'))
        self.generation=str(uuid.UUID('30000000-0000-4000-8000-000000000003'))
        self.boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.parent={'installation_id':self.installation,'generation':self.generation,
            'bundle_sha256':'a'*64,'prefix':str(self.prefix),'host_root':str(self.root),
            'service_uid':1000,'execution':'pilot-live',
            'release':str(self.prefix/'releases'/'v7.3.0-aaaaaaaaaaaaaaaa')}
        self.aliases=['room1','room2','room3','room4']
        self.host={'rooms':[{'alias':alias,'preset':'standard-fixed'} for alias in self.aliases]}
        self.projection={'format':1,'installation_id':self.installation,'generation':self.generation,
            'rooms':{alias:{'revision':(4 if alias=='room1' else 2 if alias=='room2' else 0),
                'desired':'running' if alias in ('room1','room2') else 'stopped',
                'settings':{'preset':'standard-fixed','visibility':'public'}} for alias in self.aliases}}
        self.configuration={'config/host.json':'1'*64,'config/rooms/room1.json':'2'*64}
        self.now=time.time();self.monotonic=time.monotonic();ticks=os.sysconf('SC_CLK_TCK')
        self.queued_at=self.now-50.
        self.process={'pid':4321,'start':str(int((self.monotonic-100.)*ticks)),'boot':self.boot}
        self.start_monotonic=int(int(self.process['start'])/ticks*1_000_000)
        self.exit_monotonic=int((self.monotonic-10.)*1_000_000)
        self.same_process=False
        self.unit='poolrotater-v7-pilot-fixture.service'
        self.control_group='/system.slice/'+self.unit
        self.systemd_exec_pid=self.process['pid']
        self.job_id=str(uuid.UUID('40000000-0000-4000-8000-000000000004'))
        self.request_id=str(uuid.uuid5(uuid.UUID(self.job_id),'poolrotater-empty-room-drain-v1'))
        self.initial={}
        for alias,room_id,generation,revision in (
                ('room1',501,'worker-generation-1',4),('room2',502,'worker-generation-2',2)):
            identity={'pid':1000+room_id,'generation':generation,'room_id':room_id,
                      'release':self.parent['release']}
            self.initial[alias]={'identity':identity,
                'managed':{'desired':'running','revision':revision,'phase':'running',
                    'room_id':room_id,'room_generation':'managed-'+generation,
                    'pending_operation':None,'preset':'standard-fixed','visibility':'public'},
                'reservation':{'generation':generation,'room_id':room_id,
                    'phase':'owned','operation':None},'closure_started_at':None}
        created=self.queued_at+1.
        self.drain={'format':1,'job_id':self.job_id,'request_id':self.request_id,
            'installation_id':self.installation,'generation':self.generation,
            'parent_sha256':self.parent['bundle_sha256'],'mode':'empty',
            'rooms':['room1','room2'],'created_at':created,'expires_at':created+86400.,
            'configuration_sha256':fingerprint(self.configuration),
            'projection_sha256':fingerprint(self.projection),'eligibility_sha256':'f'*64,
            'projection':deepcopy(self.projection),'initial':deepcopy(self.initial),'round':0}
        self.job={'format':1,'id':self.job_id,'phase':'held','reason':'controller_identity_changed',
            'before':self.parent,'after':{'bundle_sha256':'b'*64,'installation_id':self.installation,
                'generation':self.generation,'prefix':str(self.prefix),'host_root':str(self.root),
                'service_uid':1000,'execution':'pilot-live'},
            'archive':str(self.prefix/('controller-'+'b'*64+'.zip')),
            'process':self.process,'rooms':self.aliases,'configuration':self.configuration,
            'managed_before':deepcopy(self.projection),'queued_at':self.queued_at,'nonce':'d'*32,
            'controller_update_drain':deepcopy(self.drain),
            'automatic_recovery':{'format':1,'attempts':0}}
        self.store=_Store(self)
        self.managed_rows=[]
        self.owners={'format':1,'rooms':{}}
        for alias in self.aliases:
            base=self.projection['rooms'][alias]
            self.managed_rows.append({'alias':alias,'desired':base['desired'],
                'revision':base['revision'],'applied_revision':base['revision'],
                'phase':'stopped','room_id':None,'room_generation':None,
                'pending_operation':None,'failure_code':None,
                'preset':base['settings']['preset'],'visibility':'public'})
            generation=(self.initial[alias]['identity']['generation']
                        if alias in self.initial else 'off-'+alias)
            owner={'phase':'parked','operation':'closed','room_id':None,'pid':None,
                'generation':generation,'release':self.parent['release'],
                'ownership_unknown':False,'creation_candidate':None,'creation_verification_failure':None,
                'target':None,'error':None,'admission_locked':False,'updated_at':created+20.}
            self.owners['rooms'][alias]=owner
        self.provider={'format':1,'execution':'provider-live-pilot','phase':'failed',
            'pid':self.process['pid'],'uid':1000,'installation_id':self.installation,
            'generation':self.generation,'live_ready':False,'failure_code':'service_failed',
            'workers_reaped':True,'referee_child_reaped':True}
        self.failure={'format':1,'at':created+20.,'pid':self.process['pid'],'failure_code':'service_failed'}
        self.request={'format':1,'id':self.request_id,'installation_id':self.installation,
            'generation':self.generation,'parent_sha256':self.parent['bundle_sha256'],
            'phase':'requested','mode':'empty','rooms':['room1','room2'],
            'created_at':created,'expires_at':created+86400.}
        self.progress={'format':1,'id':self.request_id,'phase':'draining','mode':'empty',
            'controller_pid':self.process['pid'],'updated_at':created+30.,
            'rooms':{'room1':'waiting_empty','room2':'closed'},'closure_evidence':{
                'room2':{'room_id':self.initial['room2']['identity']['room_id'],'mode':'empty',
                    'notice_confirmed':False,'active_map_observed':False,'require_empty':True,
                    'closure_started_at':created+20.}}}
        self.service={'MainPID':0,'ActiveState':'failed'}
        self.reservations=[];self.live_operations=[];self.pending=[];self.booking_held=False

    def _control(self,path,*args,**kwargs):
        name=Path(path).name
        if Path(path)==self.prefix/'controller-update.json':return deepcopy(self.job)
        values={'provider-service.json':self.provider,'controller-failure.json':self.failure,
            'ownership.json':self.owners,
            'supervisor-maintenance.json':None}
        if Path(path)==self.prefix/'operator-maintenance.json':return deepcopy(self.request)
        if str(path).endswith('state/rolling/operator-maintenance.json'):
            return deepcopy(self.progress)
        if str(path).endswith('state/rolling/supervisor-maintenance.json'):
            return None
        if name in values:return deepcopy(values[name])
        if kwargs.get('optional'):return None
        raise AssertionError('unexpected control read '+str(path))

    def patches(self,*,archive=False):
        stack=ExitStack()
        stack.enter_context(patch('poolrotater_host.pilot_installation._quiescent',
            side_effect=lambda *a,**k:nullcontext()))
        stack.enter_context(patch('poolrotater_host.ratings.operations._identity',
            side_effect=lambda *a,**k:nullcontext()))
        stack.enter_context(patch('poolrotater_host.layout.load',
            return_value=({'format':1,'installation_id':self.installation,
                'generation':self.generation},self.host)))
        stack.enter_context(patch('poolrotater_host.managed_host.policy.state',return_value=self.store))
        stack.enter_context(patch('poolrotater_host.managed_host.projection.read',
            return_value=deepcopy(self.projection)))
        stack.enter_context(patch('poolrotater_host.reconciliation._journal',
            return_value=deepcopy(self.owners)))
        stack.enter_context(patch('poolrotater_host.diagnostic_logs.common.transaction',
            side_effect=lambda path:_TxnContext(self)))
        stack.enter_context(patch.object(update,'_control',side_effect=self._control))
        stack.enter_context(patch('poolrotater_host.pilot_operations._service_state',return_value=self.service))
        stack.enter_context(patch.object(update,'_process',
            side_effect=(lambda pid:deepcopy(self.process)) if self.same_process else FileNotFoundError))
        systemd={'LoadState':'loaded','ActiveState':'failed','SubState':'failed','MainPID':'0',
            'ExecMainPID':str(self.systemd_exec_pid),'Result':'exit-code','ExecMainStatus':'1',
            'ExecMainStartTimestampMonotonic':str(self.start_monotonic),
            'ExecMainExitTimestampMonotonic':str(self.exit_monotonic),
            'ControlGroup':self.control_group,
            'InvocationID':'e'*32}
        stack.enter_context(patch('poolrotater_host.pilot_operations._systemctl',
            return_value='\n'.join(k+'='+v for k,v in systemd.items()).encode()))
        stack.enter_context(patch('poolrotater_host.updates.native.require_idle_worker_deployment'))
        stack.enter_context(patch('poolrotater_host.bookings.policy.holds_updates',
            side_effect=lambda root:self.booking_held))
        stack.enter_context(patch('poolrotater_host.update_drain._configuration_sha',
            return_value=fingerprint(self.configuration)))
        stack.enter_context(patch('poolrotater_host.update_drain._eligibility_sha',return_value='f'*64))
        stack.enter_context(patch.object(update,'_configuration_unprepared',return_value=True))
        stack.enter_context(patch.object(update,'_configuration_snapshot',return_value={
            'configuration':self.configuration,'rooms':self.aliases,
            'managed_before':deepcopy(self.projection)}))
        if archive:
            raw=b'exact signed candidate archive fixture'
            digest=hashlib.sha256(raw).hexdigest()
            self.job['after']['bundle_sha256']=digest
            self.job['archive']=str(self.prefix/('controller-'+digest+'.zip'))
            Path(self.job['archive']).write_bytes(raw)
            stack.enter_context(patch.object(update,'protected',side_effect=lambda p:p))
        return stack

    def test_terminal_failed_controller_and_closed_drain_prove_without_stopped_host(self):
        # This mirrors the actual v7.3.0 case: no controller-failure.json and
        # systemd has removed the already-empty service cgroup.
        self.failure=None;self.control_group=''
        with self.patches():
            with patch('poolrotater_host.managed_host.admission.stopped_host',
                       side_effect=AssertionError('desired-running rooms must not be forced off')):
                proof=update._closed_failed_controller_proof('fixture',
                    {'root':self.root,'prefix':self.prefix,'unit':self.unit},self.parent,self.job,now=self.now)
        self.assertEqual(proof['closed_aliases'],self.aliases)
        self.assertEqual(proof['drain_request_id'],self.request_id)
        self.assertEqual(proof['process'],self.process)

    def test_post_native_stop_reuses_persisted_invocation_and_rechecks_live_closure(self):
        target={'root':self.root,'prefix':self.prefix,'unit':self.unit}
        with self.patches():
            proof=update._closed_failed_controller_proof('fixture',target,self.parent,self.job,now=self.now)
        self.job.update(phase='recovering',admission_kind='confirmed_closed_controller_failure',
                        commit_started_at=self.now+1,precommit_controller_failure_proof=proof,
                        precommit_controller_failure_proof_sha256=fingerprint(proof))
        self.service={'MainPID':0,'ActiveState':'inactive'}
        # Native stop may replace/remove both predecessor receipts. Recovery
        # must use the persisted invocation proof while re-reading current
        # managed closure, ownership, configuration and booking state.
        self.provider=None;self.failure=None
        with self.patches():
            replay=update._closed_failed_controller_proof('fixture',target,self.parent,self.job,
                now=self.now+2,committing=True,after_native_stop=True)
        self.assertEqual(replay,proof)

        for mutate in (
                lambda value:value['provider_service_failure']['systemd_invocation'].
                    update(invocation_id='f'*32),
                lambda value:value['provider_service_failure'].update(pid=9999)):
            with self.subTest(mutate=mutate),self.patches():
                altered=deepcopy(self.job);mutate(altered['precommit_controller_failure_proof'])
                self.assertIsNone(update._closed_failed_controller_proof('fixture',target,
                    self.parent,altered,now=self.now+2,committing=True,after_native_stop=True))

        self.owners['rooms']['room1']['ownership_unknown']=True
        with self.patches():
            self.assertIsNone(update._closed_failed_controller_proof('fixture',target,
                self.parent,self.job,now=self.now+2,committing=True,after_native_stop=True))

    def test_exact_proof_commits_only_after_gate_revalidation_and_approval(self):
        events=[]
        def gate(*args):events.append('gate');return True
        def commit(instance,target,parent,job,save):
            events.append('commit');save(phase='complete')
        def approval(target,job):events.append('approval')
        with self.patches(archive=True),patch.object(update,'_gate',side_effect=gate),\
             patch.object(update,'_commit',side_effect=commit),\
             patch('poolrotater_host.updates.recovery._approval',side_effect=approval),\
             patch('poolrotater_host.bookings.policy.holds_updates',return_value=False),\
             patch.object(update,'disarm'):
            self.assertTrue(update._step_precommit_controller_failure('fixture',
                {'root':self.root,'prefix':self.prefix,'unit':self.unit},self.parent,self.job,
                lambda **values:self.job.update(values)))
        self.assertLess(events.index('approval'),events.index('gate'))
        self.assertLess(events.index('gate'),events.index('commit'))
        self.assertEqual(self.job['admission_kind'],'confirmed_closed_controller_failure')
        self.assertEqual(self.job['phase'],'complete')
        self.assertIsInstance(self.job['commit_admitted_at'],float)

    def test_helper_interruption_before_quiescence_keeps_proof_backed_recovery_admission(self):
        target={'root':self.root,'prefix':self.prefix,'unit':self.unit}
        def interrupted(*args,**kwargs):raise KeyboardInterrupt()
        with self.patches(archive=True),patch.object(update,'_gate',return_value=True),\
             patch('poolrotater_host.updates.recovery._approval'),\
             patch('poolrotater_host.bookings.policy.holds_updates',return_value=False),\
             patch.object(update,'_commit',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                update._step_precommit_controller_failure('fixture',target,self.parent,self.job,
                    lambda **values:self.job.update(values))
        from poolrotater_host.update_lifecycle import automatic_eligible
        self.assertEqual(self.job['phase'],'committing')
        self.assertIsInstance(self.job.get('commit_admitted_at'),float)
        self.assertTrue(automatic_eligible(self.job))
        with self.patches():
            replay=update._closed_failed_controller_proof('fixture',target,self.parent,self.job,
                now=time.time(),committing=True,after_native_stop=True)
        self.assertEqual(replay,self.job['precommit_controller_failure_proof'])

    def test_step_autonomously_retries_the_same_held_job_without_stopped_host(self):
        target={'root':self.root,'prefix':self.prefix,'unit':self.unit};events=[]
        def gate(*args):events.append('gate');return True
        def commit(instance,target,parent,job,save):
            events.append('commit');save(phase='complete')
        with self.patches(archive=True),\
             patch.object(update,'_binding',return_value=(target,{},None,self.parent)),\
             patch.object(update,'file_lease',side_effect=lambda *a,**k:nullcontext()),\
             patch.object(update,'_write'),patch.object(update,'_gate',side_effect=gate),\
             patch.object(update,'_commit',side_effect=commit),\
             patch('poolrotater_host.updates.recovery._approval'),\
             patch.object(update,'disarm'),\
             patch('poolrotater_host.managed_host.admission.stopped_host',
                   side_effect=AssertionError('must preserve desired-running intent')):
            result=update.step('fixture')
        self.assertEqual(result['phase'],'complete')
        self.assertEqual(events,['gate','commit'])
        self.assertEqual([room['desired'] for room in self.job['managed_before']['rooms'].values()],
                         ['running','running','stopped','stopped'])

    def test_controller_process_reuse_or_changed_failure_receipt_refuses(self):
        cases=(('same-process-live',lambda:setattr(self,'same_process',True)),
               ('different-failure-pid',lambda:self.failure.update(pid=9999)),
               ('failure-predates-queue',lambda:self.failure.update(at=899.)),
               ('systemd-exec-pid-mismatch',lambda:setattr(self,'systemd_exec_pid',9999)),
               ('service-restarted',lambda:self.service.update(MainPID=5432,ActiveState='active')))
        for label,mutate in cases:
            with self.subTest(label=label):
                self.setUp();mutate()
                with self.patches():
                    self.assertIsNone(update._closed_failed_controller_proof('fixture',
                        {'root':self.root,'prefix':self.prefix,'unit':self.unit},self.parent,self.job,now=self.now),label)

    def test_unreaped_children_unknown_owner_pending_operation_or_booking_refuses(self):
        for label,mutate in (
            ('worker-not-reaped',lambda:self.provider.update(workers_reaped=False)),
            ('referee-not-reaped',lambda:self.provider.update(referee_child_reaped=False)),
            ('owner-unknown',lambda:self.owners['rooms']['room1'].update(ownership_unknown=True)),
            ('owner-error',lambda:self.owners['rooms']['room1'].update(error='failure')),
            ('owner-creation',lambda:self.owners['rooms']['room1'].update(creation_candidate={'id':'unknown'})),
            ('pending-op',lambda:self.live_operations.append({'id':'operation'})),
            ('reservation',lambda:self.reservations.append({'alias':'room1'})),
            ('booking-held',lambda:setattr(self,'booking_held',True)),
            ('desired-changed',lambda:self.managed_rows[0].update(desired='stopped')),
            ('managed-failure',lambda:self.managed_rows[0].update(failure_code='owner_failed')),
            ('projection-changed',lambda:self.projection['rooms']['room1'].update(revision=9))):
            with self.subTest(label=label):
                self.setUp();mutate()
                with self.patches():
                    self.assertIsNone(update._closed_failed_controller_proof('fixture',
                        {'root':self.root,'prefix':self.prefix,'unit':self.unit},self.parent,self.job,now=self.now))

    def test_candidate_approval_or_configuration_drift_refuses_before_commit(self):
        with self.patches(archive=True),patch.object(update,'_gate') as gate,\
             patch.object(update,'_commit') as commit,\
             patch('poolrotater_host.updates.recovery._approval',side_effect=ValueError('expired')):
            self.assertTrue(update._step_precommit_controller_failure('fixture',
                {'root':self.root,'prefix':self.prefix,'unit':self.unit},self.parent,self.job,
                lambda **values:self.job.update(values)))
        gate.assert_not_called();commit.assert_not_called()
        self.assertNotIn('commit_started_at',self.job)

    def test_unstable_or_changed_current_configuration_refuses_before_gate(self):
        self.configuration={'config/host.json':'9'*64}
        with self.patches(archive=True),patch.object(update,'_gate') as gate,\
             patch.object(update,'_commit') as commit,\
             patch('poolrotater_host.updates.recovery._approval') as approval:
            self.assertFalse(update._step_precommit_controller_failure('fixture',
                {'root':self.root,'prefix':self.prefix,'unit':self.unit},self.parent,self.job,
                lambda **values:self.job.update(values)))
        gate.assert_not_called();commit.assert_not_called();approval.assert_not_called()

    def test_live_or_replaced_controller_never_uses_failure_path_from_step(self):
        for current_service,current_parent in (
            ({'MainPID':777,'ActiveState':'active'},self.parent),
            ({'MainPID':0,'ActiveState':'failed'},dict(self.parent,bundle_sha256='c'*64))):
            with self.subTest(service=current_service,current=current_parent['bundle_sha256']):
                self.setUp();written=[]
                target={'root':self.root,'prefix':self.prefix,'unit':self.unit}
                with patch.object(update,'_binding',
                           return_value=(target,{},None,current_parent)),\
                     patch.object(update,'file_lease',side_effect=lambda *a,**k:nullcontext()),\
                     patch.object(update,'_control',return_value=self.job),\
                     patch('poolrotater_host.pilot_operations._service_state',return_value=current_service),\
                     patch.object(update,'_write',side_effect=lambda path,value:written.append(deepcopy(value))),\
                     patch.object(update,'_step_precommit_controller_failure') as attempt,\
                     patch.object(update,'disarm') as disarm:
                    result=update.step('fixture')
                attempt.assert_not_called();disarm.assert_called_once()
                self.assertEqual(result['phase'],'held')
                self.assertEqual(written,[])


class _TxnContext:
    def __init__(self,fixture):self.fixture=fixture
    def __enter__(self):return _DB(self.fixture)
    def __exit__(self,*args):return False


if __name__=='__main__':unittest.main()
