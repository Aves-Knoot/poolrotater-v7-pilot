"""Regression for owner empty refresh and reconnect cooldown starvation."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import contextlib, threading, unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from bot.hub import HubDisconnected, HubInvocationUncertain
from poolrotater_host.provider_referee import ProviderRefereeClient, _log_failure
from poolrotater_host.provider_pacing import ProviderAdmissionDeferred
import test_headless_multiroom as native

class ReconnectBudgetTests(unittest.TestCase):
 def test_pacing_wait_does_not_spend_negotiation_or_upgrade_wire_budget(self):
  for command,kind,status in [(None,'negotiated',200),({'kind':'upgrade'},'upgraded',101)]:
   with self.subTest(kind=kind):
    clock=[10.]
    @contextlib.contextmanager
    def admission(deadline):
     self.assertEqual(deadline,15.);clock[0]=14.95;yield
    gate=Mock();gate.admission=admission
    c=object.__new__(ProviderRefereeClient);c.auth=SimpleNamespace(transport=SimpleNamespace(gate=gate,trust=None),execution='provider-fixture')
    c._boundary=Mock();c._token='invented';c._stop=threading.Event();session=Mock();c._session=session
    session.receive.return_value={'kind':kind,'status':status}
    with patch('poolrotater_host.provider_referee.time.monotonic',side_effect=lambda:clock[0]),patch('poolrotater_host.provider_referee.referee_port',return_value=9999),patch('poolrotater_host.process_startup.preflight'),patch('poolrotater_host.provider_referee.ChildSession',return_value=session):
     c._stage(kind,command)
    self.assertAlmostEqual(session.receive.call_args.args[0],19.95)
    gate.uncertain.assert_not_called();gate.replied.assert_called_once_with(status,0)
 def test_typed_admission_refusal_is_logged_without_losing_code(self):
  with patch('poolrotater_host.provider_referee.log.warning')as log:
   _log_failure(ProviderAdmissionDeferred('provider_pacing_deferred'),1)
  self.assertEqual(log.call_args.args[1],'provider_pacing_deferred')
 def test_outage_announced_once_across_repeated_local_admission_failures(self):
  c=object.__new__(ProviderRefereeClient);c._stop=Mock();c._stop.is_set.return_value=False
  c._stop.wait.side_effect=[False,False,True];c._attempt=Mock();c._connect_generation=1;c._session=None
  c._open=Mock(side_effect=[None,ProviderAdmissionDeferred('provider_pacing_deferred'),ProviderAdmissionDeferred('provider_deadline_exceeded')])
  session=Mock();session.receive.side_effect=HubDisconnected('invented outage')
  original=c._open
  def op():
   original();c._session=session
  c._open=op;c._retire=Mock();c._event=Mock()
  with patch('poolrotater_host.provider_referee._log_failure'):c._run()
  self.assertEqual(c._event.call_args_list[0].args,('HubDisconnected',{'generation':1},1))
  self.assertEqual(c._event.call_count,1)
  self.assertIsInstance(c._failure,ProviderAdmissionDeferred)
  self.assertEqual([x.args[0]for x in c._stop.wait.call_args_list],[.2,.4,.8])

class RefreshRosterAdmissionTests(unittest.TestCase):
 def setUp(self):
  f=native.ProductionControllerTests();f.setUp();self.addCleanup(f.doCleanups);self.bot=f.bots[0]
  self.bot.hub=Mock();self.bot._drain_hub_events=Mock();self.bot._await_referee_roster=Mock()
 def test_first_map_local_refusal_rejoins_then_sends_once(self):
  call=Mock(side_effect=[HubDisconnected('room roster requires a fresh authoritative JoinRoom'),{'ok':True}])
  self.assertEqual(self.bot._referee_operation_with_rejoin('EditCurrentPlaylistItem',call),{'ok':True})
  self.assertEqual(call.call_count,2);self.bot.hub.join_room.assert_called_once_with(self.bot.room_id)
  self.bot._await_referee_roster.assert_called_once();self.assertFalse(self.bot._hub_rejoin_pending)
 def test_join_overlap_retries_join_before_attempting_map(self):
  self.bot.hub.join_room.side_effect=[HubDisconnected('JoinRoom overlapped undelivered roster events'),{}]
  call=Mock(side_effect=[HubDisconnected('room roster requires a fresh authoritative JoinRoom'),'confirmed'])
  self.assertEqual(self.bot._referee_operation_with_rejoin('EditCurrentPlaylistItem',call),'confirmed')
  self.assertEqual(call.call_count,2);self.assertEqual(self.bot.hub.join_room.call_count,2)
 def test_uncertain_write_is_never_replayed_even_after_successful_join(self):
  for effects in ([HubInvocationUncertain('unknown write')],[HubDisconnected('room roster requires a fresh authoritative JoinRoom'),HubInvocationUncertain('unknown write')]):
   call=Mock(side_effect=effects)
   with self.assertRaises(HubInvocationUncertain):self.bot._referee_operation_with_rejoin('AddPlaylistItem',call)
   self.assertEqual(call.call_count,len(effects))
 def test_unknown_disconnect_never_rejoins_or_replays(self):
  call=Mock(side_effect=HubDisconnected('shared referee transport is disconnected'))
  with self.assertRaises(HubDisconnected):self.bot._referee_operation_with_rejoin('StartMatch',call)
  call.assert_called_once();self.bot.hub.join_room.assert_not_called()

from test_v7_provider_rooms import RoomCase
class ActualWireAdmissionTests(RoomCase):
 def test_real_tls_negotiation_and_upgrade_survive_nearly_spent_admission_window(self):
  import time
  auth=self.authorized();backend,api,hub=self.adapters(auth)
  with auth.verified_owner():
   gate=auth.transport.gate;admission=gate.admission
   @contextlib.contextmanager
   def delayed(deadline):
    with admission(deadline):
     time.sleep(4.95)
     yield
   with patch.object(gate,'admission',side_effect=delayed):hub.connect()
   self.assertTrue(hub.is_connected())
   rid=hub.make_room('invented repaired admission',1)['room_id']
   self.assertEqual(hub.invoke('JoinRoom',[rid])['room_id'],rid)
   hub.invoke('CloseRoom',[rid]);hub.stop()
  self.assertFalse(backend.rooms)
