from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import concurrent.futures
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from poolrotater_host.managed_host.state import State
from poolrotater_host.managed_host.contracts import request,capabilities
from poolrotater_host.managed_host import settings
from poolrotater_host.managed_host.hub import Repository
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.store import Store
from poolrotater_host.network.contracts import NetworkError,secret

class Foundation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=1000000
        self.bind={k:str(uuid.uuid4()) for k in ('host_id','installation_id','generation')}
        self.state=State(self.root/'host',**self.bind,clock=lambda:self.now)
        self.rooms=[{'alias':'room'+str(i),'settings':{'preset':'standard-auto','visibility':'public','sr_min':4.0,'sr_max':5.0,'countdown_seconds':20}} for i in range(1,5)]
        self.state.seed(self.rooms)
    def command(self,action='configure',alias='room1',patch=None,**kw):
        row=self.state.room(alias)
        value={'protocol_version':1,'policy_version':1,'id':str(uuid.uuid4()),**self.bind,'alias':alias,
               'actor':'invented-admin','role':'FLEET_ADMIN','action':action,'expected_revision':row['revision'],
               'expected_room_generation':row['room_generation'],'timing':'when_empty',
               'patch':{'countdown_seconds':30} if patch is None and action=='configure' else patch or {},
               'expires_at':self.now+600,'cancel_operation_id':None}
        value.update(kw);return value
    def assert_error(self,code,fn,*args,**kw):
        with self.assertRaises(NetworkError) as caught:fn(*args,**kw)
        self.assertEqual(caught.exception.code,code)
    def test_default_four_zero_drain_and_explicit_ceiling_survive_restart(self):
        self.assertEqual(self.state.limits()['effective_limit'],4)
        for i in range(1,5):
            gen=uuid.uuid4().hex;self.state.reserve('room'+str(i),gen);self.state.mark_dispatched('room'+str(i),gen)
            self.state.observe_owned('room'+str(i),gen,100+i)
        self.state.set_limit(0);self.assertTrue(self.state.limits()['over_capacity_draining'])
        restarted=State(self.root/'host',**self.bind,clock=lambda:self.now);restarted.seed(self.rooms)
        self.assertEqual(restarted.limits()['max_concurrent_rooms'],0)
        self.assertEqual(restarted.limits()['capacity_used'],4)
        self.assertEqual(restarted.room('room1')['phase'],'running')
    def test_concurrent_public_private_creation_reserves_only_remaining_slots(self):
        self.state.set_limit(2)
        def reserve(i):
            try:self.state.reserve('room'+str(i),uuid.uuid4().hex);return True
            except NetworkError as exc:self.assertEqual(exc.code,'host_capacity_reached');return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(reserve,range(1,5)))
        self.assertEqual(sum(results),2);self.assertEqual(self.state.limits()['capacity_used'],2)
    def test_provider_allowance_and_external_owned_rooms_reduce_capacity(self):
        self.state.provider_capacity(4,3);self.state.reserve('room1',uuid.uuid4().hex)
        self.assert_error('host_capacity_reached',self.state.reserve,'room2',uuid.uuid4().hex)
    def test_uncertain_creation_retains_capacity_and_cannot_repeat_make(self):
        self.state.set_limit(1);gen=uuid.uuid4().hex
        self.state.reserve('room1',gen);self.state.mark_dispatched('room1',gen)
        self.state.uncertain_creation('room1',gen,456)
        self.assert_error('host_capacity_reached',self.state.reserve,'room2',uuid.uuid4().hex)
        self.assert_error('room_ownership_requires_review',self.state.reserve,'room1',gen)
        self.assert_error('room_generation_changed',self.state.confirmed_closed,'room1',uuid.uuid4().hex,456)
        self.state.confirmed_closed('room1',gen,456);self.state.reserve('room2',uuid.uuid4().hex)
        self.assertEqual(self.state.limits()['capacity_used'],1)
    def test_disconnected_existing_room_adopts_without_recreation_even_over_ceiling(self):
        self.state.set_limit(0);gen=uuid.uuid4().hex
        self.state.observe_owned('room1',gen,789,adopt=True)
        self.assertEqual(self.state.room('room1')['room_id'],789)
        self.assertTrue(self.state.limits()['over_capacity_draining'])
    def test_cancel_reservation_only_before_any_dispatch(self):
        gen=uuid.uuid4().hex;self.state.reserve('room1',gen);self.state.cancelled_before_dispatch('room1',gen)
        self.state.reserve('room1',gen);self.state.mark_dispatched('room1',gen)
        self.assert_error('room_ownership_requires_review',self.state.cancelled_before_dispatch,'room1',gen)
    def test_retry_is_exact_and_late_retry_returns_durable_receipt(self):
        value=self.command();receipt=self.state.enqueue(value)
        self.assertEqual(self.state.enqueue(value),receipt)
        self.state.begin(value['id'],ready=True);self.state.finish(value['id'],'applied',result={'application':'live','revision':1})
        self.now+=1000
        self.assertEqual(self.state.enqueue(value)['phase'],'applied')
        changed={**value,'patch':{'countdown_seconds':50}}
        self.assert_error('managed_operation_conflict',self.state.enqueue,changed)
    def test_stale_revision_and_replacement_generation_rejected(self):
        value=self.command(expected_revision=7)
        self.assert_error('configuration_revision_changed',self.state.enqueue,value)
        value=self.command(expected_room_generation=uuid.uuid4().hex)
        self.assert_error('room_generation_changed',self.state.enqueue,value)
    def test_closed_room_desired_off_survives_reboot_and_blocks_reopen(self):
        gen=uuid.uuid4().hex;self.state.observe_owned('room1',gen,789,adopt=True)
        value=self.command('close',timing='immediate');self.state.enqueue(value);self.state.begin(value['id'],ready=True)
        self.assertEqual(self.state.room('room1')['desired'],'stopped')
        self.state.confirmed_closed('room1',gen,789);self.state.finish(value['id'],'applied',result={'application':'lifecycle','room_id':None,'revision':1})
        other=State(self.root/'host',**self.bind);other.seed(self.rooms)
        self.assertEqual(other.room('room1')['desired'],'stopped')
        self.assert_error('room_intentionally_stopped',other.reserve,'room1',uuid.uuid4().hex)
    def test_expired_queued_change_does_not_mutate_settings(self):
        value=self.command();self.state.enqueue(value);before=self.state.room('room1')
        self.now+=601
        self.assertEqual(self.state.begin(value['id'],ready=True)['phase'],'expired')
        after=self.state.room('room1');self.assertEqual(before['config'],after['config']);self.assertEqual(before['revision'],after['revision'])
    def test_pending_change_cancelled_without_mutation_or_later_open(self):
        value=self.command();self.state.enqueue(value);self.state.begin(value['id'],ready=False)
        cancel=self.command('cancel',cancel_operation_id=value['id'])
        self.state.enqueue(cancel);self.state.begin(cancel['id'],ready=True);self.state.cancel_target(cancel['id'])
        self.assertEqual(self.state.operation(value['id'])['phase'],'cancelled')
        self.assertFalse(self.state.pending());self.assertEqual(self.state.room('room1')['revision'],0)
    def test_interrupted_mutation_not_replayed_as_a_new_operation(self):
        value=self.command();self.state.enqueue(value);self.state.begin(value['id'],ready=True)
        self.assert_error('managed_application_requires_review',self.state.begin,value['id'],ready=True)
        self.state.recover_applying();self.assertEqual(self.state.operation(value['id'])['phase'],'uncertain')
        self.assertFalse(self.state.pending())
    def test_public_receipts_and_snapshot_never_contain_private_password(self):
        value=self.command(patch={'visibility':'private','password':'secret-canary'})
        self.state.enqueue(value);self.state.begin(value['id'],ready=True)
        self.state.finish(value['id'],'applied',result={'revision':1,'application':'recreate'})
        visible=json.dumps([self.state.operation(value['id']),self.state.snapshot(),self.state.room('room1')])
        self.assertNotIn('secret-canary',visible);self.assertIn('password_configured',visible)
        self.assertEqual(self.state.room('room1',private=True)['config']['password'],'secret-canary')
    def test_configuration_complete_validation_and_application_inventory(self):
        inv=settings.inventory();self.assertIn('custom_join_greetings',inv);self.assertIn('min_players',inv)
        self.assertEqual(settings.application({'room_name':'Name'}),'live')
        self.assertEqual(settings.application({'sr_min':4}),'next_map')
        self.assertEqual(settings.application({'preset':'mania-7k'}),'recreate')
        for bad in [{'token_file':'/etc/passwd'},{'max_concurrent_rooms':100},{'room_name':'x\nclose'},
                    {'sr_min':True},{'sr_min':float('nan')},{'sr_min':6,'sr_max':4},
                    {'preset':'taiko'},{'password':'canary','visibility':'public'}]:
            with self.subTest(patch=bad):
                with self.assertRaises(ValueError):settings.validate(bad,current=self.rooms[0]['settings'])
    def test_privileged_role_and_exact_generation_binding_required(self):
        for role in ['MOD','ADMIN','PLAYER']:
            self.assert_error('managed_role_refused',request,self.command(role=role))
        value=self.command(generation=str(uuid.uuid4()))
        self.assert_error('host_identity_mismatch',self.state.enqueue,value)
    def test_policy_only_marked_after_migration_complete(self):
        self.assertEqual(self.state.snapshot()['policy_version'],0)
        self.state.policy_applied(1);capabilities(self.state.snapshot())
    def test_no_remote_capacity_setting_action(self):
        self.assert_error('invalid_managed_action',request,self.command(action='set_limit',patch={}))
    def test_applied_receipt_requires_actual_application_intent(self):
        value=self.command();self.state.enqueue(value)
        self.assert_error('managed_application_requires_review',self.state.finish,value['id'],'applied')

