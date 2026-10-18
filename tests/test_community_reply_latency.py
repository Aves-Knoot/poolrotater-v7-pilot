"""Main-thread responsiveness, inherited greetings and local snapshot reads."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from contextlib import ExitStack
import json
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch
from bot.api import ApiAdmissionDeferred, ApiKeepaliveUnavailable, ApiChatReadUnavailable
from bot.bot import LobbyBot
from bot.custom_greetings import DEFAULT_JOIN_GREETINGS, configured_greeting
from tests import test_room_chat_backpressure as chat_fixture
from tests import test_chat_admission_retry as maintenance_fixture
from tests import test_local_statistics_replica as replica_fixture


class CommunityGreetingsTests(unittest.TestCase):
    def test_shared_defaults_work_without_host_integration_configuration(self):
        self.assertEqual(len(DEFAULT_JOIN_GREETINGS), 6)
        for key, message in DEFAULT_JOIN_GREETINGS.items():
            uid, name = (int(key), 'unrelated') if key.isdigit() else (999999999, key.upper())
            self.assertEqual(configured_greeting({}, uid, name), message)

    def test_explicit_local_mapping_and_empty_optout_preserved(self):
        key = next(k for k in DEFAULT_JOIN_GREETINGS if not k.isdigit())
        self.assertIsNone(configured_greeting({'custom_join_greetings': {}}, 7, key))
        self.assertEqual(configured_greeting({'custom_join_greetings': {key: 'Local welcome'}}, 7, key), 'Local welcome')
        self.assertIsNone(configured_greeting({'custom_join_greetings': None}, 7, key))


class ChatLatencyTests(unittest.TestCase):
    bot = chat_fixture.RoomChatBackpressureTests.bot
    clock = chat_fixture.RoomChatBackpressureTests.clock

    def test_multiple_replies_never_sleep_the_room_thread(self):
        b = self.bot(); b.api.chat_send.return_value = {'message_id': 1}
        with self.clock(100):
            b.say('First reply'); b.say('Second reply'); b.say('Third reply')
        self.assertEqual(b.api.chat_send.call_count, 1)
        self.assertEqual([r['text'] for r in b._pending_room_chat], ['Second reply', 'Third reply'])
        with self.clock(102): b._flush_room_chat()
        self.assertEqual(b.api.chat_send.call_count, 2)

    def test_replies_precede_automatic_backlog_but_keep_command_order(self):
        b = self.bot(); b.api.chat_send.side_effect = ApiAdmissionDeferred('busy')
        with self.clock(100):
            b.say('Reminder', automatic=True); b.say('Welcome', automatic=True)
            b.say('First reply'); b.say('Second reply')
        self.assertEqual([r['text'] for r in b._pending_room_chat],
                         ['First reply', 'Second reply', 'Reminder', 'Welcome'])
        b.api.chat_send.side_effect = None
        b.api.chat_send.return_value = {'message_id': 1}
        with self.clock(102): b._flush_room_chat()
        self.assertEqual(b.api.chat_send.call_args.args[1], 'First reply')


class MaintenanceLatencyTests(unittest.TestCase):
    bot = maintenance_fixture.ChatAdmissionRetryTests.bot

    def test_network_ack_failure_does_not_delay_incoming_commands(self):
        b = self.bot(); b.api.chat_ack.side_effect = ApiKeepaliveUnavailable('temporary')
        b.api.chat_messages.return_value = [{'message_id': 78}]
        with patch('bot.bot.time.monotonic', return_value=100):
            self.assertFalse(b._chat_api_call('chat_ack')[0])
            self.assertTrue(b._chat_api_call('chat_messages', 11, since=77)[0])
            self.assertFalse(b._chat_api_call('chat_ack')[0])
        self.assertEqual(b.api.chat_ack.call_count, 1)

    def test_read_outage_does_not_delay_ack_and_retains_network_backoff(self):
        b = self.bot(); b.api.chat_messages.side_effect = ApiChatReadUnavailable('temporary')
        delays = []
        for now in range(100, 240, 20):
            with patch('bot.bot.time.monotonic', return_value=now):
                self.assertFalse(b._chat_api_call('chat_messages', 11)[0])
                delays.append(b._chat_maintenance_retries['chat_messages']['at']-now)
                self.assertTrue(b._chat_api_call('chat_ack')[0])
        self.assertEqual(delays, [1, 2, 4, 8, 15, 15, 15])


class LocalSnapshotLatencyTests(unittest.TestCase):
    setUpClass = classmethod(replica_fixture.ReplicaTests.setUpClass.__func__)
    tearDownClass = classmethod(replica_fixture.ReplicaTests.tearDownClass.__func__)
    setUp = replica_fixture.ReplicaTests.setUp
    stop = replica_fixture.ReplicaTests.stop
    enable = replica_fixture.ReplicaTests.enable
    presence = replica_fixture.ReplicaTests.presence
    _publish_presence = replica_fixture.ReplicaTests._publish_presence
    _new_generation = replica_fixture.ReplicaTests._new_generation
    _fresh = replica_fixture.ReplicaTests._fresh

    def test_complete_local_stats_read_during_background_writer_without_http(self):
        self._publish_presence(); self._new_generation()
        from poolrotater_host.activity.reader import Reader
        from poolrotater_host.activity.replica import Replica
        reader = Reader(self.root)
        expected = reader.user_stats(7)['total_lobby_seconds']
        connection = sqlite3.connect(reader.path, timeout=.1)
        self.addCleanup(connection.close)
        connection.execute('BEGIN IMMEDIATE')
        with patch.object(self.client, 'transport', side_effect=AssertionError('no command HTTP')), \
             patch('poolrotater_host.activity.reader.connection', side_effect=AssertionError('no command writer')):
            start = time.monotonic()
            self.assertEqual(reader.lookup('Invented Player'), (7, 'Invented Player'))
            self.assertEqual(reader.user_stats(7)['total_lobby_seconds'], expected)
            self.assertFalse(reader.hidden(7))
            self.assertEqual(Replica(self.root).status()['phase'], 'ready')
            elapsed = time.monotonic()-start
        self.assertLess(elapsed, .5)
        print('LOCAL_STATS_CONCURRENT_READ '+json.dumps({'seconds': elapsed, 'network_requests': 0, 'writer_reservations': 0}))
        connection.rollback()


if __name__ == '__main__': unittest.main()
