"""Optional provider reads cannot hold the room thread or replace its roster."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import threading
import unittest
from unittest.mock import Mock, patch
from bot.api import ApiProfileReadUnavailable
from bot.hub import HubInvocationUncertain
from tests import test_player_profile_admission as fixture


class BackgroundProfileTests(unittest.TestCase):
    def bot(self):
        b = fixture.DeferredProfileTests.bot(self)
        b.cfg['background_player_profiles'] = True
        b._send_custom_join_greeting = Mock(return_value=False)
        return b

    def launch(self, b):
        with patch('bot.bot.time.monotonic', return_value=100): b.on_user_joined(7)
        b.api.user.assert_not_called()
        with patch('bot.bot.time.monotonic', return_value=102): b._flush_player_profiles()
        return b._background_profile_job['thread']

    def finish(self, b, thread, now=103):
        thread.join(2)
        self.assertFalse(thread.is_alive())
        # Profile and terminal score results are distinct room-thread ticks.
        with patch('bot.bot.time.monotonic', return_value=now):
            for _ in range(2): b._flush_player_profiles()

    def test_slow_profile_and_top_scores_do_not_block_or_mutate_on_background_thread(self):
        b = self.bot(); entered = threading.Event(); release = threading.Event()
        self.addCleanup(release.set)
        def slow(uid, **kwargs):
            entered.set()
            if not release.wait(2): raise AssertionError('test never released profile read')
            return fixture.PROFILE
        b.api.user.side_effect = slow
        thread = self.launch(b)
        self.assertTrue(entered.wait(1))
        b.players[7]['status'] = 'ready'
        with patch('bot.bot.time.monotonic', return_value=102):
            for _ in range(20): b._flush_player_profiles()
        self.assertIsNone(b.players[7]['pp'])
        self.assertEqual(b.api.user.call_count, 1)
        release.set(); thread.join(2)
        # Even a completed background read cannot mutate shared room state.
        self.assertIsNone(b.players[7]['pp'])
        self.finish(b, thread)
        self.assertEqual(b.players[7]['pp'], 1234)
        self.assertEqual(b.players[7]['status'], 'ready')
        self.assertEqual(b.players[7]['top'], [(100, 22)])
        b._record_lobby_join.assert_called_once()
        self.assertTrue(b._send_custom_join_greeting.called)
        self.assertTrue(all(call.args == (7,) for call in b._send_custom_join_greeting.call_args_list))

    def test_late_results_discarded_across_leave_rejoin_room_channel_and_expiry(self):
        for change in ('leave', 'rejoin', 'room', 'channel', 'expiry'):
            with self.subTest(change=change):
                b = self.bot(); thread = self.launch(b); thread.join(2)
                if change == 'leave': b.players.pop(7)
                if change == 'rejoin': b.players[7] = dict(b.players[7])
                if change == 'room': b.room_id = 99
                if change == 'channel': b.channel_id = 99
                self.finish(b, thread, now=500 if change == 'expiry' else 103)
                self.assertFalse(b._pending_player_profiles)
                b._send_custom_join_greeting.assert_not_called()
                if 7 in b.players: self.assertIsNone(b.players[7]['pp'])

    def test_transient_top_failure_retains_profile_without_repeating_user_lookup(self):
        b = self.bot(); b.api.user_top_scores.side_effect = ApiProfileReadUnavailable('temporary')
        thread = self.launch(b); self.finish(b, thread)
        self.assertEqual(b.players[7]['pp'], 1234)
        self.assertIn(7, b._pending_player_profiles)
        b.api.user_top_scores.side_effect = None
        with patch('bot.bot.time.monotonic', return_value=106): b._flush_player_profiles()
        self.finish(b, b._background_profile_job['thread'], now=107)
        self.assertEqual(b.api.user.call_count, 1)
        self.assertFalse(b._pending_player_profiles)

    def test_generic_rpc_uncertainty_still_propagates_on_room_thread(self):
        b = self.bot(); b.api.user.side_effect = HubInvocationUncertain('authority outcome unknown')
        thread = self.launch(b)
        with self.assertRaises(HubInvocationUncertain): self.finish(b, thread)

    def test_only_one_read_job_and_bounded_pending_roster(self):
        b = self.bot(); release = threading.Event(); entered = threading.Event()
        self.addCleanup(release.set)
        def slow(uid, **kwargs):
            entered.set(); release.wait(2)
            return {'id': uid, 'username': 'Invented', 'statistics': {'pp': 1}}
        b.api.user.side_effect = slow
        with patch('bot.bot.time.monotonic', return_value=100):
            for uid in range(1, 40): b.on_user_joined(uid)
        self.assertEqual(len(b._pending_player_profiles), 32)
        b.api.user.assert_not_called()
        with patch('bot.bot.time.monotonic', return_value=102): b._flush_player_profiles()
        self.assertTrue(entered.wait(1))
        thread = b._background_profile_job['thread']
        with patch('bot.bot.time.monotonic', return_value=102):
            for _ in range(20): b._flush_player_profiles()
        self.assertEqual(b.api.user.call_count, 1)
        release.set(); self.finish(b, thread)


if __name__ == '__main__': unittest.main()
