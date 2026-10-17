"""Offline remaining-time command and shared join estimate checks."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.bot import IDLE, PLAYING, _PUBLIC_COMMANDS
from test_replay_and_freedomdive import make_bot


class TimeCommandTests(unittest.TestCase):
    def bot(self):
        bot = make_bot()
        bot.state = PLAYING
        bot.playing_map = SimpleNamespace(hit_length=120)
        bot.current_map.hit_length = 300
        bot._match_started_at = 1000
        bot._state_since = 900
        bot._is_admin = Mock(return_value=False)
        bot._is_moderator = Mock(return_value=False)
        bot._allow_public_command_burst = Mock(return_value=True)
        bot._mid_match_intro_sent = set()
        bot._join_help_tip = Mock(return_value="")
        return bot

    def test_remaining_uses_running_map_and_is_read_only(self):
        bot = self.bot()
        with patch("bot.bot.time.time", return_value=1030):
            bot.handle_command(1, ".time")
        bot.say.assert_called_once_with("About 1m 30s left—you're in next.")
        bot._allow_public_command_burst.assert_called_once_with(1, "time")
        bot.hub.add_playlist_item.assert_not_called()
        bot.hub.remove_playlist_item.assert_not_called()
        bot.set_state.assert_not_called()
        self.assertIn("time", _PUBLIC_COMMANDS)

    def test_join_and_command_share_estimate_and_hint_once(self):
        bot = self.bot()
        with patch("bot.bot.time.time", return_value=1080):
            bot.handle_command(1, ".time")
            self.assertTrue(bot._send_mid_match_join_intro(1))
            self.assertFalse(bot._send_mid_match_join_intro(1))
        self.assertEqual(bot.say.call_count, 2)
        self.assertEqual(bot.say.call_args_list[0].args[0], "About 40s left—you're in next.")
        bot.say.assert_called_with(
            "player-1, about 40s left—you're in next. .time for remaining.", automatic=True)

    def test_no_map_running_is_short(self):
        for state in (IDLE, "STARTING", "RESULTS"):
            with self.subTest(state=state):
                bot = self.bot(); bot.state = state
                bot.handle_command(1, ".time")
                bot.say.assert_called_once_with("No map running.")

    def test_expired_estimate_never_reports_negative_time(self):
        bot = self.bot()
        with patch("bot.bot.time.time", return_value=1300):
            bot.handle_command(1, ".time")
        bot.say.assert_called_once_with("Finishing—you're in next.")

    def test_missing_or_invalid_duration_is_not_a_fake_timer(self):
        for length in (None, 0, -5, "invalid", float("nan"), float("inf")):
            with self.subTest(length=length):
                bot = self.bot(); bot.playing_map.hit_length = length
                bot.handle_command(1, ".time")
                bot.say.assert_called_once_with("Time remaining unavailable—you're in next.")

    def test_missing_running_map_and_start_time_use_existing_fallbacks(self):
        bot = self.bot(); bot.playing_map = None; bot._match_started_at = None
        with patch("bot.bot.time.time", return_value=1030):
            bot.handle_command(1, ".time")
        bot.say.assert_called_once_with("About 2m 50s left—you're in next.")

    def test_future_start_is_clamped_and_help_is_available(self):
        bot = self.bot()
        with patch("bot.bot.time.time", return_value=990):
            bot.handle_command(1, ".time")
        bot.say.assert_called_once_with("About 2m 00s left—you're in next.")
        bot.say.reset_mock()
        bot.handle_command(1, ".help time")
        self.assertIn(".time", bot.say.call_args.args[0])
        self.assertIn("running map", bot.say.call_args.args[0])

    def test_command_respects_disabled_public_commands_and_burst_guard(self):
        bot = self.bot(); bot.cfg["user_commands_enabled"] = False
        bot.handle_command(1, ".time")
        bot.say.assert_not_called()
        bot.cfg["user_commands_enabled"] = True
        bot._allow_public_command_burst.return_value = False
        bot.handle_command(1, ".time")
        bot.say.assert_not_called()

    def test_command_in_mania_modes_and_custom_prefix(self):
        for keys in (4, 7):
            with self.subTest(keys=keys):
                bot = self.bot(); bot.cfg.update(ruleset_id=3, mania_keys=keys)
                bot.prefix = "!"
                with patch("bot.bot.time.time", return_value=1030):
                    bot.handle_command(1, "!time")
                    bot._send_mid_match_join_intro(1)
                self.assertEqual(bot.say.call_args_list[0].args[0], "About 1m 30s left—you're in next.")
                self.assertIn("!time for remaining.", bot.say.call_args.args[0])

    def test_short_alias_has_same_timing_guard_and_permissions_in_all_modes(self):
        for ruleset, keys in ((0, None), (3, 4), (3, 7)):
            for prefix in ('.', '!'):
                with self.subTest(ruleset=ruleset, keys=keys, prefix=prefix):
                    bot = self.bot()
                    bot.cfg.update(ruleset_id=ruleset, mania_keys=keys)
                    bot.prefix = prefix
                    with patch('bot.bot.time.time', return_value=1030):
                        bot.handle_command(1, prefix + 't')
                    bot.say.assert_called_once_with("About 1m 30s left—you're in next.")
                    bot._allow_public_command_burst.assert_called_once_with(1, 'time')
                    bot.say.reset_mock()
                    bot.cfg['user_commands_enabled'] = False
                    bot.handle_command(1, prefix + 't')
                    bot.say.assert_not_called()
                    bot.cfg['user_commands_enabled'] = True
                    bot._allow_public_command_burst.return_value = False
                    bot.handle_command(1, prefix + 't')
                    bot.say.assert_not_called()

    def test_short_alias_help_uses_the_canonical_command(self):
        bot = self.bot()
        bot.handle_command(1, '.help t')
        self.assertIn('.time', bot.say.call_args.args[0])
        self.assertIn('.t', bot.say.call_args.args[0])
