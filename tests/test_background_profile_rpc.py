"""Only optional profile GETs bypass the serialized room-control RPC lane."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import socket
import threading
import unittest
from unittest.mock import Mock
from bot.api import ApiAdmissionDeferred
from rolling.adapters import Api
from rolling.protocol import Peer, RemoteError


class BackgroundProfileRPCTests(unittest.TestCase):
    def peers(self, handler, enabled=True):
        a, b = socket.socketpair()
        server = Peer(a, handler=handler, background_profiles=enabled).start()
        client = Peer(b).start()
        self.addCleanup(server.close); self.addCleanup(client.close)
        return client

    def caller(self, operation):
        result = []
        def call():
            try: result.append(operation())
            except Exception as error: result.append(error)
        thread = threading.Thread(target=call)
        thread.start()
        self.addCleanup(thread.join, 2)
        return thread, result

    def test_blocked_profile_does_not_hold_chat_or_room_control_and_second_profile_defers(self):
        entered = threading.Event(); release = threading.Event()
        self.addCleanup(release.set)
        def handler(method, *args, **kwargs):
            if method == 'api.user':
                entered.set()
                if not release.wait(2): raise AssertionError('test profile not released')
                return {'id': 7}
            return method
        client = self.peers(handler)
        thread, result = self.caller(lambda: Api(client).user(7))
        self.assertTrue(entered.wait(1))
        self.assertEqual(Api(client).chat_messages(11), 'api.chat_messages')
        self.assertEqual(client.call('hub.is_connected', timeout=.5), 'hub.is_connected')
        with self.assertRaises(ApiAdmissionDeferred): Api(client).user_top_scores(7)
        release.set(); thread.join(1)
        self.assertEqual(result, [{'id': 7}])
        self.assertEqual(Api(client).user_top_scores(7), 'api.user_top_scores')

    def test_mutations_keep_single_serial_execution(self):
        entered = threading.Event(); release = threading.Event(); calls = []
        self.addCleanup(release.set)
        def handler(method, *args):
            calls.append(method)
            if method == 'hub.first': entered.set(); release.wait(2)
            return method
        client = self.peers(handler)
        first, one = self.caller(lambda: client.call('hub.first', timeout=2))
        self.assertTrue(entered.wait(1))
        second, two = self.caller(lambda: client.call('hub.second', timeout=2))
        # A queued second mutation cannot have completed while first is held.
        second.join(.05)
        self.assertTrue(second.is_alive()); self.assertEqual(calls, ['hub.first'])
        release.set(); first.join(1); second.join(1)
        self.assertEqual(one, ['hub.first']); self.assertEqual(two, ['hub.second'])

    def test_old_peer_default_and_uncertain_results_preserve_wire_contract(self):
        client = self.peers(lambda method, *args: method, enabled=False)
        self.assertEqual(Api(client).user(7), 'api.user')
        fake = Mock(); fake.call.side_effect = RemoteError('BackgroundReadDeferred', 'busy')
        with self.assertRaises(RemoteError): Api(fake).chat_send(11, 'Never relabel a mutation')


if __name__ == '__main__': unittest.main()
