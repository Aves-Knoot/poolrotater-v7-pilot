from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,json,tempfile,types,unittest,uuid
import threading,time
from pathlib import Path
from unittest.mock import patch
from poolrotater_host.managed_host import catalog_rollout as rollout
from poolrotater_host.managed_host.catalog_fleet import review
from poolrotater_host.managed_host.contracts import request,digest
from poolrotater_host.managed_host.hub import Repository
from poolrotater_host.network.contracts import NetworkError
from tests.test_managed_foundation import Foundation
from tests.test_catalog_store import StoreFixture,repository
from poolrotater_host.catalog_export import inspect_bundle
from poolrotater_host.layout import atomic_json

class CatalogState(Foundation):
    def catalog(self,**kw):return self.command('catalog',patch={'mode':'standard','content_id':'a'*64},**kw)
    def test_catalog_preserves_stopped_intent_settings_ratings(self):
        close=self.command('close');self.state.enqueue(close);self.state.begin(close['id'],ready=True)
        self.state.finish(close['id'],'applied',result={'application':'lifecycle','room_id':None,'revision':1})
        before=self.state.room('room1',private=True)
        self.assertEqual(before['desired'],'stopped')
        value=self.catalog(expires_at=self.now+86400);self.state.enqueue(value)
        self.state.begin(value['id'],ready=True)
        result=self.state.finish(value['id'],'applied',result={'application':'recreate','revision':before['revision'],'room_id':None,'catalog_content_id':'a'*64})
        after=self.state.room('room1',private=True)
        self.assertEqual(after['config'],before['config']);self.assertEqual(after['desired'],before['desired'])
        self.assertEqual(after['revision'],before['revision']);Repository._receipt(result)
    def test_only_empty_timing_allowed(self):
        for timing in ('live','immediate','next_map'):
            with self.subTest(timing=timing),self.assertRaises(NetworkError):request(self.catalog(timing=timing))
    def test_no_paths_urls_or_code_in_request(self):
        for extra in ({'path':'/etc/passwd'},{'origin':'https://evil.test'},{'command':'sh'},{'force':True}):
            value=self.catalog();value['patch'].update(extra)
            with self.subTest(extra=extra),self.assertRaises(NetworkError):request(value)
    def test_bad_hash_mode_and_foreign_binding(self):
        for key,value in (('mode','unsupported'),('content_id','../bad'),('content_id','A'*64),('content_id',123)):
            cmd=self.catalog();cmd['patch'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(NetworkError):request(cmd)
        cmd=self.catalog();cmd['generation']=str(uuid.uuid4())
        self.assert_error('host_identity_mismatch',self.state.enqueue,cmd)
    def test_ordinary_operation_cannot_extend_expiry(self):
        self.assert_error('operation_expired',self.state.enqueue,self.command(expires_at=self.now+86400))
    def test_receipt_requires_exact_request_digest_and_target(self):
        cmd=self.catalog();receipt={'format':1,'request_sha256':digest(cmd),'phase':'prepared','content_id':'a'*64,'updated_at':self.now}
        self.assertEqual(rollout.validate_receipt(cmd,receipt),receipt)
        for key,value in (('request_sha256','b'*64),('content_id','b'*64),('phase','installed')):
            modified=dict(receipt);modified[key]=value
            with self.subTest(key=key),self.assertRaises(NetworkError):rollout.validate_receipt(cmd,modified)
    def test_corrupt_result_hash_never_acknowledged(self):
        result={'id':str(uuid.uuid4()),'alias':'room1','phase':'applied','failure_code':None,
                'result':{'application':'recreate','catalog_content_id':'bad'}}
        with self.assertRaises(NetworkError):Repository._receipt(result)
    def test_prepare_commits_only_exact_requested_signed_mode(self):
        self.check_prepare()
    def test_wrong_published_target_is_held_without_commit(self):
        self.check_prepare(target='b'*64,fail=True)
    def test_other_modes_cannot_be_replaced(self):
        self.check_prepare(other='b'*64,fail=True)
    def test_noop_already_current_does_not_commit_again(self):
        self.check_prepare(current='a'*64,commits=0)
    def check_prepare(self,target='a'*64,other='c'*64,current='d'*64,fail=False,commits=1):
        value=self.catalog();directory=self.root/'prefix';directory.mkdir()
        parent={k:self.bind[k] for k in ('installation_id','generation')}
        class Store:
            def __init__(self):self.ref={'modes':{'standard':current,'mania4':'c'*64}};self.commits=0
            def current(self):return copy.deepcopy(self.ref)
            def stage(self,repo,modes):
                assert modes==['standard']
                self.staged={'modes':{'standard':target,'mania4':other}};return self.staged
            def commit(self):self.ref=self.staged;self.commits+=1
        store=Store()
        kwargs=dict(store=store,repository=object(),clock=lambda:self.now)
        with patch.object(rollout.os,'geteuid',return_value=0),patch('poolrotater_host.provisioning.protected',side_effect=lambda p:p):
            if fail:
                with self.assertRaises(NetworkError):rollout.prepare_one({'prefix':directory},parent,value,**kwargs)
                self.assertEqual(store.commits,0)
                self.assertEqual(json.loads(rollout.receipt_path(directory,value).read_text())['phase'],'review_required')
            else:
                result=rollout.prepare_one({'prefix':directory},parent,value,**kwargs)
                self.assertEqual(result['phase'],'prepared');self.assertEqual(store.commits,commits)
        self.assertEqual(store.ref['modes']['mania4'],'c'*64)

class FleetGate(unittest.TestCase):
    def setUp(self):
        self.now=1000;self.hosts=[];self.reports={};self.ids=[]
        for i in range(3):
            hid=str(uuid.uuid4());installation=str(uuid.uuid4());generation=str(uuid.uuid4());self.ids.append(hid)
            self.hosts.append(dict(id=hid,revoked=0,received=990,installation_id=installation,generation=generation,
                heartbeat=json.dumps({'code_version':'v7.2.14','rooms':[{'alias':'room1','code_version':'v7.2.14','observed_at':990}]})))
            self.reports[hid]={'received':990,'capabilities':{'controller_phase':'running','installation_id':installation,
                'generation':generation,'rooms':[{'alias':'room1','desired':'running','phase':'running',
                'pending_operation':None,'failure_code':None}]}}
    def check(self):return review(self.hosts,self.reports,expected_hosts=self.ids,required_version='v7.2.14',now=self.now)
    def test_all_hosts_and_workers_verified(self):self.assertEqual(len(self.check()),3)
    def test_missing_host_report_holds_everything(self):
        self.reports.pop(self.ids[-1])
        with self.assertRaises(NetworkError):self.check()
    def test_old_controller_holds_everything(self):
        self.hosts[-1]['heartbeat']=json.dumps({'code_version':'v7.2.13','rooms':[]})
        with self.assertRaises(NetworkError):self.check()
    def test_old_worker_holds_everything(self):
        self.hosts[-1]['heartbeat']=json.dumps({'code_version':'v7.2.14','rooms':[{'alias':'room1','code_version':'v7.2.13'}]})
        with self.assertRaises(NetworkError):self.check()
    def test_stale_worker_observation_holds_even_with_fresh_host(self):
        self.hosts[-1]['heartbeat']=json.dumps({'code_version':'v7.2.14','rooms':[{'alias':'room1','code_version':'v7.2.14','observed_at':0}]})
        with self.assertRaises(NetworkError):self.check()
    def test_operator_cannot_gate_on_pre_feature_version(self):
        with self.assertRaises(NetworkError):review(self.hosts,self.reports,expected_hosts=self.ids,required_version='v7.2.13',now=self.now)
    def test_stale_future_and_failed_host_hold(self):
        for change in ('stale','future','failed'):
            with self.subTest(change=change):
                old=copy.deepcopy(self.reports[self.ids[0]])
                row=self.reports[self.ids[0]]
                if change=='stale':row['received']=0
                elif change=='future':row['received']=1001
                else:row['capabilities']['controller_phase']='failed'
                with self.assertRaises(NetworkError):self.check()
                self.reports[self.ids[0]]=old
    def test_enrollment_changed_holds_inventory(self):
        self.hosts[-1]['revoked']=1
        with self.assertRaises(NetworkError):self.check()
    def test_pending_room_operation_blocks_batch(self):
        self.reports[self.ids[-1]]['capabilities']['rooms'][0]['pending_operation']=str(uuid.uuid4())
        with self.assertRaises(NetworkError):self.check()
    def test_stopped_slot_does_not_need_worker_or_reopen(self):
        self.reports[self.ids[-1]]['capabilities']['rooms'][0].update(desired='stopped',phase='stopped')
        self.hosts[-1]['heartbeat']=json.dumps({'code_version':'v7.2.14','rooms':[]})
        self.assertEqual(len(self.check()),3)

class NativeBoundary(unittest.TestCase):
    def setUp(self):
        from poolrotater_host.managed_host.native import NativeAdapter
        self.worker=types.SimpleNamespace(process=types.SimpleNamespace(poll=lambda:None),
            status={'ready':True},fresh=lambda:True,idle_empty=lambda:False,empty_since=time.monotonic()-60)
        self.owner=types.SimpleNamespace(stopping=threading.Event(),rollout_alias=None,targets={},
            protected_parent={'prefix':'/unused'},runtime='/unused',workers={'room1':self.worker},empty_seconds=5)
        state=types.SimpleNamespace()
        self.adapter=NativeAdapter(types.SimpleNamespace(owner=self.owner,state=state))
        self.value={'action':'catalog','alias':'room1','timing':'when_empty'}
    def test_occupied_worker_never_ready(self):
        with patch('poolrotater_host.managed_host.catalog_rollout.ready',return_value=True):
            self.assertFalse(self.adapter.ready(self.value))
    def test_empty_worker_ready_only_after_existing_delay(self):
        self.worker.idle_empty=lambda:True
        with patch('poolrotater_host.managed_host.catalog_rollout.ready',return_value=True):
            self.assertTrue(self.adapter.ready(self.value))
            self.worker.empty_since=time.monotonic();self.assertFalse(self.adapter.ready(self.value))
    def test_booked_empty_room_never_ready_for_catalog_handoff(self):
        self.worker.idle_empty=lambda:True
        with patch('poolrotater_host.managed_host.catalog_rollout.ready',return_value=True), \
                patch('poolrotater_host.bookings.update_guard.holds_alias',return_value=True):
            self.assertFalse(self.adapter.ready(self.value))

    def test_catalog_admission_rechecks_reservation_under_shared_mutex(self):
        from contextlib import contextmanager
        entered=[]
        @contextmanager
        def admission(root):
            entered.append('locked')
            try:yield
            finally:entered.append('unlocked')
        self.owner.status_lock=threading.Lock()
        with patch('poolrotater_host.bookings.admission.update_admission',admission), \
                patch('poolrotater_host.bookings.update_guard.holds_alias',return_value=True):
            self.assertFalse(self.adapter.admit(self.value))
        self.assertEqual(entered,['locked','unlocked']);self.assertIsNone(self.owner.rollout_alias)
        entered.clear()
        with patch('poolrotater_host.bookings.admission.update_admission',admission), \
                patch('poolrotater_host.bookings.update_guard.holds_alias',return_value=False):
            self.assertTrue(self.adapter.admit(self.value))
            self.assertEqual(entered,['locked'])
            self.adapter.release()
        self.assertEqual(entered,['locked','unlocked']);self.assertIsNone(self.owner.rollout_alias)

    def test_no_prepared_root_receipt_never_ready(self):
        self.worker.idle_empty=lambda:True
        with patch('poolrotater_host.managed_host.catalog_rollout.ready',return_value=False):
            self.assertFalse(self.adapter.ready(self.value))
    def test_other_rollout_blocks_catalog_handoff(self):
        self.worker.idle_empty=lambda:True;self.owner.rollout_alias='room2'
        with patch('poolrotater_host.managed_host.catalog_rollout.ready',return_value=True):
            self.assertFalse(self.adapter.ready(self.value))
    def test_stopped_slot_does_not_prepare_or_create_worker(self):
        self.owner.workers={};self.adapter.state.room=lambda alias:{'desired':'stopped','revision':3}
        value={**self.value,'patch':{'mode':'standard','content_id':'a'*64}}
        result=self.adapter.apply(value)
        self.assertEqual(result['room_id'],None);self.assertEqual(self.owner.workers,{})

class SignedIntegration(StoreFixture,unittest.TestCase):
    def test_real_signed_stage_preserves_old_pins_other_modes_and_rating_bytes(self):
        old=self.install();before={m:self.store.version(m,c)[0] for m,c in old['modes'].items()}
        ratings=self.root/'state/controller-ratings.db';ratings.write_bytes(b'untouched rating sentinel')
        directory=self.base/'next';directory.mkdir();_,bundles=repository(directory,3)
        for bundle in bundles:self.repo.add_bundle(bundle)
        self.repo.publish(bump=True)
        target=inspect_bundle(bundles[0])['content_id']
        binding=self.store.binding
        cmd={'protocol_version':1,'policy_version':1,'id':str(uuid.uuid4()),'host_id':str(uuid.uuid4()),
             'installation_id':binding['installation_id'],'generation':binding['generation'],'alias':'room1',
             'actor':'test-admin','role':'FLEET_ADMIN','action':'catalog','expected_revision':0,
             'expected_room_generation':None,'timing':'when_empty','patch':{'mode':'standard','content_id':target},
             'expires_at':2000,'cancel_operation_id':None}
        prefix=self.base/'prefix';prefix.mkdir()
        def private_write(path,value,**kw):atomic_json(path,value)
        with patch.object(rollout,'administrator'),patch.object(rollout,'atomic_json',side_effect=private_write),patch('poolrotater_host.provisioning.protected',side_effect=lambda p:p):
            result=rollout.prepare_one({'prefix':prefix},binding,cmd,store=self.store,repository=self.repo.folder,clock=lambda:1000)
        self.assertEqual(result['phase'],'prepared')
        after=self.store.current();self.assertEqual(after['modes']['standard'],target)
        for mode in ('mania4','mania7'):self.assertEqual(after['modes'][mode],old['modes'][mode])
        for mode,path in before.items():self.assertTrue((path/'catalog.db').is_file())
        self.assertEqual(ratings.read_bytes(),b'untouched rating sentinel')