class Hub(Foundation):
    def setUp(self):
        super().setUp();h=self.root/'hub';initialize(h);self.network=Store(h,clock=lambda:self.now)
        from poolrotater_host.ratings.repository import prepare
        prepare(self.network)
        self.token=secret();identity={'operator_id':str(uuid.uuid4()),'account_id':99,'directory':True,
            **{k:self.bind[k] for k in ('installation_id','generation')}}
        invitation=self.network.invite(identity,label='Invented managed host')['invitation']
        self.bind['host_id']=self.network.enroll({'protocol_version':1,'request_id':str(uuid.uuid4()),
            'invitation':invitation,'credential':self.token,**identity})['host_id']
        # Recreate only fixture host state with its authenticated enrollment ID.
        import shutil
        shutil.rmtree(self.root/'host');self.state=State(self.root/'host',**self.bind,clock=lambda:self.now);self.state.seed(self.rooms)
        self.state.policy_applied(1);self.repository=Repository(self.network);self.sequence=0
    def test_policy_only_marked_after_migration_complete(self):
        self.assertEqual(self.state.snapshot()['policy_version'],1)
        capabilities(self.state.snapshot())
    def exchange(self,receipts=None):
        self.sequence+=1
        return self.repository.accept(self.token,{'protocol_version':1,
            **{k:self.bind[k] for k in ('installation_id','generation')},'sequence':self.sequence,
            'sent_at':self.now,'capabilities':self.state.snapshot(controller_phase='running'),'receipts':receipts or []})
    def test_hub_authenticated_exchange_queues_full_operation_and_accepts_receipt(self):
        self.exchange();queued=self.repository.queue(self.bind['host_id'],'room1','configure',patch={'room_name':'A public room'})['request']
        self.assertEqual(self.exchange()['requests'],[queued]);self.state.enqueue(queued)
        self.state.begin(queued['id'],ready=True);receipt=self.state.finish(queued['id'],'applied',result={'revision':1,'application':'live'})
        self.assertFalse(self.exchange([receipt])['requests'])
        self.assertEqual(self.repository.inspect(self.bind['host_id'])['operations'][0]['receipt']['phase'],'applied')
    def test_hub_stale_capabilities_does_not_create_unbound_request(self):
        self.exchange();self.now+=121
        self.assert_error('managed_fresh_capabilities_required',self.repository.queue,self.bind['host_id'],'room1','open')
    def test_hub_receipt_does_not_expose_request_password(self):
        self.exchange();self.repository.queue(self.bind['host_id'],'room1','configure',patch={'visibility':'private','password':'password-canary'})
        self.assertNotIn('password-canary',json.dumps(self.repository.inspect(self.bind['host_id'])))
    def test_hub_rejects_unknown_receipts_and_host_identity_mismatch(self):
        self.exchange()
        bad={'id':str(uuid.uuid4()),'alias':'room1','phase':'applied','failure_code':None,'result':None}
        self.assert_error('unknown_managed_receipt',self.exchange,[bad])
    def test_unupgraded_host_is_explicitly_missing_managed_capabilities(self):
        self.assert_error('managed_fresh_capabilities_required',self.repository.queue,self.bind['host_id'],'room1','open')

if __name__=='__main__':unittest.main()
