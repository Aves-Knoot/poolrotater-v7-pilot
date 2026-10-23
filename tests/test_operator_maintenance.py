"""Controller-owned one-map/empty maintenance, without provider credentials."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
from contextlib import nullcontext
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock,patch

from bot.api import ApiAdmissionDeferred,ApiChatDeliveryUncertain
from poolrotater_host.config import ConfigError
from poolrotater_host import operator_maintenance as operations
from poolrotater_host.layout import atomic_json,read_json,setup,load
from rolling.operator_maintenance import WorkerDrain


class CloseRpcTests(unittest.TestCase):
    def test_deferred_close_remains_disconnected_across_real_rpc_without_uncertain_reclassification(self):
        from rolling.adapters import call
        from rolling.broker import InfrastructureCloseDeferred
        from rolling.protocol import Peer
        from bot.hub import HubDisconnected,HubInvocationUncertain
        left,right=socket.socketpair();calls=[]
        def refuse(method,*args,**kwargs):
            calls.append(method)
            raise InfrastructureCloseDeferred('roster changed before dispatch')
        server=Peer(left,handler=refuse).start();client=Peer(right).start()
        try:
            with self.assertRaises(HubDisconnected) as error:
                call(client,'hub.invoke','CloseRoom',[101])
            self.assertNotIsInstance(error.exception,HubInvocationUncertain)
            self.assertEqual(calls,['hub.invoke'])
        finally:client.close();server.close()


def job(mode='one-map'):
    return dict(format=1,id=str(uuid.uuid4()),installation_id='installation',generation='generation',
        parent_sha256='a'*64,phase='requested',mode=mode,rooms=['room1'],created_at=time.time(),expires_at=time.time()+3600)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.worker=WorkerDrain()
        self.bot=SimpleNamespace(running=True,_room_ready=True,_hub_rejoin_pending=False,
            hub=Mock(),events=queue.Queue(),channel_id=10,room_id=1,state='PLAYING',players={7:True},
            _deliver_room_chat=Mock(),handle_event=Mock())
        self.bot.hub.is_connected.return_value=True
        self.job=job();self.worker.command(self.job)

    def test_confirmed_warning_current_match_then_scoring_then_idle(self):
        self.assertTrue(self.worker.tick(self.bot))
        self.assertTrue(self.worker.status()['notice_confirmed'])
        self.assertEqual(self.bot._deliver_room_chat.call_count,1)
        self.bot.state='SCORING';self.assertTrue(self.worker.tick(self.bot))
        self.bot.state='IDLE';self.assertFalse(self.worker.tick(self.bot))
        self.assertEqual(self.worker.phase,'ready')
        self.bot.hub.close_room.assert_not_called()

    def test_idle_or_scoring_request_waits_for_a_subsequent_real_map(self):
        for initial in ('IDLE','SCORING','STARTING'):
            self.worker=WorkerDrain();self.worker.command(self.job);self.bot.state=initial
            self.assertTrue(self.worker.tick(self.bot));self.bot.state='IDLE'
            self.assertTrue(self.worker.tick(self.bot));self.bot.state='PLAYING'
            self.assertTrue(self.worker.tick(self.bot));self.bot.state='IDLE'
            self.assertFalse(self.worker.tick(self.bot))

    def test_unknown_warning_is_never_replayed_and_never_arms_closure(self):
        self.bot._deliver_room_chat.side_effect=ApiChatDeliveryUncertain('invented')
        self.assertTrue(self.worker.tick(self.bot));self.bot.state='IDLE'
        self.assertTrue(self.worker.tick(self.bot))
        self.assertEqual(self.worker.phase,'notice_uncertain')
        self.assertEqual(self.bot._deliver_room_chat.call_count,1)

    def test_refused_notice_keeps_gameplay_and_reports_only_safe_failure_metadata(self):
        from poolrotater_host.oauth import AuthError
        self.bot._deliver_room_chat.side_effect=AuthError('provider_api_unavailable',http_status=403)
        self.assertTrue(self.worker.tick(self.bot))
        self.assertTrue(self.bot.running)
        self.assertEqual(self.worker.phase,'notice_failed')
        self.assertEqual(self.worker.status()['failure_http_status'],403)
        self.assertFalse(self.worker.status()['notice_confirmed'])

    def test_undispatched_warning_retries_boundedly_without_claiming_delivery(self):
        self.bot._deliver_room_chat.side_effect=ApiAdmissionDeferred('invented')
        for _ in range(12):self.worker.retry_at=0;self.assertTrue(self.worker.tick(self.bot))
        self.assertFalse(self.worker.notice_at)
        self.assertEqual(self.worker.phase,'notice_failed')
        self.assertEqual(self.bot._deliver_room_chat.call_count,10)

    def test_cancel_is_idempotent_and_does_not_close_or_change_config(self):
        self.worker.tick(self.bot)
        self.worker.command({**self.job,'phase':'cancelled'})
        self.bot.state='IDLE';self.assertTrue(self.worker.tick(self.bot))
        self.worker.command(self.job);self.assertTrue(self.worker.tick(self.bot))
        self.assertEqual(self.worker.phase,'cancelled')
        self.bot.hub.close_room.assert_not_called()

    def test_empty_drain_rechecks_late_join_without_stopping_worker(self):
        self.worker.command(job('empty'));self.bot.state='IDLE';self.bot.players={}
        self.assertFalse(self.worker.tick(self.bot))
        self.bot.events.put(('UserJoined',{'user_id':7}))
        self.bot.handle_event.side_effect=lambda *args:self.bot.players.update({7:True})
        self.assertTrue(self.worker.tick(self.bot));self.assertEqual(self.worker.phase,'waiting_empty')
        self.assertTrue(self.bot.running);self.bot._deliver_room_chat.assert_not_called()

    def test_one_map_initially_empty_pauses_without_notice_and_late_join_requires_warning(self):
        self.bot.state='IDLE';self.bot.players={}
        self.assertFalse(self.worker.tick(self.bot))
        self.bot._deliver_room_chat.assert_not_called()
        self.assertFalse(self.worker.status()['notice_confirmed'])
        self.bot.events.put(('UserJoined',{'user_id':7}))
        self.bot.handle_event.side_effect=lambda *args:self.bot.players.update({7:True})
        self.assertTrue(self.worker.tick(self.bot))
        self.assertEqual(self.bot._deliver_room_chat.call_count,1)
        self.assertFalse(self.worker.status()['active_map_observed'])
        self.bot.state='PLAYING';self.assertTrue(self.worker.tick(self.bot))
        self.bot.state='SCORING';self.assertTrue(self.worker.tick(self.bot))
        self.bot.state='IDLE';self.assertFalse(self.worker.tick(self.bot))

    def test_disconnected_unknown_or_expired_requests_cannot_arm(self):
        self.bot.hub.is_connected.return_value=False
        self.assertTrue(self.worker.tick(self.bot));self.bot._deliver_room_chat.assert_not_called()
        self.worker.job['expires_at']=0;self.assertTrue(self.worker.tick(self.bot))
        self.assertEqual(self.worker.phase,'cancelled')


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        (self.root/'state/rolling').mkdir(parents=True)
        self.parent=dict(prefix=str(self.root/'prefix'),service_uid=1000,installation_id='installation',
            generation='generation',bundle_sha256='a'*64)
        self.job=job('empty')
        self.row=dict(phase='running',operation='owned',room_id=1,ownership_unknown=False)
        self.rows={'room1':self.row,'sibling':dict(self.row,room_id=2)}
        self.worker=SimpleNamespace(status={'room_id':1,'state':'IDLE','operator_maintenance':{
            'id':self.job['id'],'phase':'ready'}},fresh=lambda:True,idle_empty=Mock(return_value=True),
            broker=SimpleNamespace(known=True,close_room=Mock()),peer=Mock())
        self.supervisor=SimpleNamespace(protected_parent=self.parent,runtime=self.root,
            configs={'room1':{},'sibling':{}},registry=Mock(),workers={'room1':self.worker,'sibling':Mock()},
            shared=SimpleNamespace(lock=threading.RLock()),rollout_alias=None,dispose=Mock(),record=Mock())
        self.supervisor.registry.snapshot.return_value={'rooms':self.rows}
        def control(path,*args,**kwargs):
            if Path(path).name==operations.NAME and Path(path).parent.name=='prefix':return copy.deepcopy(self.job)
            return read_json(path) if Path(path).exists() else None
        self.enterContext(patch.object(operations,'_control',side_effect=control))

    def test_empty_room_closes_under_existing_controller_sibling_untouched(self):
        operations.poll(self.supervisor)
        self.worker.broker.close_room.assert_called_once_with(1,require_empty=True)
        self.supervisor.dispose.assert_called_once_with(self.worker)
        self.assertEqual(read_json(self.root/operations.RECEIPT)['phase'],'complete')
        self.supervisor.workers['sibling'].peer.call.assert_not_called()

    def test_selected_rooms_close_individually_as_each_becomes_empty(self):
        self.job['rooms'] = ['room1', 'sibling']
        sibling = SimpleNamespace(
            status={'room_id': 2, 'state': 'PLAYING', 'operator_maintenance': {
                'id': self.job['id'], 'phase': 'waiting_empty'}},
            fresh=lambda: True, idle_empty=Mock(return_value=False),
            broker=SimpleNamespace(known=True, close_room=Mock()), peer=Mock())
        self.supervisor.workers['sibling'] = sibling
        def disposed(worker):
            alias = 'room1' if worker is self.worker else 'sibling'
            self.rows[alias] = dict(phase='parked', operation='closed', room_id=None,
                                    ownership_unknown=False)
            self.supervisor.workers.pop(alias)
        self.supervisor.dispose.side_effect = disposed
        operations.poll(self.supervisor)
        self.worker.broker.close_room.assert_called_once_with(1, require_empty=True)
        sibling.broker.close_room.assert_not_called()
        self.assertEqual(read_json(self.root / operations.RECEIPT)['phase'], 'draining')
        sibling.status.update(state='IDLE', operator_maintenance={
            'id': self.job['id'], 'phase': 'ready'})
        sibling.idle_empty.return_value = True
        operations.poll(self.supervisor)
        sibling.broker.close_room.assert_called_once_with(2, require_empty=True)
        self.assertEqual(read_json(self.root / operations.RECEIPT)['phase'], 'complete')

    def test_late_join_or_stale_worker_does_not_close(self):
        self.worker.idle_empty.return_value=False
        operations.poll(self.supervisor);self.worker.broker.close_room.assert_not_called()
        self.worker.fresh=lambda:False
        operations.poll(self.supervisor);self.worker.broker.close_room.assert_not_called()

    def test_one_map_requires_main_thread_ready_then_explicit_occupied_close(self):
        self.job['mode']='one-map'
        self.worker.idle_empty.return_value=False
        self.worker.status['operator_maintenance']['phase']='waiting_map'
        operations.poll(self.supervisor);self.worker.broker.close_room.assert_not_called()
        self.worker.status['operator_maintenance']['phase']='ready'
        operations.poll(self.supervisor);self.worker.broker.close_room.assert_not_called()
        self.worker.status['operator_maintenance'].update(notice_confirmed=True,active_map_observed=True)
        operations.poll(self.supervisor)
        self.worker.broker.close_room.assert_called_once_with(1,require_empty=False)

    def test_one_map_empty_room_uses_empty_close_without_occupied_warning_authority(self):
        self.job['mode']='one-map'
        operations.poll(self.supervisor)
        self.worker.broker.close_room.assert_called_once_with(1,require_empty=True)
        self.assertEqual(read_json(self.root/operations.RECEIPT)['phase'],'complete')

    def test_one_map_late_join_refuses_empty_close_without_arming_occupied_close(self):
        from rolling.broker import InfrastructureCloseDeferred
        self.job['mode']='one-map'
        self.worker.broker.close_room.side_effect=InfrastructureCloseDeferred('late join')
        operations.poll(self.supervisor)
        self.supervisor.dispose.assert_not_called()
        self.assertNotEqual(read_json(self.root/operations.RECEIPT)['phase'],'complete')
        self.worker.idle_empty.return_value=False
        self.worker.broker.close_room.reset_mock()
        operations.poll(self.supervisor)
        self.worker.broker.close_room.assert_not_called()

    def test_completed_receipt_survives_controller_restart_without_replay(self):
        operations.poll(self.supervisor);del self.supervisor._operator_maintenance_receipt
        self.worker.broker.close_room.reset_mock();operations.poll(self.supervisor)
        self.assertEqual(read_json(self.root/operations.RECEIPT)['phase'],'complete')
        self.worker.broker.close_room.assert_not_called()

    def test_cancellation_acknowledged_before_terminal_and_no_reopening(self):
        self.job['phase']='cancelled';operations.poll(self.supervisor)
        self.worker.broker.close_room.assert_not_called()
        self.assertEqual(read_json(self.root/operations.RECEIPT)['phase'],'cancelling')
        self.worker.status['operator_maintenance']['phase']='cancelled';operations.poll(self.supervisor)
        self.assertEqual(read_json(self.root/operations.RECEIPT)['phase'],'cancelled')
        self.supervisor.dispose.assert_not_called()

    def test_restart_requires_review_without_reissuing_old_request(self):
        self.worker.status['operator_maintenance']['phase']='waiting_map'
        operations.poll(self.supervisor);del self.supervisor._operator_maintenance_receipt
        operations.poll(self.supervisor)
        self.assertEqual(self.supervisor._operator_maintenance_receipt['phase'],'requires_review')
        self.worker.peer.call.assert_not_called();self.worker.broker.close_room.assert_not_called()

    def test_parent_upgrade_never_adopts_predecessor_maintenance(self):
        self.parent['bundle_sha256']='b'*64;operations.poll(self.supervisor)
        self.worker.peer.call.assert_not_called();self.worker.broker.close_room.assert_not_called()

    def test_uncertain_closure_is_not_replayed_and_requires_review(self):
        from bot.hub import HubInvocationUncertain
        self.worker.broker.close_room.side_effect=HubInvocationUncertain('OAuth: provider_api_mutation_uncertain')
        operations.poll(self.supervisor);operations.poll(self.supervisor)
        self.assertEqual(self.worker.broker.close_room.call_count,1)
        self.assertEqual(read_json(self.root/operations.RECEIPT)['phase'],'requires_review')
        self.supervisor.workers['sibling'].peer.call.assert_not_called()

    def test_cancellation_at_final_roster_lock_prevents_closure(self):
        from contextlib import contextmanager
        @contextmanager
        def cancel():
            self.job['phase']='cancelled'
            yield
        self.supervisor.shared.lock=cancel()
        operations.poll(self.supervisor)
        self.worker.broker.close_room.assert_not_called()
        self.supervisor.dispose.assert_not_called()
        self.assertEqual(read_json(self.root/operations.RECEIPT)['phase'],'cancelling')

    def test_invalid_modes_identity_and_nonfinite_values_refuse(self):
        for change in ({'mode':'shell'},{'generation':'foreign'},{'id':'not-a-uuid'},
                       {'expires_at':float('inf')},{'format':True},{'rooms':['room1','room1']}):
            with self.subTest(change=change),self.assertRaises(ConfigError):operations.validate({**self.job,**change},self.parent)


class AdministrationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.folder=Path(temp.name)
        self.root=self.folder/'host';setup(self.root,['standard-auto','mania-4k'])
        marker,_=load(self.root);self.prefix=self.folder/'prefix';self.prefix.mkdir()
        self.parent={**marker,'prefix':str(self.prefix),'host_root':str(self.root),
            'service_uid':1000,'bundle_sha256':'a'*64,'release':str(self.prefix/'release')}
        self.target={'prefix':self.prefix,'root':self.root}
        self.binding=self.enterContext(patch.object(operations,'administrative_binding',return_value=(self.target,marker,self.parent)))
        self.state=self.enterContext(patch('poolrotater_host.pilot_operations._service_state',return_value={'ActiveState':'active','MainPID':123}))
        self.enterContext(patch('poolrotater_host.ratings.operations._identity',side_effect=lambda uid:nullcontext()))
        self.enterContext(patch.object(operations,'_write',side_effect=lambda path,value:atomic_json(path,value)))
        self.enterContext(patch.object(operations,'_control',side_effect=lambda path,*a,**k:read_json(path) if Path(path).exists() else None))
        self.id=str(uuid.uuid4())

    def request(self,**kwargs):
        return operations.command('test','request',operation_id=self.id,mode='one-map',**kwargs)

    def test_request_repeat_cancel_and_status_keep_host_configuration(self):
        before={str(p):p.read_bytes()for p in (self.root/'config').rglob('*')if p.is_file()}
        first=self.request(rooms=['room1']);again=self.request(rooms=['room1'])
        self.assertEqual(first['request'],again['request'])
        self.assertFalse(first['provider_dispatch'])
        self.assertEqual(operations.command('test','status')['request']['id'],self.id)
        cancelled=operations.command('test','cancel',operation_id=self.id)
        self.assertEqual(cancelled['request']['phase'],'cancelled')
        self.assertEqual(before,{str(p):p.read_bytes()for p in (self.root/'config').rglob('*')if p.is_file()})

    def test_stopped_host_or_pending_update_refuses_without_start_or_provider(self):
        self.state.return_value={'ActiveState':'inactive','MainPID':0}
        with self.assertRaisesRegex(ConfigError,'running_controller'):self.request()
        self.assertFalse((self.prefix/operations.NAME).exists())
        self.state.return_value={'ActiveState':'active','MainPID':123}
        atomic_json(self.prefix/'controller-update.json',{'phase':'waiting_empty'})
        with self.assertRaisesRegex(ConfigError,'deployment_pending'):self.request()

    def test_wrong_id_unknown_room_and_conflicting_repeat_refuse(self):
        with self.assertRaisesRegex(ConfigError,'existing_rooms'):self.request(rooms=['unknown'])
        self.request(rooms=['room1'])
        with self.assertRaisesRegex(ConfigError,'operation_changed'):self.request(rooms=['room2'])
        with self.assertRaisesRegex(ConfigError,'operation_changed'):
            operations.command('test','cancel',operation_id=str(uuid.uuid4()))

    def test_existing_worker_and_controller_admission_block_until_cancel_ack(self):
        self.request(rooms=['room1'])
        with self.assertRaisesRegex(ConfigError,'operation_pending'):operations.require_idle(self.target,self.parent)
        operations.command('test','cancel',operation_id=self.id)
        with self.assertRaisesRegex(ConfigError,'operation_pending'):operations.require_idle(self.target,self.parent)
        path=self.root/operations.RECEIPT;path.parent.mkdir(exist_ok=True,parents=True)
        atomic_json(path,{'id':self.id,'phase':'cancelled'})
        operations.require_idle(self.target,self.parent)
