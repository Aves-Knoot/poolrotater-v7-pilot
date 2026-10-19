"""Definite pre-mutation refusals wait; unknown writes and cleanup failures hold."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import queue,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from rolling.supervisor import RollingSupervisor,HandoffDeferred
from rolling.protocol import UncertainResult

class HandoffDeferralTests(unittest.TestCase):
    def setUp(self):
        s=self.s=RollingSupervisor.__new__(RollingSupervisor);s.configs={'room1':{},'room2':{}};s.schema_baseline={}
        broker=SimpleNamespace(uncertain=False,fenced=False,quiesced=False,password='',hub=Mock(owned={101}))
        old=self.old=SimpleNamespace(fresh=Mock(return_value=True),idle_empty=Mock(return_value=True),broker=broker,status={'paused':False},empty_since=1,empty_epoch=5,release=Path('/old'))
        s.workers={'room1':old,'room2':object()};s.targets={'room1':Path('/new')};s.rollout_alias='room1'
        s.shared=SimpleNamespace(lock=threading.RLock(),events=queue.Queue());s.stopping=Mock();s.stopping.is_set.return_value=False
        self.candidate=SimpleNamespace(broker=SimpleNamespace(uncertain=False,fenced=False))
        s.prepare=Mock(return_value=self.candidate);s.dispose=Mock();s.control=Mock(side_effect=HandoffDeferred('worker_not_quiescent'));s.record=Mock()
        self.enterContext(patch('rolling.supervisor.schemas',return_value={}))
    def roll(self):
        with patch('rolling.supervisor.time.monotonic',return_value=100):self.s.roll('room1',Path('/new'))
    def test_nack_retains_target_and_current_room_without_provider_mutation(self):
        sibling=self.s.workers['room2'];self.roll()
        self.assertIs(self.s.workers['room1'],self.old);self.assertIs(self.s.workers['room2'],sibling)
        self.assertEqual(self.s.targets,{'room1':Path('/new')});self.s.dispose.assert_called_once_with(self.candidate)
        self.old.broker.hub.change_room_settings.assert_not_called();self.old.broker.hub.close_room.assert_not_called()
        self.assertIsNone(self.old.empty_since);self.assertEqual(self.old.empty_epoch,-1);self.assertEqual(self.old.handoff_retry_at,102)
        self.assertIsNone(self.s.rollout_alias);self.assertEqual(self.s.record.call_args.kwargs['phase'],'waiting_empty')
    def test_repeated_known_refusals_have_bounded_backoff_and_keep_intent(self):
        delays=[]
        for _ in range(8):self.roll();delays.append(self.old.handoff_retry_at-100)
        self.assertEqual(delays,[2,4,8,16,30,30,30,30]);self.assertIn('room1',self.s.targets)
    def test_player_arrives_while_candidate_prepares_without_quiesce_or_close(self):
        self.old.idle_empty.side_effect=[True,False];self.roll()
        self.s.control.assert_not_called();self.assertIn('room1',self.s.targets)
        self.old.broker.hub.change_room_settings.assert_not_called()
        self.assertEqual(self.s.record.call_args.kwargs['handoff_deferred_reason'],'room_changed_during_preparation')
    def test_cancelled_target_is_never_readded(self):
        def refusal(*a):self.s.targets.clear();raise HandoffDeferred('worker_not_quiescent')
        self.s.control.side_effect=refusal;self.roll();self.assertEqual(self.s.targets,{})
        self.assertEqual(self.s.record.call_args.kwargs['phase'],'running')
    def test_acknowledged_pause_is_resumed_before_rewaiting(self):
        self.old.status['paused']=True;self.old.idle_empty.side_effect=[True,True,False]
        self.s.control.side_effect=None;self.roll();self.assertEqual([c.args[1]for c in self.s.control.call_args_list],['prepare','resume'])
        self.assertIn('room1',self.s.targets);self.old.broker.hub.change_room_settings.assert_not_called()
    def test_uncertain_rpc_and_lookalike_text_do_not_authorize_retry(self):
        for error in (UncertainResult('unknown prepare outcome'),RuntimeError('worker_not_quiescent')):
            with self.subTest(error=type(error).__name__):
                self.s.targets={'room1':Path('/new')};self.s.control.side_effect=error;self.roll()
                self.assertNotIn('room1',self.s.targets);self.assertFalse(hasattr(self.old,'handoff_retry_at'))
    def test_candidate_cleanup_failure_remains_terminal(self):
        self.s.dispose.side_effect=[RuntimeError('child_cleanup_unresolved'),None]
        self.roll();self.assertNotIn('room1',self.s.targets);self.assertFalse(hasattr(self.old,'handoff_retry_at'))
    def test_real_prepare_ack_false_is_typed_but_flush_ack_false_is_terminal(self):
        s=RollingSupervisor.__new__(RollingSupervisor);worker=SimpleNamespace(status={'control':{}},peer=Mock())
        def acknowledge(command,request_id,**kw):worker.status['control']={'id':request_id,'ok':False}
        worker.peer.call.side_effect=acknowledge;s.wait_for=lambda predicate,*a:self.assertTrue(predicate())
        with self.assertRaises(HandoffDeferred):s.control(worker,'prepare')
        with self.assertRaises(RuntimeError)as error:s.control(worker,'flush')
        self.assertNotIsInstance(error.exception,HandoffDeferred)

class ProcessRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_rolling as native
        cls.native=native;native.ProcessIntegrationTests.setUpClass()
    @classmethod
    def tearDownClass(cls):cls.native.ProcessIntegrationTests.tearDownClass()
    def test_definite_nack_then_native_tick_retries_without_restarting_playing_sibling(self):
        f=self.native.ProcessIntegrationTests();f.setUp();self.addCleanup(f.doCleanups)
        old=f.start('room1');sibling=f.start('room2');s=f.supervisor
        sibling_id=sibling.status['room_id']
        f.hub.rooms[sibling_id]['players']=[{'user_id':7,'username':'PlayingSibling'}]
        s.shared.route('UserJoined',{'room_id':sibling_id,'user_id':7,'username':'PlayingSibling'})
        s.wait_for(lambda:sibling.status.get('players')==1,8,sibling)
        item=f.hub.rooms[sibling_id]['playlist'][0]['id']
        s.shared.route('MatchStarted',{'room_id':sibling_id,'playlist_item_id':item})
        s.wait_for(lambda:sibling.status.get('state')=='PLAYING',8,sibling)
        s.schema_baseline=self.native.schemas(list(s.configs.values()));s.request(['room1'],f.releases[1]);actual=s.control
        def definite_nack(worker,command,**kwargs):
            if command=='prepare':raise HandoffDeferred('worker_not_quiescent')
            return actual(worker,command,**kwargs)
        with patch.object(s,'control',side_effect=definite_nack):s.roll('room1',f.releases[1])
        self.assertIs(s.workers['room1'],old);self.assertIn('room1',s.targets)
        self.assertFalse(any(m=='CloseRoom'for m,_ in f.hub.calls));self.assertEqual(sibling.status['state'],'PLAYING')
        def complete():
            s.tick();return s.workers['room1'].release==f.releases[1]and 'room1'not in s.targets
        s.wait_for(complete,40)
        self.assertIsNone(sibling.process.poll());self.assertEqual(sibling.status['room_id'],sibling_id)
        self.assertEqual(sibling.status['state'],'PLAYING');self.assertFalse(any(m=='CloseRoom'and v[0]==sibling_id for m,v in f.hub.calls))
