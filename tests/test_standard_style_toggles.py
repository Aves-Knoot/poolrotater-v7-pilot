"""Independent Standard style/DT controls through real selection and commands."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import sqlite3
import shutil
import unittest
from unittest.mock import Mock, patch

from bot.bot import PLAYING
from bot.standard_pool import StandardPool
import test_standard_dt as dt_fixtures
from test_replay_and_freedomdive import make_bot


class StyleToggleTests(unittest.TestCase):
    pool = dt_fixtures.DTSelectionTests.pool

    def setUp(self):
        dt_fixtures.DTSelectionTests.setUp(self)
        with sqlite3.connect(self.db) as db:
            db.execute('DELETE FROM farm_report WHERE beatmap_id<=20 OR beatmap_id>40')
            db.executemany('INSERT INTO stream_features VALUES (?,?,?,?,?)',
                           [(i, .9, 180, 50, .8) for i in range(1, 21)])
        with sqlite3.connect(self.dt) as db:
            db.execute('UPDATE dt_maps SET farm_score=0,stream_score=0')
            db.execute('UPDATE dt_maps SET farm_score=500 WHERE beatmap_id BETWEEN 21 AND 40')
            db.execute('UPDATE dt_maps SET stream_score=.9 WHERE beatmap_id<=20')
            db.execute('UPDATE metadata SET value=? WHERE key="source_mtime_ns"',
                       (str(self.db.stat().st_mtime_ns),))
        jumps = {i: (.8, .9) for i in range(41, 61)}
        self.enterContext(patch('bot.pool.JUMP_CATALOG', jumps))
        self.enterContext(patch('bot.standard_pool.JUMP_CATALOG', jumps))

    def test_styles_form_union_and_dt_is_independent_in_both_orders(self):
        for dt_first in (False, True):
            pool = self.pool(); pool.set_dt_override(dt_first)
            self.assertTrue(pool.set_style_toggle('stream', True))
            self.assertTrue(pool.set_style_toggle('jump', True))
            self.assertEqual(pool.active_styles(), ('stream', 'jump'))
            self.assertEqual(pool.dt_override, dt_first)
            self.assertEqual({m.beatmap_id for m in pool.maps}, set(range(1, 21)) | set(range(41, 61)))
            pool.set_dt_override(True)
            self.assertEqual(pool.active_styles(), ('stream', 'jump'))
            self.assertEqual({m.is_dt for m in pool.maps}, {False, True})
            picks = [pool.next_map() for _ in range(60)]
            self.assertTrue(any(m.is_stream for m in picks))
            self.assertTrue(any(m.is_jump for m in picks))
            self.assertTrue(all(m.beatmap_id <= 20 or m.beatmap_id > 40 for m in picks))
            self.assertTrue(pool.set_style_toggle('stream', False))
            self.assertEqual(pool.active_styles(), ('jump',))
            self.assertTrue(pool.dt_enabled())
            self.assertTrue(pool.set_style_toggle('jump', False))
            self.assertEqual(pool.active_styles(), ())
            self.assertEqual(pool.source_mode, 'variety')
            self.assertTrue(pool.dt_enabled())

    def test_full_dt_mixed_styles_use_verified_dt_and_nm_fallback(self):
        pool = self.pool(); pool.set_style_selection(('stream', 'jump'))
        pool.set_dt_override(True, full=True)
        for _ in range(10):
            self.assertTrue(pool.next_map().is_dt)
        pool.maps = [m for m in pool.maps if not m.is_dt]
        self.assertFalse(pool.next_map().is_dt)
        self.assertEqual(pool.active_styles(), ('stream', 'jump'))

    def test_overlap_is_unique_and_pool_limit_stays_bounded_per_variant(self):
        with patch('bot.pool.JUMP_CATALOG', {1: (.8, .9), 41: (.8, .9)}), \
             patch('bot.standard_pool.JUMP_CATALOG', {1: (.8, .9), 41: (.8, .9)}):
            pool = self.pool(limit=4)
            self.assertTrue(pool.set_style_selection(('stream', 'jump', 'farm')))
            keys = [(m.beatmap_id, m.is_dt) for m in pool.maps]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertLessEqual(sum(not m.is_dt for m in pool.maps), 4)
            self.assertLessEqual(sum(m.is_dt for m in pool.maps), 4)
            both = [m for m in pool.maps if m.beatmap_id == 1]
            self.assertTrue(both)
            self.assertTrue(all(m.is_jump and m.is_stream for m in both))

    def test_failed_new_style_or_reload_preserves_prior_pool_and_dt(self):
        pool = self.pool(); pool.set_style_toggle('stream', True)
        before = pool.maps
        with patch('bot.pool.JUMP_CATALOG', {}), patch('bot.standard_pool.JUMP_CATALOG', {}):
            self.assertEqual(pool.set_style_toggle('jump', True), 0)
        self.assertIs(pool.maps, before)
        self.assertEqual(pool.active_styles(), ('stream',))
        self.assertIsNone(pool.dt_override)
        with patch.object(pool, '_reload_single', side_effect=sqlite3.OperationalError('invented')):
            self.assertEqual(pool.set_style_toggle('jump', True), 0)
        self.assertIs(pool.maps, before)

    def test_reservations_exclusions_and_sparse_pool_cannot_revive_disabled_styles(self):
        pool = self.pool(); pool.set_style_selection(('stream', 'jump'))
        pool.reserve_playlist_maps(range(1, 21))
        pool.exclude_beatmap(41)
        for _ in range(15):
            chosen = pool.next_map()
            self.assertGreater(chosen.beatmap_id, 41)
        pool.reserve_playlist_maps(range(1, 61))
        self.assertIsNone(pool.next_map())

    def test_disabling_last_style_restores_last_explicit_variety_mix(self):
        pool = self.pool(); pool.set_variety_mode(70, 30)
        pool.set_style_toggle('stream', True); pool.set_style_toggle('stream', False)
        self.assertEqual(pool.source_mode, 'variety')
        self.assertAlmostEqual(pool.farm_share, .7)
        self.assertAlmostEqual(pool.stream_share, .3)

    def bot(self):
        bot = make_bot(); bot.pool = self.pool()
        bot.state = PLAYING
        bot._is_admin = Mock(return_value=False)
        bot._is_moderator = Mock(return_value=False)
        bot._allow_public_command_burst = Mock(return_value=True)
        bot.gameplay_player_ids = Mock(return_value={7})
        return bot

    def test_player_commands_aliases_and_variety_preserve_queue_and_settings(self):
        bot = self.bot(); config = dict(bot.cfg); queue = list(bot.queued)
        for cmd in ('.stream on', '.aim on', '.dt on'):
            bot.handle_command(7, cmd)
        self.assertEqual(bot.pool.active_styles(), ('stream', 'jump'))
        self.assertTrue(bot.pool.dt_enabled())
        bot.handle_command(7, '.aim')
        self.assertEqual(bot.pool.active_styles(), ('stream',))
        bot._allow_public_command_burst.assert_called_with(7, 'jump')
        bot.handle_command(7, '.jump on')
        bot.handle_command(7, '.dt off')
        self.assertEqual(bot.pool.active_styles(), ('stream', 'jump'))
        self.assertFalse(bot.pool.dt_enabled())
        bot.handle_command(7, '.variety')
        self.assertEqual(bot.pool.active_styles(), ())
        self.assertFalse(bot.pool.dt_enabled())
        self.assertEqual(bot.cfg, config)
        self.assertEqual(bot.queued, queue)
        bot.hub.add_playlist_item.assert_not_called()
        bot.hub.remove_playlist_item.assert_not_called()

    def test_outsider_malformed_disabled_and_throttled_commands_do_not_mutate(self):
        bot = self.bot(); before = bot.pool.maps
        for command in ('.stream on', '.aim on', '.farm on', '.variety'):
            bot.handle_command(8, command)
        self.assertIs(bot.pool.maps, before)
        for command in ('.stream maybe', '.aim on off', '.farm 100'):
            bot.handle_command(7, command)
            self.assertIn('Usage:', bot.say.call_args.args[0])
        self.assertIs(bot.pool.maps, before)
        bot.cfg['user_commands_enabled'] = False
        bot.handle_command(7, '.aim on')
        bot.cfg['user_commands_enabled'] = True
        bot._allow_public_command_burst.return_value = False
        bot.handle_command(7, '.aim on')
        self.assertIs(bot.pool.maps, before)

    def test_help_alias_and_custom_prefix_explain_combining_styles(self):
        bot = self.bot(); bot.prefix = '!'
        bot.handle_command(7, '!help aim')
        self.assertIn('!jump [on|off]', bot.say.call_args.args[0])
        self.assertIn('!aim', bot.say.call_args.args[0])
        self.assertIn('DT stays separate', bot.say.call_args.args[0])
        bot.handle_command(7, '!stream on'); bot.handle_command(7, '!aim on')
        self.assertEqual(bot.pool.active_styles(), ('stream', 'jump'))
        bot.handle_command(7, '!help t')
        self.assertIn('!t', bot.say.call_args.args[0])

    def test_compatible_catalog_rebuild_preserves_toggles_dt_and_variety_preference(self):
        bot = self.bot()
        bot.cfg.update(farm_db=str(self.db), standard_dt_db=str(self.dt),
                       ratings_db=str(self.db.parent / 'ratings.db'),
                       stream_enabled=True, stream_share=.5, sr_min=5.5, sr_max=6.1,
                       max_length=240, min_passcount=10000, pool_limit=400)
        bot.pool.set_variety_mode(70, 30)
        bot.pool.set_style_selection(('stream', 'jump'))
        bot.pool.set_dt_override(True, full=True)
        alternate = self.db.parent / 'next-catalog.db'
        shutil.copy2(self.db, alternate)
        queue = list(bot.queued)
        self.assertTrue(bot._hot_rebuild_pool_source({'farm_db': str(alternate)}))
        self.assertEqual(bot.pool.active_styles(), ('stream', 'jump'))
        self.assertTrue(bot.pool.dt_full)
        self.assertTrue(bot.pool.dt_catalog_ready())
        self.assertEqual(bot.pool._last_variety_mix, (.7, .3))
        self.assertEqual(bot.queued, queue)


if __name__ == '__main__':
    unittest.main()
