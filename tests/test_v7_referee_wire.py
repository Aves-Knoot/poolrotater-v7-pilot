"""RFC6455/SignalR framing and lifetime-authorized invented TLS integration."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import io
import multiprocessing
import queue
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

from bot.hub import HubDisconnected, HubInvocationUncertain
from poolrotater_host.oauth import AuthError
from poolrotater_host.provider_referee import _ProviderEvent
from poolrotater_host.referee_contract import NEGOTIATE, negotiation, prepare_referee
from poolrotater_host.referee_protocol import Frames, Records, decoded, encoded, frame, message, MAX_MESSAGE
from poolrotater_host.referee_wire import ChildSession
from test_v7_provider_rooms import RoomCase


class FrameTests(unittest.TestCase):
    def read(self, raw, masked=False):
        sock = unittest.mock.Mock()
        return Frames(sock,expect_masked=masked,read=io.BytesIO(raw).read)

    def test_masked_extended_lengths_round_trip(self):
        for size in (0,125,126,65535,65536,MAX_MESSAGE):
            raw=b'a'*size
            self.assertEqual(self.read(frame(raw,masked=True),True).receive(),(1,raw))

    def test_fragmented_utf8_allows_interleaved_control(self):
        raw=frame(b'\xe3',final=False)+frame(b'ping',opcode=9)+frame(b'\x81\x82',opcode=0)
        reader=self.read(raw)
        self.assertEqual(reader.receive(),(None,None))
        self.assertEqual(reader.receive(),(9,b'ping'))
        self.assertEqual(reader.receive(),(1,'あ'.encode()))

    def test_invalid_masks_opcodes_lengths_and_utf8_refuse(self):
        cases=(b'\xc1\x00',b'\x82\x00',b'\x80\x00',b'\x81\x7e\x00\x01a',
               b'\x81\x7f'+struct.pack('!Q',MAX_MESSAGE+1),frame(b'\xff'),
               frame(b'a',masked=True),b'\x09\x00',frame(b'a',opcode=8))
        for raw in cases:
            with self.subTest(raw=raw[:8]),self.assertRaises(AuthError):self.read(raw).receive()

    def test_fragment_deadline_applies_during_continuation_read(self):
        reader=self.read(frame(b'a',final=False)+frame(b'b',opcode=0))
        reader.receive();reader.fragment_deadline=time.monotonic()+.01
        original=reader.read
        def slow(size):time.sleep(.02);return original(size)
        reader.read=slow
        with self.assertRaises(AuthError):reader.receive()

    def test_partial_records_and_batched_handshake(self):
        records=Records()
        self.assertEqual(records.feed(b'{}\x1e{"type":'),[{}])
        self.assertEqual(records.feed(b'6}\x1e'),[{'type':6}])
        self.assertIsNone(records.deadline)

    def test_record_deadline_size_and_flood(self):
        records=Records();records.feed(b'{');records.deadline=time.monotonic()-1
        with self.assertRaises(AuthError):records.feed(b'}\x1e')
        for raw in (b'x'*(MAX_MESSAGE+1),b'{}\x1e'*257,b'\x1e'):
            with self.assertRaises(AuthError):Records().feed(raw)

    def test_json_duplicate_nonfinite_and_non_object_refuse(self):
        for raw in (b'{"type":6,"type":7}',b'{"x":NaN}',b'{"x":1e999}',b'[]',b'\xff'):
            with self.assertRaises(AuthError):decoded(raw)
        with self.assertRaises(AuthError):encoded({'x':float('inf')})

    def test_message_types_and_completion_contract(self):
        for value in ({'type':True},{'type':8},{'type':3,'invocationId':'0'},
                      {'type':3,'invocationId':'1','result':{},'error':'x'},
                      {'type':1,'target':'UserJoined','arguments':[],'invocationId':'1'},
                      {'type':7,'allowReconnect':1}):
            with self.assertRaises(AuthError):message(value)
        self.assertEqual(message({'type':3,'invocationId':'1'}),3)

    def test_negotiation_rejects_redirect_tokens_versions_and_fallback(self):
        base={'negotiateVersion':1,'connectionId':'public','connectionToken':'secret',
              'availableTransports':[{'transport':'WebSockets','transferFormats':['Text']}]}
        self.assertEqual(negotiation(base),'secret')
        for change in ({'url':'https://invalid'},{'accessToken':'secret'},{'negotiateVersion':True},
                       {'useAck':True},{'connectionToken':''},
                       {'availableTransports':[{'transport':'LongPolling','transferFormats':['Text']}]}):
            with self.assertRaises(AuthError):negotiation(base|change)
        self.assertNotIn('private-bearer',repr(prepare_referee('private-bearer')))

    def test_disabled_target_refuses_before_child_or_dns(self):
        with patch('multiprocessing.get_context') as process,patch('socket.create_connection') as dns:
            with self.assertRaises(AuthError) as error:ChildSession(None,None,'invented')
        self.assertEqual(error.exception.code,'live_provider_disabled')
        process.assert_not_called();dns.assert_not_called()

    def test_prepared_negotiation_is_revalidated_before_http(self):
        from dataclasses import replace
        from poolrotater_host.provider_contract import _exchange_checked
        original=prepare_referee('invented')
        for request in (replace(original,path='/other'),replace(original,method='GET'),
                        replace(original,headers=original.headers|{'X-Extra':'unexpected'}),
                        replace(original,body=b'{}'),replace(original,maximum_reply=MAX_MESSAGE)):
            connection=unittest.mock.Mock()
            with self.assertRaises(AuthError) as error:_exchange_checked(connection,request,timeout=1,on_status=None)
            self.assertEqual(error.exception.code,'invalid_prepared_request')
            connection.request.assert_not_called()

    def test_dormant_referee_entry_retains_host_fence(self):
        import http.client
        from poolrotater_host import referee_contract
        connection=http.client.HTTPSConnection('127.0.0.1')
        with patch.object(referee_contract,'require_live_provider'),patch.object(connection,'request') as dispatch:
            with self.assertRaises(AuthError) as error:
                referee_contract.exchange_referee_upstream(connection,prepare_referee('invented'),timeout=1,on_status=None)
        self.assertEqual(error.exception.code,'fixed_referee_required');dispatch.assert_not_called()


class WireTests(RoomCase):
    def start(self):
        auth=self.authorized();backend,api,hub=self.adapters(auth)
        return auth,backend,api,hub

    def test_create_join_mutate_complete_over_tls_websocket(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();room=hub.make_room('invented-wire',1)
            rid=room['room_id'];self.assertEqual(hub.invoke('JoinRoom',[rid])['room_id'],rid)
            hub.invoke('ChangeRoomSettings',[rid,{'name':'updated'}])
            self.assertEqual(backend.rooms[rid]['name'],'updated')
            hub.invoke('CloseRoom',[rid]);hub.stop()
        self.assertEqual(backend.wire.connections,1)
        self.assertEqual([v['target'] for v in backend.wire.commands],
                         ['MakeRoom','JoinRoom','ChangeRoomSettings','CloseRoom'])
        self.assertTrue(backend.wire.upgrades[0]['path'].startswith('/referee?id=invented-connection-'))
        self.assertNotIn(backend.wire.upgrades[0]['bearer'],backend.wire.upgrades[0]['path'])
        self.assertFalse(backend.rooms)

    def test_events_do_not_block_command_completion(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();rid=hub.make_room('invented',1)['room_id']
            hub.invoke('AddPlaylistItem',[rid,{'beatmap_id':2,'ruleset_id':0}])
            event=hub.client.events.get(timeout=2)
            self.assertIs(type(event),_ProviderEvent)
            self.assertEqual(event.name,'PlaylistItemAdded');self.assertEqual(event.payload['room_id'],rid)
            self.assertEqual(event.generation,hub._connect_generation)
            self.assertGreater(event.sequence,0)
            self.assertFalse(hub.roster_progress()[2])
            hub.stop()

    def test_wire_roster_event_uses_local_generation_instead_of_payload_tag(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();rid=hub.make_room('invented',1)['room_id']
            current=hub._connect_generation
            for name,key in (('UserJoined','user_id'),('UserLeft','user_id'),('UserKicked','kicked_user_id')):
                payload={'room_id':rid,key:7,'generation':current+999}
                backend.wire.route(name,payload)
                event=hub.client.events.get(timeout=2)
                self.assertIs(type(event),_ProviderEvent)
                self.assertEqual(event.name,name);self.assertEqual(event.payload,payload)
                self.assertEqual(event.generation,current)
                self.assertGreater(event.sequence,0)
                self.assertTrue(hub.roster_progress()[2])
                hub.acknowledge_event(event)
                self.assertFalse(hub.roster_progress()[2])
            hub.stop()

    def test_wire_roster_receipts_require_each_ack_despite_reordered_consumers(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();rid=hub.make_room('invented',1)['room_id']
            backend.wire.route('UserLeft',{'room_id':rid,'user_id':7})
            held=hub.client.events.get(timeout=2)
            backend.wire.route('UserJoined',{'room_id':rid,'user_id':8})
            newer=hub.client.events.get(timeout=2)
            self.assertIs(type(held),_ProviderEvent);self.assertIs(type(newer),_ProviderEvent)
            self.assertLess(held.sequence,newer.sequence)
            self.assertTrue(hub.roster_progress()[2])
            hub.acknowledge_event(newer)
            self.assertTrue(hub.roster_progress()[2])
            hub.acknowledge_event(held)
            self.assertFalse(hub.roster_progress()[2])
            hub.stop()

    def test_lost_create_completion_is_once_and_child_reaped(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();process=hub.client._session.process;pid=process.pid
            backend.fault('MakeRoom','after')
            with self.assertRaises(HubInvocationUncertain):hub.make_room('invented',1)
            hub.stop()
        self.assertEqual(backend.authenticated_calls.count('MakeRoom'),1)
        self.assertEqual(len(backend.rooms),1)
        self.assertFalse(any(child.pid==pid for child in multiprocessing.active_children()))

    def test_command_error_redacted_without_retry(self):
        auth,backend,api,hub=self.start();backend.wire.completion_error='private-bearer raw upstream error'
        with auth.verified_owner():
            hub.connect()
            with self.assertRaises(RuntimeError) as error:hub.make_room('invented',1)
            self.assertEqual(str(error.exception),'referee_command_rejected')
            self.assertTrue(hub.is_connected());hub.stop()
        self.assertEqual(len(backend.wire.commands),1);self.assertFalse(backend.rooms)

    def test_negotiation_redirect_is_refused_without_upgrade(self):
        auth,backend,api,hub=self.start()
        self.provider.wire(NEGOTIATE,302,headers=[('Location','https://invalid.example/'),('Content-Length','0')])
        with auth.verified_owner():self.error('provider_redirect_refused',hub.connect)
        self.assertFalse(backend.wire.upgrades);self.assertFalse(backend.authenticated_calls)

    def test_malformed_negotiation_retires_worker(self):
        auth,backend,api,hub=self.start()
        self.provider.wire(NEGOTIATE,200,b'{"negotiateVersion":1,"url":"https://invalid"}')
        with auth.verified_owner():self.error('referee_negotiation_refused',hub.connect)
        self.assertIsNone(hub.client._session)

    def test_upgrade_accept_verified_before_handshake(self):
        auth,backend,api,hub=self.start();backend.wire.bad_accept=True
        with auth.verified_owner():self.error('referee_upgrade_refused',hub.connect)
        self.assertEqual(backend.wire.connections,0);self.assertIsNone(hub.client._session)

    def test_handshake_error_refuses_and_redacts(self):
        auth,backend,api,hub=self.start();backend.wire.handshake_reply={'error':'private-bearer'}
        with auth.verified_owner():self.error('referee_handshake_refused',hub.connect)
        self.assertIsNone(hub.client._session)

    def test_handshake_stall_has_deadline_and_reaps_child(self):
        auth,backend,api,hub=self.start();backend.wire.handshake_delay=20
        started=time.monotonic()
        with auth.verified_owner():
            with self.assertRaises(AuthError):hub.connect()
            hub.stop()
        self.assertLess(time.monotonic()-started,9);self.assertIsNone(hub.client._session)

    def test_invalid_command_does_not_dispatch(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect()
            for method,args in (('Unknown',[]),('MakeRoom',()),('MakeRoom',[float('nan')])):
                with self.assertRaises(AuthError):hub.client.invoke(method,args)
            for timeout in (True,0,float('inf'),16):
                with self.assertRaises(AuthError):hub.client.invoke('MakeRoom',[],timeout=timeout)
            hub.stop()
        self.assertFalse(backend.wire.commands)

    def test_reconnect_retains_rooms_and_new_generation(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();rid=hub.make_room('invented',1)['room_id'];generation=hub._connect_generation
            hub.reconnect();self.assertGreater(hub._connect_generation,generation)
            retired=hub.client.events.get(timeout=2);reconnected=hub.client.events.get(timeout=2)
            self.assertIs(type(retired),_ProviderEvent);self.assertIs(type(reconnected),_ProviderEvent)
            self.assertEqual((retired.name,retired.payload,retired.generation),
                             ('HubDisconnected',{'generation':generation},generation))
            self.assertEqual((reconnected.name,reconnected.payload,reconnected.generation),
                             ('HubReconnected',{'generation':hub._connect_generation},hub._connect_generation))
            self.assertGreater(retired.sequence,0);self.assertGreater(reconnected.sequence,retired.sequence)
            self.assertEqual(hub.invoke('JoinRoom',[rid])['room_id'],rid)
            hub.invoke('CloseRoom',[rid]);hub.stop()
        self.assertEqual(backend.wire.connections,2)
        self.assertEqual(backend.authenticated_calls.count('MakeRoom'),1)

    def test_stop_during_opening_interrupts_and_reaps(self):
        auth,backend,api,hub=self.start();backend.wire.handshake_delay=20
        errors=[]
        def connect():
            try:hub.connect()
            except AuthError as error:errors.append(error.code)
        with auth.verified_owner():
            thread=threading.Thread(target=connect);thread.start()
            deadline=time.monotonic()+4
            while not backend.wire.upgrades and time.monotonic()<deadline:time.sleep(.02)
            started=time.monotonic();hub.stop();thread.join(timeout=3)
            self.assertFalse(thread.is_alive());self.assertLess(time.monotonic()-started,3)
        self.assertTrue(errors);self.assertIsNone(hub.client._session)

    def test_owner_exit_refuses_subsequent_command(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():hub.connect()
        self.error('verified_owner_required',lambda:hub.make_room('invented',1));hub.stop()
        self.assertFalse(backend.wire.commands)

    def test_invocation_deadline_retires_connection_without_replay(self):
        auth,backend,api,hub=self.start();backend.wire.completion_delay=2
        with auth.verified_owner():
            hub.connect();started=time.monotonic()
            with self.assertRaises(HubInvocationUncertain):
                hub.client.invoke('MakeRoom',[{'name':'invented','beatmap_id':1,'ruleset_id':0}],timeout=.1)
            self.assertLess(time.monotonic()-started,2)
            hub.stop()
        # A deadline may precede delivery or follow commit. Both are uncertain;
        # the contract is bounded retirement with no replay. The separate lost
        # create-completion test deterministically establishes post-commit loss.
        self.assertLessEqual(backend.authenticated_calls.count('MakeRoom'),1)
        self.assertLessEqual(len(backend.rooms),1)

    def test_automatic_socket_reconnect_does_not_replay_create(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();rid=hub.make_room('invented',1)['room_id'];generation=hub._connect_generation
            backend.wire.drop();deadline=time.monotonic()+5
            while hub._connect_generation==generation and time.monotonic()<deadline:time.sleep(.02)
            self.assertGreater(hub._connect_generation,generation)
            self.assertEqual(hub.invoke('JoinRoom',[rid])['room_id'],rid)
            hub.stop()
        self.assertEqual(backend.authenticated_calls.count('MakeRoom'),1)

    def test_late_completion_cannot_satisfy_another_invocation(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();rid=hub.make_room('invented',1)['room_id']
            backend.wire.send({'type':3,'invocationId':'1','result':{'room_id':999}})
            self.assertEqual(hub.invoke('JoinRoom',[rid])['room_id'],rid)
            hub.stop()

    def test_event_overflow_retires_connection(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();hub.client.events=queue.Queue(maxsize=1)
            hub.client.events.put(('UserJoined',{}))
            backend.wire.route('UserJoined',{'room_id':101})
            deadline=time.monotonic()+2
            while hub.is_connected() and time.monotonic()<deadline:time.sleep(.01)
            self.assertFalse(hub.is_connected());hub.stop()

    def test_terminal_protocol_close_prevents_reconnect(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();backend.wire.send({'type':7,'allowReconnect':False,'error':'private-bearer'})
            deadline=time.monotonic()+2
            while hub.is_connected() and time.monotonic()<deadline:time.sleep(.01)
            self.assertFalse(hub.is_connected());hub.stop()
        self.assertEqual(backend.wire.connections,1)

    def test_profile_change_refuses_before_existing_socket_mutation(self):
        from poolrotater_host.layout import atomic_json,read_json
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();value=read_json(self.root/'secrets/provider-operator-client.json')
            value['client_secret']='changed-invented-secret'
            atomic_json(self.root/'secrets/provider-operator-client.json',value)
            self.error('operator_client_changed',lambda:hub.make_room('invented',1));hub.stop()
        self.assertFalse(backend.wire.commands)

    def test_negotiate_stall_has_owned_deadline(self):
        auth,backend,api,hub=self.start()
        self.provider.wire(NEGOTIATE,200,b'{}',delay=20)
        started=time.monotonic()
        with auth.verified_owner():
            with self.assertRaises(AuthError):hub.connect()
            hub.stop()
        self.assertLess(time.monotonic()-started,9)
        self.assertIsNone(hub.client._session);self.assertFalse(backend.wire.upgrades)

    def test_stop_during_negotiate_reaps_before_replacement(self):
        auth,backend,api,hub=self.start()
        self.provider.wire(NEGOTIATE,200,b'{}',delay=20)
        errors=[]
        def connect():
            try:hub.connect()
            except AuthError as error:errors.append(error.code)
        with auth.verified_owner():
            thread=threading.Thread(target=connect);thread.start()
            deadline=time.monotonic()+3
            while hub.client._session is None and time.monotonic()<deadline:time.sleep(.01)
            started=time.monotonic();hub.stop();thread.join(timeout=3)
            self.assertFalse(thread.is_alive());self.assertLess(time.monotonic()-started,3)
        self.assertTrue(errors);self.assertIsNone(hub.client._session)

    def test_uncertain_child_cleanup_latches_against_replacement(self):
        auth,backend,api,hub=self.start()
        with auth.verified_owner():
            hub.connect();session=hub.client._session
            with patch.object(session,'close',side_effect=AuthError('referee_child_cleanup_uncertain')):
                self.error('referee_child_cleanup_uncertain',hub.client._retire)
                with patch('poolrotater_host.provider_referee.ChildSession') as replacement:
                    self.error('referee_child_cleanup_uncertain',hub.connect)
                replacement.assert_not_called()
            hub.stop()
            self.error('referee_child_cleanup_uncertain',hub.connect)
        self.assertEqual(backend.wire.connections,1)


if __name__=='__main__':unittest.main()
