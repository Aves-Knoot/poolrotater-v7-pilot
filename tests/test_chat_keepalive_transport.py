"""ACK-only transport recovery keeps uncertainty/cooldown and write fencing."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import multiprocessing
import socket
import time
import unittest
from unittest.mock import patch

from bot.api import ApiAdmissionDeferred, ApiKeepaliveUnavailable
from bot.hub import HubInvocationUncertain
from poolrotater_host.layout import read_json
from poolrotater_host.oauth import AuthError
from poolrotater_host.provider_api import ProviderKeepaliveUnavailable, ProviderChatDeliveryUncertain, ProviderScoreReadUnavailable
from poolrotater_host.provider_pacing import ProviderAdmissionDeferred
from rolling.adapters import Api
from rolling.protocol import Peer, RemoteError, UncertainResult
from tests.test_v7_provider_rooms import RoomCase


class KeepaliveWireTests(RoomCase):
    def setup_api(self):
        auth=self.authorized();backend,api,_hub=self.adapters(auth)
        return auth,backend,api

    def cooldown(self,auth):
        value=read_json(auth.transport.gate.root/auth.transport.gate.state_path)
        self.assertEqual(value['phase'],'idle')
        self.assertEqual(value['counters']['uncertain'],1)
        self.assertGreaterEqual(value['blocked']-value['clock']['monotonic'],59)
        return value

    def test_expiry_after_admission_before_dispatch_remains_typed_and_does_not_send(self):
        from poolrotater_host.provider_transport import _Permit
        auth,backend,api=self.setup_api()
        original=_Permit.check
        def expire(permit):
            permit.deadline=time.monotonic()-1
            return original(permit)
        with auth.verified_owner():
            paths_before=len(self.provider.calls)
            for name,args in [('chat_ack',()),('chat_messages',(1101,)),
                              ('chat_send',(1101,'must not send')),
                              ('room_item_scores',(101,1))]:
                with self.subTest(method=name),patch.object(_Permit,'check',expire):
                    expected=ProviderScoreReadUnavailable if name=='room_item_scores' else ProviderAdmissionDeferred
                    with self.assertRaises(expected)as caught:
                        getattr(api,name)(*args)
                    self.assertEqual(caught.exception.code,'provider_score_read_unavailable' if name=='room_item_scores' else 'provider_deadline_exceeded')
                # A reservation can exist, but the transport never used it.
                gate=auth.transport.gate.snapshot()
                self.assertEqual(gate['phase'],'idle')
                self.assertEqual(gate['counters']['uncertain'],0)
            self.assertEqual(len(self.provider.calls),paths_before)
        self.assertFalse(backend.api.messages)

    def test_expired_used_or_inactive_permit_still_refuses_instead_of_deferring(self):
        from poolrotater_host.provider_transport import _Permit
        auth,backend,api=self.setup_api()
        original=_Permit.check
        for field in ('used','active'):
            def refuse(permit):
                permit.deadline=time.monotonic()-1
                setattr(permit,field,field=='used')
                return original(permit)
            with self.subTest(field=field),auth.verified_owner(),patch.object(_Permit,'check',refuse):
                with self.assertRaises(AuthError)as caught:api.chat_ack()
                self.assertNotIsInstance(caught.exception,ProviderAdmissionDeferred)
                self.assertNotIsInstance(caught.exception,ProviderKeepaliveUnavailable)

    def test_actual_ack_deadline_is_typed_child_reaped_and_cooldown_retained(self):
        auth,backend,api=self.setup_api()
        with auth.verified_owner():
            auth.transport.timeout=.45
            before={child.pid for child in multiprocessing.active_children()}
            # Real TLS accepts the ACK but stalls its body past the owned deadline.
            self.provider.wire('/api/v2/chat/ack',200,b'{"silences":[]}',delay=1.5)
            started=time.monotonic()
            with self.assertRaises(ProviderKeepaliveUnavailable)as caught:api.chat_ack()
            self.assertIn(caught.exception.code,('provider_deadline_exceeded','provider_transport_uncertain'))
            self.assertIsNone(caught.exception.http_status)
            self.assertLess(time.monotonic()-started,1.5)
            self.assertEqual({child.pid for child in multiprocessing.active_children()},before)
            before_counters=self.cooldown(auth)['counters']
            with self.assertRaises(ProviderAdmissionDeferred):api.chat_ack()
            after=auth.transport.gate.snapshot()['counters']
            self.assertEqual(after['dispatched'],before_counters['dispatched'])
            self.assertEqual(after['uncertain'],1)
        self.assertFalse(backend.api.messages)

    def test_actual_ack_lost_tls_reply_is_typed_without_replay(self):
        auth,backend,api=self.setup_api()
        original=self.provider.api_handler
        def lost(method,path,body,bearer,headers):
            result=original(method,path,body,bearer,headers)
            return ('disconnect',None) if path=='/api/v2/chat/ack' else result
        self.provider.api_handler=lost
        with auth.verified_owner():
            before=auth.transport.gate.snapshot()['counters']['dispatched']
            with self.assertRaises(ProviderKeepaliveUnavailable)as caught:api.chat_ack()
            self.assertEqual(caught.exception.code,'provider_transport_uncertain')
            self.assertIsNone(caught.exception.http_status)
            self.assertEqual(self.cooldown(auth)['counters']['dispatched'],before+1)
        self.assertFalse(backend.api.messages)

    def test_arbitrary_postdispatch_exception_remains_fatal(self):
        auth,backend,api=self.setup_api()
        with auth.verified_owner(),patch('poolrotater_host.provider_transport._owned_exchange',side_effect=RuntimeError('invented unexpected failure')):
            with self.assertRaises(RuntimeError)as caught:api.chat_ack()
            self.assertNotIsInstance(caught.exception,ProviderKeepaliveUnavailable)
            self.cooldown(auth)
        self.assertFalse(backend.api.messages)

    def test_actual_ack_http_503_is_typed_status_safe_no_inline_retry(self):
        auth,backend,api=self.setup_api()
        with auth.verified_owner():
            self.provider.wire('/api/v2/chat/ack',503,b'private response marker')
            before=auth.transport.gate.snapshot()['counters']['dispatched']
            with self.assertRaises(ProviderKeepaliveUnavailable)as caught:api.chat_ack()
            self.assertEqual(caught.exception.code,'provider_chat_keepalive_unavailable')
            self.assertEqual(caught.exception.http_status,503)
            self.assertNotIn('private response marker',str(caught.exception))
            self.assertEqual(self.cooldown(auth)['counters']['dispatched'],before+1)
            with self.assertRaises(ProviderAdmissionDeferred):api.chat_send(1101,'not dispatched')
        self.assertFalse(backend.api.messages)

    def test_actual_ack_401_remains_fatal_no_refresh_or_replay(self):
        auth,backend,api=self.setup_api()
        with auth.verified_owner():
            self.provider.wire('/api/v2/chat/ack',401,b'private response marker')
            before=auth.transport.gate.snapshot()['counters']['dispatched']
            with self.assertRaises(AuthError)as caught:api.chat_ack()
            self.assertNotIsInstance(caught.exception,ProviderKeepaliveUnavailable)
            self.assertEqual(caught.exception.code,'provider_api_unauthorized')
            self.assertEqual(caught.exception.http_status,401)
            self.assertEqual(auth.transport.gate.snapshot()['counters']['dispatched'],before+1)
            self.assertEqual(auth.transport.gate.snapshot()['counters']['uncertain'],0)
        self.assertFalse(backend.api.messages)

    def test_actual_ack_malformed_success_defers_without_replaying_or_advancing_ack(self):
        auth,backend,api=self.setup_api()
        with auth.verified_owner():
            self.provider.wire('/api/v2/chat/ack',200,b'[]')
            with self.assertRaises(AuthError)as caught:api.chat_ack()
            self.assertIsInstance(caught.exception,ProviderKeepaliveUnavailable)
            self.assertEqual(caught.exception.code,'provider_chat_keepalive_reply_refused')
            self.cooldown(auth)

    def test_postdispatch_guard_cleanup_and_arbitrary_errors_remain_fatal(self):
        # The real owned permit records uncertain state; only its final exchange
        # is injected to qualify negative classifications without corrupting files.
        for code in ('provider_child_cleanup_uncertain','provider_pacing_state_refused',
                     'provider_api_unauthorized','operator_client_changed','provider_private_unknown'):
            with self.subTest(code=code):
                auth,backend,api=self.setup_api()
                with auth.verified_owner(),patch('poolrotater_host.provider_transport._owned_exchange',side_effect=AuthError(code)):
                    with self.assertRaises(AuthError)as caught:api.chat_ack()
                    self.assertNotIsInstance(caught.exception,ProviderKeepaliveUnavailable)
                    self.assertEqual(caught.exception.code,'provider_api_mutation_uncertain')
                    self.cooldown(auth)
                # Each case uses a fresh local runtime setup; persistent cooldown
                # must not be reset to force another request through the same gate.
                if code!='provider_private_unknown':RoomCase.setUp(self)

    def test_uncertain_chat_send_503_is_typed_and_unreplayed(self):
        auth,backend,api=self.setup_api()
        with auth.verified_owner():
            self.provider.wire('/api/v2/chat/channels/1101/messages',503,b'private response marker')
            before=auth.transport.gate.snapshot()['counters']['dispatched']
            with self.assertRaises(AuthError)as caught:api.chat_send(1101,'once')
            self.assertNotIsInstance(caught.exception,ProviderKeepaliveUnavailable)
            self.assertIsInstance(caught.exception,ProviderChatDeliveryUncertain)
            self.assertEqual(caught.exception.code,'provider_chat_delivery_uncertain')
            self.assertEqual(self.cooldown(auth)['counters']['dispatched'],before+1)
        self.assertFalse(backend.api.messages)


class KeepaliveRPCTests(unittest.TestCase):
    def peers(self,error):
        a,b=socket.socketpair()
        def fail(*args,**kwargs):raise error
        server=Peer(a,handler=fail).start();client=Peer(b).start()
        self.addCleanup(server.close);self.addCleanup(client.close)
        return Api(client)

    def test_actual_rpc_keepalive_type_maps_only_ack(self):
        api=self.peers(ProviderKeepaliveUnavailable('provider_chat_keepalive_unavailable',http_status=503))
        with self.assertRaises(ApiKeepaliveUnavailable)as caught:api.chat_ack()
        self.assertIn('HTTP 503',str(caught.exception))
        for name,args in [('chat_send',(1101,'once')),('chat_messages',(1101,)),('beatmap',(123,))]:
            with self.assertRaises(RemoteError):getattr(api,name)(*args)

    def test_actual_rpc_predispatch_send_is_safe_deferred_not_keepalive(self):
        api=self.peers(ProviderAdmissionDeferred('provider_pacing_deferred'))
        for name,args in [('chat_ack',()),('chat_send',(1101,'later')),('chat_messages',(1101,))]:
            with self.assertRaises(ApiAdmissionDeferred):getattr(api,name)(*args)
        with self.assertRaises(RemoteError):api.beatmap(123)

    def test_uncertain_rpc_is_never_typed_no_dispatch_or_keepalive(self):
        from unittest.mock import Mock
        peer=Mock();peer.call.side_effect=UncertainResult('reply missing')
        for name,args in [('chat_ack',()),('chat_send',(1101,'once'))]:
            with self.assertRaises(HubInvocationUncertain):getattr(Api(peer),name)(*args)


if __name__=='__main__':unittest.main()
