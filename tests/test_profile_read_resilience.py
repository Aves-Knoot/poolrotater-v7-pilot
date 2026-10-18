"""Replay the two community profile crashes through the typed RPC boundary."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import socket,unittest,multiprocessing
from unittest.mock import Mock,patch
from bot.api import ApiProfileReadUnavailable
from bot.bot import LobbyBot
from poolrotater_host.provider_api import ProviderProfileReadUnavailable, ProviderApiClient
from poolrotater_host.oauth import AuthError
from rolling.adapters import Api
from rolling.protocol import Peer,RemoteError
import test_player_profile_admission as profile_fixture
from test_v7_provider_rooms import RoomCase

class ProfileGameplayTests(unittest.TestCase):
 def bot(self):return profile_fixture.DeferredProfileTests.bot(self)
 def api(self,error):
  a,b=socket.socketpair()
  def handle(*args,**kwargs):raise error
  server=Peer(a,handler=handle).start();client=Peer(b).start()
  self.addCleanup(server.close);self.addCleanup(client.close)
  return Api(client)
 def test_join_profile_timeout_preserves_roster_and_schedules_retry_without_closure(self):
  b=self.bot();b.api=self.api(ProviderProfileReadUnavailable('provider_profile_read_unavailable'));b.running=True
  with patch('bot.bot.time.monotonic',return_value=100):b.on_user_joined(7)
  self.assertTrue(b.running);self.assertIn(7,b.players);self.assertIsNone(b.players[7]['pp'])
  b._record_lobby_join.assert_called_once();self.assertIn(7,b._pending_player_profiles)
 def test_deferred_profile_timeout_keeps_ready_then_enriches_once(self):
  b=self.bot();b.api.user.side_effect=ApiProfileReadUnavailable('provider_profile_read_unavailable')
  with patch('bot.bot.time.monotonic',return_value=100):b.on_user_joined(7)
  b.players[7]['status']='ready'
  with patch('bot.bot.time.monotonic',return_value=102):b._flush_player_profiles()
  self.assertIn(7,b._pending_player_profiles);self.assertEqual(b.players[7]['status'],'ready')
  b.api.user.side_effect=None
  with patch('bot.bot.time.monotonic',return_value=105):b._flush_player_profiles()
  self.assertEqual(b.players[7]['pp'],1234);self.assertEqual(b.players[7]['status'],'ready');self.assertFalse(b._pending_player_profiles)
  b._record_lobby_join.assert_called_once()
 def test_top_score_timeout_reuses_valid_profile_and_never_infers_zero(self):
  b=self.bot();b.api.user_top_scores.side_effect=ApiProfileReadUnavailable('provider_profile_read_unavailable')
  with patch('bot.bot.time.monotonic',return_value=100):b.on_user_joined(7)
  self.assertEqual(b.players[7]['pp'],1234);self.assertNotIn('top',b.players[7])
  b.api.user_top_scores.side_effect=None
  with patch('bot.bot.time.monotonic',return_value=102):b._flush_player_profiles()
  self.assertEqual(b.api.user.call_count,1);self.assertEqual(b.players[7]['top'],[(100,22)])
 def test_auth_cleanup_and_rpc_failures_are_not_optional_profile_deferrals(self):
  for error in (AuthError('verified_owner_required'),AuthError('provider_child_cleanup_uncertain')):
   b=self.bot();b.api=self.api(error)
   with self.assertRaises(RemoteError):b.on_user_joined(7)

class ProfileWireTests(RoomCase):
 def test_actual_dispatched_timeout_is_typed_and_child_reaped_without_retry(self):
  auth=self.authorized();api=ProviderApiClient(auth)
  with auth.verified_owner():
   auth.transport.timeout=.45;self.provider.wire('/api/v2/users/7/osu?key=id',200,b'{"id":7,"username":"Invented","statistics":{"pp":1}}',delay=1.5)
   before=auth.transport.gate.snapshot()['counters'].copy();children={p.pid for p in multiprocessing.active_children()}
   with self.assertRaises(ProviderProfileReadUnavailable):api.call('user',7)
   after=auth.transport.gate.snapshot()['counters'];self.assertEqual(after['dispatched'],before['dispatched']+1)
   self.assertEqual(children,{p.pid for p in multiprocessing.active_children()})
 def test_forbidden_profile_remains_authority_error(self):
  auth=self.authorized();api=ProviderApiClient(auth)
  with auth.verified_owner():
   self.provider.wire('/api/v2/users/7/osu?key=id',403,b'{}')
   with self.assertRaises(AuthError)as got:api.call('user',7)
   self.assertNotIsInstance(got.exception,ProviderProfileReadUnavailable)
 def test_admission_deadline_is_typed_but_unsafe_cleanup_is_not(self):
  auth=self.authorized()
  for code,expected in [('provider_deadline_exceeded',ProviderProfileReadUnavailable),('provider_child_cleanup_uncertain',AuthError)]:
   api=ProviderApiClient(auth)
   with auth.verified_owner(),patch.object(api,'_req',side_effect=AuthError(code)):
    with self.assertRaises(expected)as got:api.call('user',7)
    if expected is AuthError:self.assertNotIsInstance(got.exception,ProviderProfileReadUnavailable)
