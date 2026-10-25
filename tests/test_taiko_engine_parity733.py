"""Taiko engine parity checks for shared UI and user-scoped state."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import tempfile
import unittest
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

from bot.bot import LobbyBot
from bot.player_preferences import preference
from bot.taiko_preferences import preference_scope
from bot.rating_outbox import storage_key, TAIKO_KEY_BASE
from bot.taiko_pool import TaikoPool
from bot.taiko_runtime import handle_command
from poolrotater_host.catalog_taiko import MAP_COLUMNS
from tests.test_taiko_catalog733 import map_tuple


class TaikoEngineParity733Tests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.catalog = Path(folder.name) / 'catalog.db'
        with sqlite3.connect(self.catalog) as db:
            db.execute('CREATE TABLE taiko_maps (' + ','.join(n + ' ' + t for n, t in MAP_COLUMNS) + ')')
            db.executemany('INSERT INTO taiko_maps VALUES (' + ','.join('?' for _ in MAP_COLUMNS) + ')',
                           [map_tuple(1, mode=1), map_tuple(2, mode=0)])
        self.pool = TaikoPool(self.catalog, purpose='production', sr_min=3, sr_max=5,
                              max_length=240, ratings_db=None)
        self.messages = []
        self.bot = type('BotStub', (), {})()
        self.bot.cfg = {'ruleset_id': 1}
        self.bot.pool = self.pool
        self.bot.prefix = '.'
        self.bot.say = lambda *args, **kwargs: self.messages.append(args[0])

    def test_same_player_preference_windows_are_ruleset_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / 'ratings.db')
            taiko_scope = preference_scope({'ruleset_id': 1}, self.pool)
            standard_scope = preference_scope({'ruleset_id': 0}, self.pool)
            preference(db, taiko_scope, 42, (3.0, 4.0))
            preference(db, standard_scope, 42, (5.0, 6.0))

            self.assertEqual(preference(db, taiko_scope, 42), (3.0, 4.0))
            self.assertEqual(preference(db, standard_scope, 42), (5.0, 6.0))
            self.assertNotEqual(taiko_scope, standard_scope)

    def test_lobby_taiko_preference_save_and_restore_use_taiko_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / 'ratings.db')
            preference(db, 'standard:ranked', 42, (5.0, 6.0))
            bot = LobbyBot.__new__(LobbyBot)
            bot.cfg = {'ruleset_id': 1}
            bot.ratings = SimpleNamespace(path=db)
            bot.players = {42: {'status': 'idle'}}
            bot.referee_ids = set()
            bot.pool = SimpleNamespace(sr_min=3.5, sr_max=4.5)
            bot._preference_played = (42, (3.5, 4.5))
            bot._preference_choice = 42
            bot._release_recalled_difficulty = Mock()
            bot._save_departing_preference(42)

            self.assertEqual(preference(db, 'taiko:ranked', 42), (3.5, 4.5))
            self.assertEqual(preference(db, 'standard:ranked', 42), (5.0, 6.0))

            def set_range(low, high):
                bot.pool.sr_min, bot.pool.sr_max = low, high
                return 1
            bot.pool.set_range = set_range
            bot.state = 'IDLE'
            bot.perform_soft_restart = Mock(return_value=True)
            bot._clear_community_difficulty = Mock()
            bot.update_room_name_for_range = Mock()
            self.assertEqual(bot._restore_solo_difficulty(42), (3.5, 4.5))
            self.assertEqual((bot.pool.sr_min, bot.pool.sr_max), (3.5, 4.5))
            bot.perform_soft_restart.assert_called_once_with(announce=False)

    def test_identical_provider_map_ids_have_distinct_vote_identity(self):
        standard_key = storage_key(123, {'ruleset_id': 0})
        taiko_key = storage_key(123, {'ruleset_id': 1}, map_origin='taiko-native-nm')
        converted_key = storage_key(123, {'ruleset_id': 1}, map_origin='taiko-convert-nm')
        self.assertEqual(standard_key, 123)
        self.assertEqual(taiko_key, TAIKO_KEY_BASE + 123)
        self.assertEqual(converted_key, taiko_key)

    def test_no_argument_map_pp_variety_and_queue_paths_are_safe(self):
        self.bot._pp_command_map = lambda: self.pool.get_map(1)
        self.bot.state = 'IDLE'
        self.bot.current_map = self.pool.get_map(1)
        self.bot.playing_map = None
        self.bot.starting_map = None
        self.bot.last_played_map = None
        self.bot.queued = []

        for cmd in ('map', 'pp', 'variety'):
            with self.subTest(command=cmd):
                self.messages.clear()
                self.assertTrue(handle_command(self.bot, cmd, [cmd]))
                self.assertEqual(len(self.messages), 1)

        # The inherited playlist selection path must continue to return a
        # qualified Taiko map without requiring Standard-only map metadata.
        picked = self.pool.next_map()
        self.assertIsNotNone(picked)
        self.assertEqual(picked.ruleset_id, 1)
        self.pool.verify_variant(picked, in_range=True)


if __name__ == '__main__':
    unittest.main()
