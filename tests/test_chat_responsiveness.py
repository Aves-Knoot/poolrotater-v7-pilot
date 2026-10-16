"""Live commands precede maintenance without introducing parallel mutations."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from collections import deque
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bot.api import ApiAdmissionDeferred
from bot.bot import LobbyBot
from bot.chat_websocket import ChatSocketEvent
from rolling.protocol import RemoteError
from tests import test_headless_multiroom as fixtures


class LiveChatPriorityTests(unittest.TestCase):
    def bot(self):
        b = object.__new__(LobbyBot)
        b.cfg = {}; b.state = 'IDLE'; b.room_id = 10; b.channel_id = 20
        b.last_message_id = 0; b._last_ack = 0; b._last_chat_poll = 0
        b.api = Mock(); b.api.chat_messages.return_value = []
        b.chat_websocket = SimpleNamespace(enabled=True, connected=True)
        b._chat_websocket_generation = 1; b._chat_websocket_needs_reconcile = False
        b._last_chat_websocket_reconcile = 100
        b._chat_websocket_inbox = queue.Queue()
        b._chat_patrol_enabled = Mock(return_value=False)
        b._process_chat_messages = Mock()
        return b

    def message(self, b, mid=1):
        return ChatSocketEvent('messages', 1, ({'message_id': mid},), b.channel_id)

    def test_ready_live_batch_runs_before_a_slow_keepalive(self):
        b = self.bot(); observed = []; clock = [100.0]
        b._chat_websocket_inbox.put(self.message(b))
        b._process_chat_messages.side_effect = lambda rows: observed.append(('command', clock[0]))
        def ack():
            clock[0] += 5
            observed.append(('ack', clock[0]))
        b.api.chat_ack.side_effect = ack
        with patch('bot.bot.time.time', side_effect=lambda: clock[0]): b.poll_chat()
        self.assertEqual(observed, [('command', 100.0), ('ack', 105.0)])
        b.api.chat_messages.assert_not_called()

    def test_reconnect_recovers_history_before_live_and_before_ack(self):
        b = self.bot(); observed = []
        b._chat_websocket_needs_reconcile = True
        b._chat_websocket_inbox.put(self.message(b, 2))
        b.api.chat_messages.return_value = [{'message_id': 1}]
        b._process_chat_messages.side_effect = lambda rows: observed.append(rows[0]['message_id'])
        b.api.chat_ack.side_effect = lambda: observed.append('ack')
        with patch('bot.bot.time.time', return_value=100): b.poll_chat()
        self.assertEqual(observed, [1, 2, 'ack'])
        self.assertFalse(b._chat_websocket_needs_reconcile)

    def test_deferred_history_retains_live_batch_and_cursor(self):
        b = self.bot(); b._chat_websocket_needs_reconcile = True
        b.api.chat_messages.side_effect = ApiAdmissionDeferred('before dispatch')
        b._chat_websocket_inbox.put(self.message(b))
        with patch('bot.bot.time.time', return_value=100), patch('bot.bot.time.monotonic', return_value=100):
            b.poll_chat()
        b._process_chat_messages.assert_not_called()
        self.assertEqual(b.last_message_id, 0)
        self.assertEqual(b._chat_websocket_inbox.qsize(), 1)
        self.assertTrue(b._chat_websocket_needs_reconcile)

    def test_fatal_keepalive_still_raises_after_already_received_command(self):
        b = self.bot(); b._chat_websocket_inbox.put(self.message(b))
        b.api.chat_ack.side_effect = RemoteError('AuthError', 'authority refused')
        with patch('bot.bot.time.time', return_value=100), self.assertRaises(RemoteError): b.poll_chat()
        b._process_chat_messages.assert_called_once_with([{'message_id': 1}])
        self.assertEqual(b._last_ack, 0)

    def test_disconnected_socket_keeps_idle_and_playing_fallback_intervals(self):
        for state, interval in (('IDLE', 5), ('PLAYING', 10)):
            b = self.bot(); b.state = state; b.chat_websocket.connected = False
            b._last_ack = 100; b._last_chat_poll = 100
            with patch('bot.bot.time.time', return_value=100 + interval - .1): b.poll_chat()
            b.api.chat_messages.assert_not_called()
            with patch('bot.bot.time.time', return_value=100 + interval): b.poll_chat()
            b.api.chat_messages.assert_called_once_with(20, since=None)

    def test_forced_check_still_reconciles_live_history_and_sends_keepalive(self):
        b = self.bot()
        with patch('bot.bot.time.time', return_value=100): b.poll_chat(force=True)
        b.api.chat_messages.assert_called_once_with(20, since=None)
        b.api.chat_ack.assert_called_once()

    def test_stale_room_socket_batch_is_discarded(self):
        b = self.bot(); b._last_ack = 100
        b._chat_websocket_inbox.put(ChatSocketEvent('messages', 1, ({'message_id': 1},), 999))
        with patch('bot.bot.time.time', return_value=100): b.poll_chat()
        b._process_chat_messages.assert_not_called()


class RoomLoopOrderingTests(unittest.TestCase):
    def bot(self):
        b = object.__new__(LobbyBot); b.running = True; b.room_id = 10; b.channel_id = 20
        b.events = queue.Queue(); b._hub_rejoin_pending = False
        b.hub = SimpleNamespace(is_connected=lambda: True)
        b.process_control = SimpleNamespace(pending_shutdown=lambda: None)
        b._activity_checkpoint_at = 100
        self.trace = []
        for name in ('_reload_config_from_disk', '_refresh_operator_exclusions',
                     '_enforce_project_bans', 'poll_chat', '_drain_ai_results',
                     '_drain_discord_control_commands', '_flush_room_chat',
                     '_flush_player_profiles', '_update_discord_public_status',
                     '_maybe_language_offer'):
            setattr(b, name, Mock(side_effect=lambda n=name: self.trace.append(n)))
        b._maybe_reset_empty_autodiff = Mock(side_effect=StopIteration)
        return b

    def test_live_chat_precedes_profiles_and_outgoing_work_on_same_thread(self):
        b = self.bot()
        with patch('bot.bot.time.monotonic', return_value=100), self.assertRaises(StopIteration): b._loop()
        self.assertEqual(self.trace[:4], ['_reload_config_from_disk',
            '_refresh_operator_exclusions', '_enforce_project_bans', 'poll_chat'])
        for name in ('_drain_ai_results', '_drain_discord_control_commands',
                     '_flush_room_chat', '_flush_player_profiles', '_update_discord_public_status'):
            self.assertLess(self.trace.index('poll_chat'), self.trace.index(name))

    def test_chat_shutdown_prevents_later_background_work(self):
        b = self.bot(); b.poll_chat.side_effect = lambda: setattr(b, 'running', False)
        with patch('bot.bot.time.monotonic', return_value=100): b._loop()
        b._flush_room_chat.assert_not_called(); b._flush_player_profiles.assert_not_called()

    def test_pending_shutdown_wins_over_incoming_commands(self):
        b = self.bot()
        b.process_control.pending_shutdown = lambda: {'target': 'room', 'requested_by': 'fixture'}
        with patch('bot.bot.time.monotonic', return_value=100): b._loop()
        b.poll_chat.assert_not_called()


class CommandDurationTests(unittest.TestCase):
    def test_samples_bounded_and_summary_does_not_include_command_content(self):
        b = object.__new__(LobbyBot)
        with patch('bot.bot.time.monotonic', return_value=100):
            for _ in range(1000): b._record_chat_command_duration(99)
        self.assertIsInstance(b._chat_command_durations, deque)
        self.assertEqual(len(b._chat_command_durations), 64)
        with patch('bot.bot.time.monotonic', return_value=161), self.assertLogs('bot', level='INFO') as logs:
            b._record_chat_command_duration(159)
        self.assertIn('samples=64', logs.output[0]); self.assertIn('max=2.000s', logs.output[0])
        self.assertIn('processing only', logs.output[0])
        with patch('bot.bot.time.monotonic', return_value=162), patch('bot.bot.log.info') as log:
            b._record_chat_command_duration(161)
        log.assert_not_called()


class CommandOrderingTests(unittest.TestCase):
    setUp = fixtures.ProductionControllerTests.setUp

    def test_timing_retains_deduplication_and_original_uncertain_command_failure(self):
        b = self.bots[0]
        failure = RemoteError('HubInvocationUncertain', 'mutation reply uncertain')
        b.handle_command = Mock(side_effect=failure)
        message = {'message_id': 5001, 'sender_id': 7, 'content': '.start'}
        with self.assertRaises(RemoteError) as raised: b._process_chat_messages([message])
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(b._chat_command_durations), 1)
        b._process_chat_messages([message])
        b.handle_command.assert_called_once()

    def test_out_of_order_batch_preserves_message_order_and_original_map_context(self):
        b = self.bots[0]; b.current_item_id = 100
        b.current_map = SimpleNamespace(beatmap_id=200)
        handled = []
        def handle(user, text, **context):
            handled.append((text, context['command_item_id'], context['command_beatmap_id']))
            b.current_item_id = 101; b.current_map = SimpleNamespace(beatmap_id=201)
        b.handle_command = handle
        b._process_chat_messages([
            {'message_id': 5002, 'sender_id': 7, 'content': '.skip'},
            {'message_id': 5001, 'sender_id': 8, 'content': '.skip'},
        ])
        self.assertEqual(handled, [('.skip', 100, 200), ('.skip', 100, 200)])


if __name__ == '__main__': unittest.main()
