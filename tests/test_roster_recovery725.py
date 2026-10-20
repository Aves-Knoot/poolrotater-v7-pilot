"""Oct20 skip/rejoin and delayed close regressions; no real provider traffic."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import queue, socket, threading, time, unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from bot.hub import HubDisconnected, HubInvocationUncertain, HubTransportError
from rolling.protocol import Peer
from rolling.adapters import Hub
from rolling.supervisor import RollingSupervisor, Worker
from test_v7_provider_rooms import RoomCase
from test_crash_recovery_admission import RefreshRosterAdmissionTests
from test_provider_service_liveness import NativeBlockedObservationTests
from poolrotater_host.provider_service import run_provider_service

class FailedAliasCleanupTests(unittest.TestCase):
 def setUp(self):
  self.state={'operation':'owned','ownership_unknown':False,'room_id':123,'phase':'running'}
  self.s=object.__new__(RollingSupervisor);self.s.shared=SimpleNamespace(lock=threading.RLock(),hub=Mock())
  self.s.registry=Mock();self.s.registry.snapshot.side_effect=lambda:{'rooms':{'room1':dict(self.state)}}
  self.s.dispose=Mock();self.s.record=Mock()
  hub=Mock();hub.owned={123};hub.roster_fenced=False;hub.roster_progress.return_value=(1,1,False,1);hub._generation.return_value=1
  self.b=SimpleNamespace(uncertain=False,known=True,hub=hub,snapshot_generation=1,handle=Mock(),close_room=Mock())
  self.w=Worker('room1',None,{},self.b);self.w.failure='invented worker failure'
 def test_pending_close_is_allowed_to_finish_before_ownership_decision(self):
  held=threading.Event();release=threading.Event()
  def close():
   with self.s.shared.lock:
    self.state.update(operation='close_pending',ownership_unknown=True);held.set();release.wait(3)
    self.state.update(operation='closed',ownership_unknown=False,room_id=None);self.b.hub.owned.clear()
  t=threading.Thread(target=close);t.start();self.assertTrue(held.wait(2))
  try:
   self.assertFalse(self.s._settle_failed_worker(self.w,time.monotonic()))
   self.s.record.assert_not_called();self.b.close_room.assert_not_called()
  finally:release.set();t.join(3)
  self.assertTrue(self.s._settle_failed_worker(self.w,time.monotonic()))
  self.s.record.assert_called_once_with('room1',phase='parked',pid=None,error=None)
  self.b.close_room.assert_not_called()
 def test_stale_roster_refresh_defers_then_closes_only_known_room(self):
  self.b.known=False
  def refresh(*args):
   self.b.known=True;return {}
  self.b.handle.side_effect=[HubDisconnected('referee_join_rejected'),{}]
  now=time.monotonic();self.assertFalse(self.s._settle_failed_worker(self.w,now));deadline=self.w.failure_cleanup_deadline
  self.b.close_room.assert_not_called();self.s.record.assert_not_called()
  self.assertTrue(self.s._settle_failed_worker(self.w,now+.1));self.assertEqual(deadline,self.w.failure_cleanup_deadline)
  self.assertEqual(self.b.handle.call_count,2)
  self.b.handle.assert_called_with('hub.invoke','JoinRoom',[123])
  self.b.close_room.assert_called_once_with(123,require_empty=False)
 def test_unknown_creation_never_joins_closes_or_recreates(self):
  self.state.update(operation='make_pending',ownership_unknown=True,room_id=None)
  with self.assertRaisesRegex(RuntimeError,'ownership_requires_review'):self.s._settle_failed_worker(self.w,time.monotonic())
  self.b.handle.assert_not_called();self.b.close_room.assert_not_called();self.s.dispose.assert_not_called()
 def test_uncertain_close_is_never_replayed_or_parked(self):
  def close(*a,**k):self.b.uncertain=True;raise HubInvocationUncertain('lost close response')
  self.b.close_room.side_effect=close
  with self.assertRaises(HubInvocationUncertain):self.s._settle_failed_worker(self.w,time.monotonic())
  with self.assertRaisesRegex(RuntimeError,'ownership_requires_review'):self.s._settle_failed_worker(self.w,time.monotonic())
  self.assertEqual(self.b.close_room.call_count,1);self.s.record.assert_not_called()
 def test_fixed_deadline_does_not_extend_on_ordering_deferrals(self):
  self.b.hub.roster_progress.return_value=(1,1,True,1)
  start=time.monotonic();self.assertFalse(self.s._settle_failed_worker(self.w,start));deadline=self.w.failure_cleanup_deadline
  with patch('rolling.supervisor.time.monotonic',return_value=deadline+1):
   with self.assertRaises(HubTransportError):self.s._settle_failed_worker(self.w,deadline+1)
  self.assertEqual(self.w.failure_cleanup_deadline,deadline);self.b.close_room.assert_not_called()

class RejectedJoinWireTests(RoomCase):
 def test_rejected_join_survives_tls_and_rpc_as_recoverable_without_replaying_mutation(self):
  auth=self.authorized();backend,api,hub=self.adapters(auth)
  with auth.verified_owner():
   hub.connect();rid=hub.make_room('invented room',1)['room_id']
   a,b=socket.socketpair();server=Peer(a,handler=lambda method,operation,args:hub.invoke(operation,args)).start();client=Peer(b).start()
   self.addCleanup(server.close);self.addCleanup(client.close)
   backend.wire.completion_error='invented private upstream detail'
   view=Hub(client)
   try:
    with self.assertRaisesRegex(HubDisconnected,'^referee_join_rejected$'):view.join_room(rid)
    self.assertEqual([c['target']for c in backend.wire.commands].count('JoinRoom'),1)
    with self.assertRaises(Exception) as raised:view.invoke('EditCurrentPlaylistItem',[rid,{'beatmap_id':2}])
    self.assertNotIsInstance(raised.exception,HubTransportError)
    self.assertNotIn('private upstream',str(raised.exception))
   finally:backend.wire.completion_error=None
   self.assertEqual(hub.invoke('JoinRoom',[rid])['room_id'],rid)
   hub.invoke('CloseRoom',[rid]);hub.stop()

class RejectedRosterCommandTests(RefreshRosterAdmissionTests):
 def test_refused_refresh_exits_to_transport_recovery_without_replaying_skip_mutation(self):
  call=Mock(side_effect=HubDisconnected('room roster requires a fresh authoritative JoinRoom'))
  self.bot.hub.join_room.side_effect=HubDisconnected('referee_join_rejected')
  with self.assertRaisesRegex(HubDisconnected,'referee_join_rejected'):
   self.bot._referee_operation_with_rejoin('EditCurrentPlaylistItem',call)
  call.assert_called_once();self.bot.hub.join_room.assert_called_once()

class NativeIncidentTests(NativeBlockedObservationTests):
 def test_skip_refused_join_recovers_in_place_while_sibling_keeps_playing(self):
  proof={};phases=[]
  def observer(phase,supervisor,workers):
   phases.append(phase)
   if phase!='running':return
   affected,*siblings=list(workers);sibling=siblings[-1]
   for index,w in enumerate([affected,sibling]):
    rid=w.status['room_id'];self.backend.rooms[rid]['players'].append({'user_id':7+index,'status':'idle'})
    self.backend.wire.route('UserJoined',{'room_id':rid,'user_id':7+index})
   original_handler=affected.peer.handler;injected=False;rejected=False;rejoined=False;issued=False;playing=False
   original_messages=self.api.chat_messages
   def messages(channel,*args,**kwargs):
    nonlocal issued
    result=original_messages(channel,*args,**kwargs)
    if (channel==affected.broker.channel and playing and not issued
        and affected.status.get('players')==1 and sibling.status.get('state')=='PLAYING'):
     issued=True;return [*(result or []),{'message_id':987654,'sender_id':7,'content':'.skip'}]
    return result
   self.api.chat_messages=messages
   def handler(method,*args,**kwargs):
    nonlocal injected,rejected,rejoined
    if method=='hub.invoke' and args[0]=='EditCurrentPlaylistItem' and issued and not injected:
     injected=True;affected.broker.known=False;affected.broker.hub.roster_fenced=True
    if method=='hub.invoke' and args[0]=='JoinRoom' and injected and not rejected:
     rejected=True
     with supervisor.shared.lock:
      self.backend.wire.completion_error='invented transient JoinRoom refusal'
      try:return original_handler(method,*args,**kwargs)
      finally:self.backend.wire.completion_error=None
    result=original_handler(method,*args,**kwargs)
    if method=='hub.invoke' and args[0]=='JoinRoom' and rejected:rejoined=True
    return result
   affected.peer.handler=handler
   native_tick=supervisor.tick;started=time.monotonic()
   def tick():
    nonlocal playing
    self.assertLess(time.monotonic()-started,65,'incident did not recover')
    native_tick()
    if not playing and sibling.status.get('players')==1:
     rid=sibling.status['room_id'];self.backend.wire.route('MatchStarted',{'room_id':rid,'playlist_item_id':self.backend.rooms[rid]['playlist'][0]['id']});playing=True
    if rejoined and affected.fresh() and affected.status.get('ready'):
     self.assertTrue(issued and rejected)
     self.assertTrue(all(w.process.poll() is None for w in workers))
     self.assertEqual(sibling.status['state'],'PLAYING')
     self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),4)
     self.assertEqual(self.backend.authenticated_calls.count('CloseRoom'),0)
     self.assertTrue(all(supervisor.workers[w.alias] is w for w in workers))
     proof['recovered']=True;supervisor.request_shutdown()
   supervisor.tick=tick
  run_provider_service(self.root,self.release,authentication=self.auth,api=self.api,hub=self.hub,observer=observer)
  self.assertTrue(proof['recovered']);self.assertEqual(phases,['running','stopped']);self.assertFalse(self.backend.rooms)

 def test_dead_worker_stale_roster_refresh_closes_only_failed_alias(self):
  proof={};phases=[]
  def observer(phase,supervisor,workers):
   phases.append(phase)
   if phase!='running':return
   affected,*siblings=list(workers);occupied=siblings[-1];rid=occupied.status['room_id']
   self.backend.rooms[rid]['players'].append({'user_id':8,'status':'idle'})
   self.backend.wire.route('UserJoined',{'room_id':rid,'user_id':8})
   original=affected.peer.handler;faulted=False;playing=False;staled=False
   def handler(method,*args,**kwargs):
    nonlocal faulted
    if method=='api.chat_messages' and playing and occupied.status.get('state')=='PLAYING' and not faulted:
     faulted=True;raise ValueError('invented worker fault')
    return original(method,*args,**kwargs)
   affected.peer.handler=handler;native_tick=supervisor.tick;started=time.monotonic()
   native_poll=affected.process.poll
   def poll():
    nonlocal staled
    code=native_poll()
    if code is not None and faulted and not staled:
     affected.broker.known=False;affected.broker.hub.roster_fenced=True;staled=True
    return code
   affected.process.poll=poll
   def tick():
    nonlocal playing,staled
    self.assertLess(time.monotonic()-started,60,'failed alias did not settle')
    if faulted and affected.process.poll() is not None and not staled:
     affected.broker.known=False;affected.broker.hub.roster_fenced=True;staled=True
    native_tick()
    if not playing and occupied.status.get('players')==1:
     self.backend.wire.route('MatchStarted',{'room_id':rid,'playlist_item_id':self.backend.rooms[rid]['playlist'][0]['id']});playing=True
    row=supervisor.registry.snapshot()['rooms'][affected.alias]
    if row.get('phase')=='parked':
     self.assertTrue(faulted and staled);self.assertFalse(row['ownership_unknown']);self.assertEqual(row['operation'],'closed')
     self.assertTrue(all(w.process.poll() is None and supervisor.workers[w.alias] is w for w in siblings))
     self.assertEqual(occupied.status['state'],'PLAYING');self.assertIn(rid,self.backend.rooms)
     self.assertEqual(self.backend.authenticated_calls.count('MakeRoom'),4);self.assertEqual(self.backend.authenticated_calls.count('CloseRoom'),1)
     proof['isolated']=True;supervisor.request_shutdown()
   supervisor.tick=tick
  run_provider_service(self.root,self.release,authentication=self.auth,api=self.api,hub=self.hub,observer=observer)
  self.assertTrue(proof['isolated']);self.assertEqual(phases,['running','stopped']);self.assertFalse(self.backend.rooms)
