"""Real lifecycle leases around managed preflight, boundaries and dispatch."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tests.test_managed_foundation as foundation
from poolrotater_host.layout import file_lease, LeaseBusy
from poolrotater_host.refresh_fence import LOCK, refresh_fence
from poolrotater_host.managed_host.native import NativeAdapter
from poolrotater_host.managed_host.engine import Engine, Deferred, BeforeMutationError


class Serialization(unittest.TestCase):
    command=foundation.Foundation.command
    def setUp(self):
        foundation.Foundation.setUp(self)
        (self.state.root/'config').mkdir(mode=0o700)
        self.runtime=self.root/'runtime'
        (self.runtime/'state/rolling').mkdir(parents=True,mode=0o700)
        (self.runtime/'config').mkdir(mode=0o700)
        # Enable the actual booking admission lease without a booking database.
        (self.runtime/'config/private-bookings.json').write_text('{}')
        self.observations=[];self.journal={'ownership_unknown':False,'phase':'stopped'}
        def snapshot():
            self.observations.append('read')
            return {'rooms':{'room1':dict(self.journal),'room2':dict(self.journal)}}
        self.owner=SimpleNamespace(runtime=self.runtime,configs={'room1':{},'room2':{}},
            workers={},config_loader=lambda:None,registry=SimpleNamespace(snapshot=snapshot),
            status_lock=threading.Lock(),stopping=threading.Event(),rollout_alias=None,targets={})
        self.adapter=NativeAdapter(SimpleNamespace(owner=self.owner,state=self.state))
        self.addCleanup(self.adapter.release)
        self.value=self.command()
    def assert_unlocked(self):
        with file_lease(self.runtime/'state/booking-admission.lock'):
            with refresh_fence(self.runtime) as allowed:self.assertTrue(allowed)
    def test_refresh_busy_defers_before_temporary_journal_is_interpreted(self):
        self.journal.update(phase='recovery_required',ownership_unknown=True)
        with refresh_fence(self.runtime):
            with self.assertRaises(Deferred):self.adapter.preflight(self.value)
            self.assertEqual(self.observations,[])
            # Failed refresh-lock acquisition must release booking admission.
            with file_lease(self.runtime/'state/booking-admission.lock'):pass
        self.journal.update(phase='stopped',ownership_unknown=False)
        self.adapter.preflight(self.value)
        self.assertEqual(self.observations,['read'])
        self.adapter.release();self.assert_unlocked()
    def test_managed_preflight_and_apply_exclude_refresh_and_booking(self):
        self.adapter.preflight(self.value)
        for path in (LOCK,'state/booking-admission.lock'):
            with self.subTest(path=path),self.assertRaises(LeaseBusy):
                with file_lease(self.runtime/path):pass
        self.assertTrue(self.adapter.admit(self.value))
        self.assertEqual(self.owner.rollout_alias,'room1')
        self.adapter.release();self.adapter.release()
        self.assertIsNone(self.owner.rollout_alias);self.assert_unlocked()
    def test_busy_booking_does_not_acquire_refresh(self):
        with file_lease(self.runtime/'state/booking-admission.lock'):
            with self.assertRaises(Deferred):self.adapter.preflight(self.value)
            with refresh_fence(self.runtime) as allowed:self.assertTrue(allowed)
        self.assertEqual(self.observations,[]);self.assert_unlocked()
    def test_engine_busy_request_remains_pending_and_applies_once_after_refresh(self):
        self.state.enqueue(self.value);calls=[]
        def apply(value):
            with self.assertRaises(LeaseBusy):
                with refresh_fence(self.runtime):pass
            calls.append(value['id'])
            return {'application':'live','revision':1}
        self.adapter.apply=apply;engine=Engine(self.state,self.adapter)
        with refresh_fence(self.runtime):
            self.assertIsNone(engine.step())
            self.assertEqual(self.state.operation(self.value['id'])['phase'],'waiting_boundary')
            self.assertEqual(calls,[])
        self.assertEqual(engine.step()['phase'],'applied')
        self.assertEqual(calls,[self.value['id']]);self.assertIsNone(engine.step())
        self.assert_unlocked()
    def test_waiting_sibling_does_not_retain_leases(self):
        self.state.enqueue(self.value);self.now+=1
        second=self.command(alias='room2');self.state.enqueue(second)
        native_ready=self.adapter.ready
        self.adapter.ready=lambda value:False if value['alias']=='room1' else native_ready(value)
        self.adapter.apply=lambda value:{'application':'live','revision':1}
        result=Engine(self.state,self.adapter).step()
        self.assertEqual(result['id'],second['id']);self.assertEqual(result['phase'],'applied')
        self.assertEqual(self.state.operation(self.value['id'])['phase'],'waiting_boundary')
        self.assert_unlocked()
    def test_ownership_refusal_releases_preflight_locks(self):
        self.journal.update(ownership_unknown=True,phase='recovery_required')
        self.state.enqueue(self.value)
        result=Engine(self.state,self.adapter).step()
        self.assertEqual(result['failure_code'],'room_ownership_requires_review');self.assert_unlocked()
    def test_apply_failure_is_uncertain_without_retained_leases_or_replay(self):
        self.state.enqueue(self.value)
        def fail(value):raise RuntimeError('invented provider failure')
        self.adapter.apply=fail;engine=Engine(self.state,self.adapter)
        self.assertEqual(engine.step()['phase'],'uncertain');self.assert_unlocked()
        self.assertIsNone(engine.step())
    def test_admission_rechecks_stop_and_other_rollout_without_deadlock(self):
        for field,value in (('rollout_alias','room2'),('stopping',threading.Event())):
            with self.subTest(field=field):
                if field=='stopping':value.set()
                self.adapter.preflight(self.value);setattr(self.owner,field,value)
                self.assertFalse(self.adapter.admit(self.value));self.assert_unlocked()
                self.owner.rollout_alias=None;self.owner.stopping=threading.Event()
    def test_catalog_booking_hold_releases_both_leases(self):
        value={**self.value,'action':'catalog'}
        with patch('poolrotater_host.bookings.update_guard.holds_alias',return_value=True):
            self.assertFalse(self.adapter.admit(value))
        self.assert_unlocked()

    def test_capacity_reconciliation_uses_same_leases_without_external_operation(self):
        self.assertTrue(self.adapter.admit({'alias':'room1'}))
        with self.assertRaises(LeaseBusy):
            with refresh_fence(self.runtime):pass
        self.adapter.release();self.assert_unlocked()
