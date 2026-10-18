"""Missing chat membership follows the native bounded room recovery contract."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from poolrotater_host.oauth import AuthError
from poolrotater_host.provider_referee import ProviderRefereeClient,_log_failure
from test_v7_provider_rooms import RoomCase
import test_headless_multiroom as native

class ProviderChatRecoveryTests(RoomCase):
    def native_bot(self,api):
        fixture=native.ProductionControllerTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        bot=fixture.bots[0];bot.cfg['_authenticated_user_id']=99
        bot.api=SimpleNamespace(chat_join=api.chat_join,room_probe=lambda _:('ok',{}))
        bot._sync_from_room_snapshot=Mock();bot.reconcile_playlist=Mock()
        bot.hub.join_room=Mock(wraps=bot.hub.join_room)
        return bot,fixture

    def test_actual_tls_missing_channel_retries_native_join_then_resumes(self):
        auth=self.authorized();backend,api,hub=self.adapters(auth)
        bot,fixture=self.native_bot(api);statuses=iter((404,200));actual=api.chat_join;attempts=[]
        def join(channel,user):
            status=next(statuses);attempts.append(status)
            payload={'channel':{'channel_id':channel,'current_user_attributes':{'can_message':True}}}
            self.provider.wire('/api/v2/chat/channels/'+str(channel),status,json.dumps(payload).encode())
            return actual(channel,user)
        bot.api.chat_join=join
        with auth.verified_owner(),patch('bot.bot.time.sleep') as delay,patch.object(api.client,'_req',wraps=api.client._req) as requests:
            bot._recover_hub_transport('invented transient membership')
        self.assertEqual(attempts,[404,200]);delay.assert_called_once_with(2.0)
        self.assertTrue(bot.running);self.assertFalse(bot._hub_rejoin_pending)
        bot.reconcile_playlist.assert_called_once()
        self.assertEqual(bot.hub.join_room.call_count,2)
        # First JoinRoom is still cached; missing membership invalidates it,
        # so exactly one actual same-room JoinRoom dispatch restores access.
        self.assertEqual([method for method,args in fixture.hub.calls].count('JoinRoom'),1)
        self.assertEqual([(c.args[0],c.args[1]) for c in requests.call_args_list],[('GET','/chat/channels/1101'),('GET','/chat/channels/1101')])
        self.assertFalse(backend.rooms)

    def test_403_and_unknown_status_remain_fatal_without_join_replay(self):
        auth=self.authorized();backend,api,hub=self.adapters(auth)
        for status in (403,422):
            bot,fixture=self.native_bot(api)
            self.provider.wire('/api/v2/chat/channels/'+str(bot.channel_id),status,b'private_response')
            with auth.verified_owner(),patch('bot.bot.time.sleep') as delay,self.assertRaises(AuthError) as caught:
                bot._recover_hub_transport('invented strict refusal')
            self.assertEqual(caught.exception.code,'provider_api_unavailable');self.assertEqual(caught.exception.http_status,status)
            self.assertNotIn('private_response',str(caught.exception));delay.assert_not_called()
            self.assertEqual(bot.hub.join_room.call_count,1)
            self.assertEqual([method for method,args in fixture.hub.calls].count('JoinRoom'),0)
        self.assertFalse(backend.rooms)

class RefereeAuditTests(unittest.TestCase):
    def test_closed_failure_log_never_prints_exception_payload_or_unknown_code(self):
        errors=[AuthError('referee_event_refused'),AuthError('private_token_https://elsewhere'),RuntimeError('private_token https://elsewhere')]
        with self.assertLogs('poolrotater_host.provider_referee',level='WARNING') as captured:
            for error in errors:_log_failure(error,7)
        self.assertIn('code=referee_event_refused generation=7',captured.output[0])
        self.assertTrue(all('code=referee_transport_uncertain' in row for row in captured.output[1:]))
        self.assertFalse(any('private_token' in row or 'https://' in row for row in captured.output))

    def test_native_manager_logs_actual_closed_failure_before_retirement(self):
        # Manager sequencing is native; only the external wire-open fault is
        # injected. It must retain/reap/stop as before, not swallow the failure.
        client=object.__new__(ProviderRefereeClient)
        import threading
        client._session=None;client._stop=threading.Event();client._attempt=threading.Event();client._connect_generation=3
        client._open=Mock(side_effect=AuthError('referee_socket_closed'));client._retire=Mock()
        with self.assertLogs('poolrotater_host.provider_referee',level='WARNING') as captured:client._run()
        self.assertEqual(client._failure.code,'referee_socket_closed');client._retire.assert_called_once()
        self.assertTrue(client._attempt.is_set());self.assertIn('code=referee_socket_closed generation=3',captured.output[0])
