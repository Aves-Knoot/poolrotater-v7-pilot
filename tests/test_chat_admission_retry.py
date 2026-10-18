"""Typed no-dispatch refusals keep chat cursors and the gameplay loop alive."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import socket,unittest
from unittest.mock import Mock,patch
from bot.api import ApiAdmissionDeferred
from bot.bot import LobbyBot
from bot.hub import HubInvocationUncertain
from rolling.adapters import Api
from rolling.protocol import Peer,RemoteError,UncertainResult
from poolrotater_host.provider_pacing import ProviderAdmissionDeferred

class ChatAdmissionRetryTests(unittest.TestCase):
 def bot(self):
  b=object.__new__(LobbyBot);b.api=Mock();b.cfg={};b.state='IDLE'
  b.room_id=1;b.channel_id=11;b.last_message_id=77;b._last_ack=0;b._last_chat_poll=0
  b._chat_websocket_enabled=Mock(return_value=False);b._chat_patrol_enabled=Mock(return_value=False)
  b._process_chat_messages=Mock();return b
 def test_ack_retry_never_advances_ack_or_message_cursor_before_success(self):
  b=self.bot();b.api.chat_ack.side_effect=[ApiAdmissionDeferred('deferred'),None]
  b.api.chat_messages.return_value=[{'message_id':78}]
  with patch('bot.bot.time.time',return_value=100),patch('bot.bot.time.monotonic',return_value=100):
   b.poll_chat(force=True);b.poll_chat(force=True)
  self.assertEqual(b.api.chat_ack.call_count,1);self.assertEqual(b.api.chat_messages.call_count,2)
  self.assertEqual(b._last_ack,0);self.assertEqual(b.last_message_id,77)
  with patch('bot.bot.time.time',return_value=102),patch('bot.bot.time.monotonic',return_value=102):b.poll_chat(force=True)
  self.assertEqual(b._last_ack,102);b.api.chat_messages.assert_called_with(11,since=77)
  b._process_chat_messages.assert_called_with([{'message_id':78}])
 def test_deferred_ack_does_not_prevent_realtime_inbox_processing(self):
  b=self.bot();b.api.chat_ack.side_effect=ApiAdmissionDeferred('deferred')
  b._chat_websocket_enabled.return_value=True;b.chat_websocket=Mock(connected=True)
  b._chat_websocket_needs_reconcile=False;b._last_chat_websocket_reconcile=100
  b._drain_chat_websocket_inbox=Mock()
  with patch('bot.bot.time.time',return_value=100),patch('bot.bot.time.monotonic',return_value=100):b.poll_chat()
  b._drain_chat_websocket_inbox.assert_called_once();self.assertEqual(b._last_ack,0)
 def test_history_retry_retains_reconciliation_and_cursor(self):
  b=self.bot();b.api.chat_messages.side_effect=ApiAdmissionDeferred('deferred')
  b._last_chat_websocket_reconcile=0
  with patch('bot.bot.time.monotonic',return_value=100):b._reconcile_chat_history(reason='test gap')
  self.assertTrue(b._chat_websocket_needs_reconcile);self.assertEqual(b.last_message_id,77)
  self.assertEqual(b._last_chat_websocket_reconcile,0);b._process_chat_messages.assert_not_called()
 def test_backoff_is_bounded_and_success_resets_it(self):
  b=self.bot();b.api.chat_ack.side_effect=ApiAdmissionDeferred('deferred')
  delays=[]
  for now in range(100,240,20):
   with patch('bot.bot.time.monotonic',return_value=now):b._chat_api_call('chat_ack')
   delays.append(b._chat_api_retry_at-now)
  self.assertEqual(delays,[1,1,1,1,1,1,1])
  b.api.chat_ack.side_effect=None
  with patch('bot.bot.time.monotonic',return_value=300):self.assertTrue(b._chat_api_call('chat_ack')[0])
  self.assertEqual(b._chat_api_retry_at,0);self.assertEqual(b._chat_api_deferred_count,0)
 def test_generic_auth_and_uncertain_rpc_are_not_reclassified(self):
  for error in (RemoteError('AuthError','OAuth: provider_pacing_deferred'),UncertainResult('unknown reply')):
   peer=Mock();peer.call.side_effect=error;b=self.bot();b.api=Api(peer)
   with self.assertRaises((RemoteError,HubInvocationUncertain)):b.poll_chat(force=True)
 def test_real_rpc_converts_only_proven_no_dispatch_chat_operations(self):
  a,b=socket.socketpair()
  def refuse(*args,**kwargs):raise ProviderAdmissionDeferred('provider_pacing_deferred')
  server=Peer(a,handler=refuse).start();client=Peer(b).start()
  self.addCleanup(server.close);self.addCleanup(client.close)
  for method,args in [('chat_ack',()),('chat_messages',(11,)),('chat_send',(11,'one message'))]:
   with self.assertRaises(ApiAdmissionDeferred):getattr(Api(client),method)(*args)
  with self.assertRaises(RemoteError):Api(client).beatmap(123)

if __name__=='__main__':unittest.main()
