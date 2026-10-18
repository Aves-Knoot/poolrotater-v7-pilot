"""Undispatched room chat waits without changing gameplay or replaying uncertainty."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from contextlib import ExitStack
from unittest.mock import Mock, patch
import unittest
from bot.api import ApiAdmissionDeferred, ApiKeepaliveUnavailable
from bot.bot import LobbyBot as Bot
from rolling.protocol import RemoteError


class RoomChatBackpressureTests(unittest.TestCase):
    def bot(self):
        b = Bot.__new__(Bot)
        b.cfg = {'_headless_supervised': True, 'bot_chat_silenced': False}
        b.room_id = 20; b.channel_id = 11; b._last_sent = 0
        b._own_messages = set(); b.api = Mock(); b._discord_command_reply_channel_id = None
        b._localize_command_reply = Mock(side_effect=lambda text, **kw: text)
        b._notify_discord_lobby_event = Mock()
        return b

    def clock(self, value):
        stack = ExitStack()
        stack.enter_context(patch('bot.bot.time.monotonic', return_value=value))
        stack.enter_context(patch('bot.bot.time.time', return_value=value))
        stack.enter_context(patch('bot.bot.time.sleep', side_effect=AssertionError('loop must not sleep for queued chat')))
        return stack

    def test_no_dispatch_is_queued_then_sent_once_with_original_localization(self):
        b = self.bot(); b.api.chat_send.side_effect = [ApiAdmissionDeferred('before-dispatch'), {'message_id': 99}]
        with self.clock(100): b.say('One reply')
        self.assertEqual(len(b._pending_room_chat), 1)
        self.assertEqual(b._last_sent, 0); b._notify_discord_lobby_event.assert_not_called()
        with self.clock(102): b._flush_room_chat()
        self.assertEqual(len(b._pending_room_chat), 0)
        self.assertEqual(b._own_messages, {99})
        self.assertEqual(b._localize_command_reply.call_count, 1)
        self.assertEqual(b.api.chat_send.call_count, 2)
        b._notify_discord_lobby_event.assert_called_once_with('BOT', 'One reply', automatic=False)

    def test_order_is_preserved_and_flush_respects_send_gap_without_sleep(self):
        b = self.bot(); b.api.chat_send.side_effect = [ApiAdmissionDeferred('busy'), {'message_id': 1}, {'message_id': 2}]
        with self.clock(100): b.say('First'); b.say('Second')
        self.assertEqual(b.api.chat_send.call_count, 1)
        with self.clock(102): b._flush_room_chat()
        with self.clock(103): b._flush_room_chat()
        self.assertEqual(b.api.chat_send.call_count, 2)
        with self.clock(104): b._flush_room_chat()
        self.assertEqual([call.args[1] for call in b.api.chat_send.call_args_list], ['First', 'First', 'Second'])

    def test_expired_changed_room_and_silenced_automatic_messages_are_not_delivered(self):
        for changed in ('expired', 'room', 'channel', 'silenced'):
            b = self.bot(); b.api.chat_send.side_effect = ApiAdmissionDeferred('busy')
            with self.clock(100): b.say('Automatic intro', automatic=True)
            if changed == 'room': b.room_id = 21
            if changed == 'channel': b.channel_id = 12
            if changed == 'silenced': b.cfg['bot_chat_silenced'] = True
            with self.clock(221 if changed == 'expired' else 102): b._flush_room_chat()
            self.assertEqual(b.api.chat_send.call_count, 1, changed)
            self.assertFalse(b._pending_room_chat, changed)

    def test_queue_and_retries_are_bounded_while_gameplay_state_is_unchanged(self):
        b = self.bot(); b.state = 'PLAYING'; b.api.chat_send.side_effect = ApiAdmissionDeferred('busy')
        with self.clock(100):
            for i in range(60): b.say('Reply ' + str(i))
        self.assertEqual(len(b._pending_room_chat), 32)
        self.assertEqual(b.api.chat_send.call_count, 1)
        delays = []
        for now in range(102, 203, 20):
            with self.clock(now): b._flush_room_chat()
            delays.append(b._room_chat_retry_at - now)
        self.assertEqual(delays, [1, 1, 1, 1, 1, 1])
        self.assertEqual(b.state, 'PLAYING')

    def test_dispatched_uncertain_message_and_auth_failure_are_never_queued_or_retried(self):
        for kind in ('HubInvocationUncertain', 'AuthError'):
            b = self.bot(); b.api.chat_send.side_effect = RemoteError(kind, 'uncertain or unauthorized')
            with self.clock(100), self.assertRaises(RemoteError): b.say('Do not replay')
            self.assertFalse(getattr(b, '_pending_room_chat', None))
            self.assertEqual(b.api.chat_send.call_count, 1)

    def test_discord_only_replies_stay_on_original_channel(self):
        b = self.bot(); b._discord_command_reply_channel_id = 42
        b.discord_control = Mock(); b.discord_control.send_to_channel.return_value = True
        with self.clock(100): b.say('Staff response')
        b.api.chat_send.assert_not_called()
        b.discord_control.send_to_channel.assert_called_once_with('Staff response', channel_id=42)

    def test_known_keepalive_loss_defers_without_marking_ack_success(self):
        b = self.bot(); b.api.chat_ack.side_effect = ApiKeepaliveUnavailable('unknown keepalive response')
        with self.clock(100): completed, value = b._chat_api_call('chat_ack')
        self.assertFalse(completed); self.assertIsNone(value)
        self.assertEqual(b._chat_api_retry_at, 101)


if __name__ == '__main__': unittest.main()
