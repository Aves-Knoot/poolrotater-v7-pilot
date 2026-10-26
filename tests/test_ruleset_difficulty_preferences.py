"""Exercise the real save/restore path against one shared preference database."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from bot.bot import LobbyBot
from bot.player_preferences import preference
from bot.taiko_preferences import preference_scope


class RulesetDifficultyPreferencesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = str(Path(directory.name) / 'preferences.db')

    def bot(self, ruleset, keys=4):
        bot = LobbyBot.__new__(LobbyBot)
        bot.cfg = {'ruleset_id': ruleset, 'mania_keys': keys}
        bot.ratings = SimpleNamespace(path=self.db)
        bot.players = {42: {'status': 'idle'}}
        bot.referee_ids = set()
        bot.pool = SimpleNamespace(sr_min=3.0, sr_max=4.0)
        def set_range(low, high):
            bot.pool.sr_min, bot.pool.sr_max = low, high
            return 1
        bot.pool.set_range = set_range
        bot.state = 'IDLE'
        bot._release_recalled_difficulty = Mock()
        bot._clear_community_difficulty = Mock()
        bot.perform_soft_restart = Mock(return_value=True)
        bot.update_room_name_for_range = Mock()
        return bot

    def test_unset_catch_never_reads_existing_standard_preference(self):
        preference(self.db, 'standard:ranked', 42, (5.0, 6.0))
        bot = self.bot(2)
        self.assertIsNone(bot._restore_solo_difficulty(42))
        self.assertEqual((bot.pool.sr_min, bot.pool.sr_max), (3.0, 4.0))
        bot.perform_soft_restart.assert_not_called()

    def test_catch_save_and_restore_does_not_change_other_modes(self):
        windows = {'standard:ranked': (5.0, 6.0), 'taiko:ranked': (4.0, 5.0),
                   'mania:4': (2.0, 3.0), 'mania:7': (6.0, 7.0)}
        for scope, window in windows.items():
            preference(self.db, scope, 42, window)
        bot = self.bot(2)
        bot._preference_played = (42, (3.0, 4.0))
        bot._preference_choice = 42
        bot._save_departing_preference(42)
        self.assertEqual(preference(self.db, 'catch:ranked', 42), (3.0, 4.0))
        for scope, window in windows.items():
            self.assertEqual(preference(self.db, scope, 42), window)
        bot.pool.sr_min, bot.pool.sr_max = 1.0, 2.0
        self.assertEqual(bot._restore_solo_difficulty(42), (3.0, 4.0))
        bot.perform_soft_restart.assert_called_once_with(announce=False)

    def test_all_mode_scopes_and_catch_loved_are_distinct(self):
        pool = SimpleNamespace(loved_mode=False, catch_include_loved=False)
        scopes = [preference_scope({'ruleset_id': 0}, pool),
                  preference_scope({'ruleset_id': 1}, pool),
                  preference_scope({'ruleset_id': 2}, pool),
                  preference_scope({'ruleset_id': 3, 'mania_keys': 4}, pool),
                  preference_scope({'ruleset_id': 3, 'mania_keys': 7}, pool)]
        self.assertEqual(len(set(scopes)), 5)
        pool.catch_include_loved = True
        self.assertNotIn(preference_scope({'ruleset_id': 2}, pool), scopes)

    def test_real_restore_reads_each_mode_for_same_player(self):
        cases = [(0, 4, 'standard:ranked', (5.0, 6.0)),
                 (1, 4, 'taiko:ranked', (4.0, 5.0)),
                 (2, 4, 'catch:ranked', (3.0, 4.0)),
                 (3, 4, 'mania:4', (2.0, 3.0)),
                 (3, 7, 'mania:7', (6.0, 7.0))]
        for _, _, scope, window in cases:
            preference(self.db, scope, 42, window)
        for ruleset, keys, _, window in cases:
            with self.subTest(ruleset=ruleset, keys=keys):
                bot = self.bot(ruleset, keys)
                self.assertEqual(bot._restore_solo_difficulty(42), window)


if __name__ == '__main__':
    unittest.main()
