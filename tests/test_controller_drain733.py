"""Controller integration of independently proven native maintenance closures."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from poolrotater_host import controller_update as update
from poolrotater_host import update_drain
from poolrotater_host.update_drain import RECORD


class ControllerDrainIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.target={'root':Path(folder.name),'prefix':Path(folder.name)/'prefix'}
        (self.target['root']/'config').mkdir()
        (self.target['root']/'state').mkdir()
        from poolrotater_host.managed_host.policy import NAME
        (self.target['root']/NAME).write_text('{}')
        self.parent={'service_uid':1000}
        self.job={'rooms':['room1','room2'],'phase':'waiting_empty','release_approval_required':True,
                  'before':self.parent,'after':{},RECORD:{'request_id':'invented'}}
        row={'phase':'parked','operation':'closed','room_id':None,'pid':None,
             'generation':'a'*32,'release':'/retained','ownership_unknown':False,
             'creation_candidate':None,'error':None,'target':None}
        self.rows={a:copy.deepcopy(row)for a in self.job['rooms']}
        self.observed={'generated_at':100.,'rooms':self.rows,'configuration_error':None}
        self.journal={'format':1,'rooms':copy.deepcopy(self.rows)}

    def read(self,path,*args,**kwargs):
        return self.journal if Path(path).name=='ownership.json' else self.observed

    def proof(self):
        return {'room1':{'managed_closed':True}}

    def checked(self,*,after=None):
        with patch('poolrotater_host.update_drain.proof',return_value=self.proof()), \
             patch('poolrotater_host.managed_host.admission.closed_aliases',return_value={'room2'}), \
             patch.object(update,'_control',side_effect=self.read):
            return update._drained_rows(self.target,self.parent,self.job,now=100.,after=after)

    def test_requires_all_aliases_fresh_and_matching_independent_journal(self):
        self.assertEqual(self.checked(),self.rows)
        self.assertIsNone(self.checked(after=100.))
        self.observed['generated_at']=90.;self.assertIsNone(self.checked());self.observed['generated_at']=100.
        self.journal['rooms']['room1']['generation']='different';self.assertIsNone(self.checked())

    def test_unknown_or_residual_owned_room_never_qualifies(self):
        for key,value in [('ownership_unknown',True),('operation','make_pending'),('room_id',5),
                          ('pid',25),('creation_candidate',{}),('error','unknown')]:
            with self.subTest(key=key):
                old=copy.deepcopy(self.journal['rooms']['room1'])
                self.journal['rooms']['room1'][key]=value
                if key=='creation_candidate':
                    from poolrotater_host.config import ConfigError
                    with self.assertRaises(ConfigError):self.checked()
                else:self.assertIsNone(self.checked())
                self.journal['rooms']['room1']=old
        with patch('poolrotater_host.update_drain.proof',return_value=self.proof()), \
             patch('poolrotater_host.managed_host.admission.closed_aliases',return_value=set()), \
             patch.object(update,'_control',side_effect=self.read):
            self.assertIsNone(update._drained_rows(self.target,self.parent,self.job,now=100.))

    def step(self,*,phase='draining',rows=None,gate=True):
        def save(**values):self.job.update(values)
        self.save=save
        with patch('poolrotater_host.update_drain._pending_configuration_work',return_value=[]), \
             patch('poolrotater_host.update_drain.request',return_value={'phase':phase}) as request, \
             patch.object(update,'_drained_rows',return_value=rows) as proof, \
             patch.object(update,'validate_runtime') as validate, \
             patch.object(update,'_gate',return_value=gate) as guard, \
             patch.object(update,'_commit') as commit,patch.object(update,'disarm'):
            accepted=update._step_independent_drain('invented',self.target,self.parent,
                                                   self.job,save,now=100.)
        return accepted,request,proof,validate,guard,commit

    def test_occupied_sibling_keeps_service_and_waits_for_native_closures(self):
        result=self.step(rows=None)
        self.assertTrue(result[0]);result[-1].assert_not_called()
        self.assertEqual(self.job['phase'],'waiting_empty')
        self.assertEqual(self.job['reason'],'draining_each_room_when_empty')

    def test_busy_refresh_retries_without_legacy_all_empty_admission(self):
        from poolrotater_host.layout import LeaseBusy
        def save(**values): self.job.update(values)
        with patch('poolrotater_host.update_drain._pending_configuration_work',return_value=[]), \
             patch('poolrotater_host.update_drain.request',side_effect=LeaseBusy('busy')), \
             patch.object(update,'_gate') as gate, patch.object(update,'_commit') as commit:
            accepted=update._step_independent_drain('invented',self.target,self.parent,
                                                   self.job,save,now=100.)
        self.assertTrue(accepted)
        self.assertEqual(self.job['phase'],'waiting_empty')
        self.assertEqual(self.job['reason'],'waiting_for_scheduled_room_refresh')
        self.assertIsNone(self.job['empty_since'])
        gate.assert_not_called();commit.assert_not_called()

    def test_commit_revalidates_after_hashing_and_after_gate(self):
        result=self.step(phase='complete',rows=self.rows)
        self.assertTrue(result[0]);result[-1].assert_called_once()
        self.assertEqual(result[3].call_count,2)
        self.assertGreaterEqual(result[2].call_count,3)
        self.assertIsNotNone(result[2].call_args.kwargs['after'])
        self.assertEqual(self.job['admission_kind'],'confirmed_maintenance_drained_rooms')
        self.assertEqual(self.job['phase'],'committing')

    def test_booking_gate_refusal_prevents_commit(self):
        result=self.step(phase='complete',rows=self.rows,gate=False)
        result[-1].assert_not_called();self.assertEqual(self.job['phase'],'waiting_empty')

    def test_unsigned_disabled_or_no_eligible_host_preserves_existing_path(self):
        self.job['release_approval_required']=False
        result=self.step();self.assertFalse(result[0]);result[1].assert_not_called()
        self.job['release_approval_required']=True;self.job['independent_drain_disabled']=True
        result=self.step();self.assertFalse(result[0]);result[1].assert_not_called()
        self.job.pop('independent_drain_disabled');result=self.step(phase='no_eligible_rooms')
        self.assertFalse(result[0]);result[-1].assert_not_called()

    def test_uncertain_terminal_receipt_holds_without_stopping_service(self):
        result=self.step(phase='requires_review')
        result[-1].assert_not_called();self.assertEqual(self.job['phase'],'held')
        self.assertEqual(self.job['failure_code'],'update_maintenance_outcome_requires_review')

    def test_transient_stale_status_does_not_hold_or_disarm_active_drain(self):
        from poolrotater_host.network.contracts import NetworkError
        self.job[RECORD]={'request_id':'pinned'}
        def save(**values):self.job.update(values)
        with patch('poolrotater_host.update_drain._pending_configuration_work',
                   side_effect=NetworkError('update_drain_pause_status_stale')), \
             patch.object(update,'disarm') as disarm, \
             patch('poolrotater_host.update_drain.request') as request:
            result=update._step_independent_drain('invented',self.target,self.parent,
                self.job,save,now=100.)
        self.assertTrue(result)
        self.assertEqual(self.job['phase'],'waiting_empty')
        self.assertEqual(self.job['reason'],'waiting_for_fresh_managed_configuration_evidence')
        disarm.assert_not_called();request.assert_not_called()

    def test_pending_managed_operation_blocks_both_drain_and_legacy_empty_gate(self):
        self.job.pop(RECORD,None)
        def save(**values):self.job.update(values)
        with patch('poolrotater_host.update_drain.managed_operations_pending',return_value=True), \
             patch('poolrotater_host.update_drain.request') as request, \
             patch.object(update,'_gate') as gate, \
             patch.object(update,'_rows') as legacy_rows, \
             patch.object(update,'disarm') as disarm:
            result=update._step_independent_drain('invented',self.target,self.parent,
                self.job,save,now=100.)
        self.assertTrue(result)
        self.assertEqual(self.job['reason'],'waiting_for_pending_managed_operation')
        request.assert_not_called();gate.assert_not_called();legacy_rows.assert_not_called()
        disarm.assert_not_called()

    def test_orphan_own_locking_gate_is_cleared_before_pending_work_wait(self):
        from poolrotater_host.controller_update import GATE
        self.job.pop(RECORD,None);self.job['nonce']='f'*32
        self.job['id']='11111111-1111-4111-8111-111111111111'
        self.job['before']=self.parent
        self.job['process']={'pid':4321,'start':'start','boot':'boot'}
        self.parent.update(installation_id='22222222-2222-4222-8222-222222222222',
            generation='33333333-3333-4333-8333-333333333333',bundle_sha256='a'*64,release='/retained')
        self.target['prefix'].mkdir(mode=0o700,parents=True,exist_ok=True)
        gate_path=self.target['root']/GATE;gate_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        from poolrotater_host.layout import atomic_json
        with patch('poolrotater_host.update_drain._process_matches',return_value=True):
            self.job[update_drain.GATE_BASELINE]=update_drain._pre_gate_baseline(
                self.target,self.parent,self.job,self.job['nonce'])
        atomic_json(gate_path,{'format':1,'phase':'locking','nonce':self.job['nonce'],
            'expires_at':200.,'bridge_release':'/retained'})
        cleared=[]
        def clear(target,parent,job,phase):
            if phase is None:
                current=__import__('json').loads(gate_path.read_text())
                if current.get('nonce')==job.get('nonce') and current.get('phase')=='locking':
                    gate_path.unlink();cleared.append(True)
            return True
        def save(**values):self.job.update(values)
        with patch('poolrotater_host.update_drain._process_matches',return_value=True), \
             patch('poolrotater_host.update_drain.managed_operations_pending',return_value=True), \
             patch.object(update,'_gate',side_effect=clear), \
             patch('poolrotater_host.update_drain.request') as request, \
             patch.object(update,'_rows') as legacy_rows, \
             patch.object(update,'disarm') as disarm:
            result=update._step_independent_drain('invented',self.target,self.parent,
                self.job,save,now=100.)
        self.assertTrue(result)
        self.assertFalse(gate_path.exists())
        self.assertEqual(cleared,[True])
        self.assertEqual(self.job['reason'],'waiting_for_pending_managed_operation')
        request.assert_not_called();legacy_rows.assert_not_called();disarm.assert_not_called()

    def test_foreign_orphan_gate_is_not_cleared_or_rewritten(self):
        from poolrotater_host.controller_update import GATE
        self.job.pop(RECORD,None);self.job['nonce']='f'*32
        self.job['id']='11111111-1111-4111-8111-111111111111'
        self.job['before']=self.parent;self.job['process']={'pid':4321,'start':'start','boot':'boot'}
        self.parent.update(installation_id='22222222-2222-4222-8222-222222222222',
            generation='33333333-3333-4333-8333-333333333333',bundle_sha256='a'*64,release='/retained')
        self.target['prefix'].mkdir(mode=0o700,parents=True,exist_ok=True)
        gate_path=self.target['root']/GATE;gate_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        from poolrotater_host.layout import atomic_json
        with patch('poolrotater_host.update_drain._process_matches',return_value=True):
            self.job[update_drain.GATE_BASELINE]=update_drain._pre_gate_baseline(
                self.target,self.parent,self.job,self.job['nonce'])
        atomic_json(gate_path,{'format':1,'phase':'locking','nonce':'e'*32,
            'expires_at':200.,'bridge_release':'/retained'})
        original=gate_path.read_bytes()
        def save(**values):self.job.update(values)
        with patch('poolrotater_host.update_drain._process_matches',return_value=True), \
             patch('poolrotater_host.update_drain.managed_operations_pending',return_value=True), \
             patch.object(update,'_gate') as gate, \
             patch('poolrotater_host.update_drain.request') as request, \
             patch.object(update,'disarm') as disarm:
            result=update._step_independent_drain('invented',self.target,self.parent,
                self.job,save,now=100.)
        self.assertTrue(result)
        self.assertEqual(gate_path.read_bytes(),original)
        gate.assert_not_called();request.assert_not_called();disarm.assert_not_called()

    def test_round_zero_orphan_gate_recovers_timestamp_refresh_but_rejects_changed_terminal(self):
        from poolrotater_host.controller_update import GATE
        from poolrotater_host.operator_maintenance import NAME as MAINTENANCE,RECEIPT
        from poolrotater_host.layout import atomic_json
        self.job.pop(RECORD,None);self.job['nonce']='f'*32
        self.job['id']='11111111-1111-4111-8111-111111111111'
        self.job['before']=self.parent;self.job['process']={'pid':4321,'start':'start','boot':'boot'}
        self.parent.update(installation_id='22222222-2222-4222-8222-222222222222',
            generation='33333333-3333-4333-8333-333333333333',bundle_sha256='a'*64,release='/retained')
        self.target['prefix'].mkdir(mode=0o700,parents=True,exist_ok=True)
        gate_path=self.target['root']/GATE;gate_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        mailbox={'format':1,'id':'44444444-4444-4444-8444-444444444444',
            'installation_id':self.parent['installation_id'],'generation':self.parent['generation'],
            'parent_sha256':self.parent['bundle_sha256'],'phase':'cancelled','mode':'empty',
            'rooms':['room1'],'created_at':1000.,'expires_at':2000.}
        receipt={'format':1,'id':mailbox['id'],'phase':'cancelled','mode':'empty',
            'controller_pid':4321,'updated_at':1100.,'rooms':{'room1':'cancelled'}}
        atomic_json(self.target['prefix']/MAINTENANCE,mailbox)
        receipt_path=self.target['root']/RECEIPT;receipt_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        atomic_json(receipt_path,receipt)
        def read_control(path,owner=0,*,private=False,optional=False,**kwargs):
            path=Path(path)
            if not path.exists():
                if optional:return None
                raise FileNotFoundError(path)
            return __import__('json').loads(path.read_text())
        with patch('poolrotater_host.update_drain._process_matches',return_value=True):
            with patch('poolrotater_host.update_drain._control',side_effect=read_control):
                baseline=update_drain._pre_gate_baseline(self.target,self.parent,self.job,self.job['nonce'])
        self.job[update_drain.GATE_BASELINE]=baseline
        atomic_json(gate_path,{'format':1,'phase':'locking','nonce':self.job['nonce'],
            'expires_at':1200.,'bridge_release':self.parent['release']})
        cleared=[]
        def clear(target,parent,job,phase):
            if phase is None:
                gate_path.unlink();cleared.append(True)
            return True
        def save(**values):self.job.update(values)
        with patch('poolrotater_host.update_drain._process_matches',return_value=True), \
             patch('poolrotater_host.update_drain._control',side_effect=read_control), \
             patch('poolrotater_host.update_drain.managed_operations_pending',return_value=True), \
             patch.object(update,'_gate',side_effect=clear), \
             patch('poolrotater_host.update_drain.request') as request:
            result=update._step_independent_drain('invented',self.target,self.parent,
                self.job,save,now=100.)
        self.assertTrue(result);self.assertEqual(cleared,[True]);self.assertFalse(gate_path.exists())
        request.assert_not_called()

        # A terminal republish changes only observation time. It remains the
        # same exact prior receipt, so recover the owned unpublished gate.
        gate_path.write_text(json.dumps({'format':1,'phase':'locking','nonce':self.job['nonce'],
            'expires_at':1200.,'bridge_release':self.parent['release']}));gate_path.chmod(0o600)
        changed={**receipt,'updated_at':1101.};atomic_json(receipt_path,changed)
        with patch('poolrotater_host.update_drain._process_matches',return_value=True), \
             patch('poolrotater_host.update_drain._control',side_effect=read_control), \
             patch('poolrotater_host.update_drain.managed_operations_pending',return_value=True), \
             patch.object(update,'_gate',side_effect=clear) as gate, \
             patch('poolrotater_host.update_drain.request') as request:
            result=update._step_independent_drain('invented',self.target,self.parent,
                self.job,save,now=100.)
        self.assertTrue(result);self.assertFalse(gate_path.exists())
        gate.assert_called_once();self.assertEqual(cleared,[True,True]);request.assert_not_called()

        # A semantic room-outcome change is still foreign to the pinned
        # terminal receipt and must leave the gate in place.
        gate_path.write_text(json.dumps({'format':1,'phase':'locking','nonce':self.job['nonce'],
            'expires_at':1200.,'bridge_release':self.parent['release']}));gate_path.chmod(0o600)
        altered={**changed,'rooms':{'room1':'closed'}};atomic_json(receipt_path,altered)
        with patch('poolrotater_host.update_drain._process_matches',return_value=True), \
             patch('poolrotater_host.update_drain._control',side_effect=read_control), \
             patch('poolrotater_host.update_drain.managed_operations_pending',return_value=True), \
             patch.object(update,'_gate') as gate, \
             patch('poolrotater_host.update_drain.request') as request:
            result=update._step_independent_drain('invented',self.target,self.parent,
                self.job,save,now=100.)
        self.assertTrue(result);self.assertTrue(gate_path.exists())
        gate.assert_not_called();request.assert_not_called()

    def test_stale_status_during_pause_restore_retries_without_review_hold(self):
        from poolrotater_host.network.contracts import NetworkError
        self.job[RECORD]={'request_id':'pinned'}
        self.job['controller_update_drain_pause']={'request_id':'pinned'}
        def save(**values):self.job.update(values)
        with patch('poolrotater_host.update_drain.advance_configuration_pause',
                   side_effect=NetworkError('update_drain_pause_status_stale')), \
             patch.object(update,'disarm') as disarm:
            result=update._step_independent_drain('invented',self.target,self.parent,
                self.job,save,now=100.)
        self.assertTrue(result)
        self.assertEqual(self.job['phase'],'waiting_empty')
        self.assertEqual(self.job['reason'],'waiting_for_fresh_configuration_pause_evidence')
        disarm.assert_not_called()

    def test_first_pause_attempt_transient_stale_evidence_retries_without_hold(self):
        from poolrotater_host.network.contracts import NetworkError
        self.job[RECORD]={'request_id':'pinned'}
        work=[{'id':'managed-op','alias':'room4'}]
        def save(**values):self.job.update(values)
        with patch('poolrotater_host.update_drain._pending_configuration_work',return_value=work), \
             patch('poolrotater_host.update_drain.advance_configuration_pause',
                   side_effect=NetworkError('update_drain_pause_status_stale')), \
             patch.object(update,'disarm') as disarm:
            result=update._step_independent_drain('invented',self.target,self.parent,
                self.job,save,now=100.)
        self.assertTrue(result)
        self.assertEqual(self.job['phase'],'waiting_empty')
        self.assertEqual(self.job['controller_update_drain_pause']['phase'],'cancelling')
        self.assertEqual(self.job['reason'],'waiting_for_fresh_configuration_pause_evidence')
        disarm.assert_not_called()


class CanceledDrainRestorationTests(unittest.TestCase):
    setUp=ControllerDrainIntegrationTests.setUp
    def test_restoration_waits_then_finishes_without_service_restart(self):
        self.job.update(phase='cancelled',configuration={})
        def save(**values):self.job.update(values)
        with patch('poolrotater_host.update_drain.cancel',return_value={'phase':'complete'}) as cancel, \
             patch('poolrotater_host.update_drain.restore',side_effect=[{'phase':'restoring'},{'phase':'complete'}]) as restore, \
             patch.object(update,'_configuration_unprepared',return_value=True), \
             patch.object(update,'_commit') as commit:
            self.assertTrue(update._restore_canceled_drain('invented',self.target,self.parent,self.job,save))
            self.assertTrue(update._drain_restoration_needed(self.job))
            self.assertTrue(update._restore_canceled_drain('invented',self.target,self.parent,self.job,save))
        self.assertFalse(update._drain_restoration_needed(self.job))
        self.assertEqual(restore.call_count,2);self.assertEqual(cancel.call_count,2)
        commit.assert_not_called()

    def test_post_admission_or_foreign_registration_cannot_restore(self):
        self.job.update(phase='held',commit_admitted_at=99.)
        self.assertFalse(update._drain_restoration_needed(self.job))
        self.job.pop('commit_admitted_at')
        def save(**values):self.job.update(values)
        with patch('poolrotater_host.update_drain.restore') as restore:
            self.assertTrue(update._restore_canceled_drain('invented',self.target,{'foreign':True},self.job,save))
        self.assertEqual(self.job['drain_restoration_phase'],'requires_review')
        restore.assert_not_called()
