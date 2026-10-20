from scripts.test_isolation import require_test_isolation
require_test_isolation("ordinary")
"""Offline mixed-DT selection, playlist identity and safe opt-out regressions."""
import math
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.bot import LobbyBot, IDLE, PLAYING, STARTING, SCORING
from bot.pool import PoolMap
from bot.standard_pool import StandardPool, DT_CALCULATOR
from rolling.worker import make_worker_pool
from test_poolrotater_updates import build_test_db
from test_replay_and_freedomdive import make_bot

POLICY = dict(real_max_length=240, preferred_min_length=90,
              preferred_max_length=150, preferred_length_share=.8)


def variant(bid=1, sr=5.8):
    return PoolMap(bid, 'Artist', f'Title{bid}', 'Hard', sr, 120, 100, 20000,
                   is_farm=True, is_dt=True, clock_rate=1.5, source_sr=4,
                   source_hit_length=180, dt_checksum='a'*32, dt_calculator=DT_CALCULATOR)


class DTSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name)/'farm.db'
        self.dt = Path(self.tmp.name)/'dt.db'
        build_test_db(self.db)
        with sqlite3.connect(self.db) as c:
            for table in ('beatmaps', 'farm_report', 'stream_features'):
                c.execute(f'DELETE FROM {table}')
            for bid in range(1, 61):
                row = (bid, f'Artist{bid}', f'Title{bid}', 'Hard', 5.8, 180, 20000, 'ranked', '2099-01-01')
                c.execute('INSERT INTO beatmaps VALUES (?,?,?,?,?,?,?,?,?)', row)
                c.execute('INSERT INTO farm_report VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                          (*row[:7], 1000-bid, 'balanced', .5, 300, 'NM'))
        with sqlite3.connect(self.dt) as c:
            c.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
              CREATE TABLE dt_maps(beatmap_id INTEGER PRIMARY KEY,artist TEXT,title TEXT,version TEXT,
                nm_sr REAL,dt_sr REAL,hit_length INTEGER,source_hit_length INTEGER,passcount INTEGER,
                ranked_date TEXT,farm_score REAL,dominant_mods TEXT,stream_score REAL,stream_bpm REAL,
                checksum TEXT,calculator TEXT);''')
            c.executemany('INSERT INTO metadata VALUES (?,?)', dict(format='1',mods='DT',clock_rate='1.5',
                calculator=DT_CALCULATOR,built_at=str(time.time()),source_size=str(self.db.stat().st_size),
                source_mtime_ns=str(self.db.stat().st_mtime_ns)).items())
            for bid in range(1, 61):
                c.execute('INSERT INTO dt_maps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (bid,f'Artist{bid}',f'Title{bid}','Hard',4,5.8,120,180,20000,'2099-01-01',
                     1000-bid,'DT',.8,180,'a'*32,DT_CALCULATOR))

    def pool(self, **kwargs):
        options = dict(sr_min=5.5,sr_max=6.1,max_length=240,min_passcount=10000,limit=400,
                       standard_dt_db=str(self.dt), sparse_passcount_enabled=True)
        options.update(kwargs)
        return StandardPool(str(self.db), **options)

    def edit_dt(self, sql, params=()):
        with sqlite3.connect(self.dt) as c:
            c.execute(sql, params)

    def test_auto_threshold_is_active_window_midpoint_strictly_above(self):
        for lo, hi, enabled in ((4.9,5.7,False),(5.2,5.8,False),(5.3,5.9,True)):
            pool = self.pool(sr_min=lo,sr_max=hi)
            self.assertEqual(pool.dt_enabled(), enabled)
            self.assertEqual(any(m.is_dt for m in pool.maps), enabled)

    def test_manual_on_at_low_stars_and_sticky_off_at_high_stars(self):
        self.edit_dt('UPDATE dt_maps SET dt_sr=3.5')
        pool = self.pool(sr_min=3,sr_max=4)
        self.assertFalse(pool.dt_enabled())
        pool.set_dt_override(True)
        self.assertTrue(pool.dt_enabled())
        self.assertTrue(all(m.sr == 3.5 for m in pool.maps))
        pool.set_dt_override(False)
        pool.set_window(5.5,6.1,240)
        self.assertFalse(pool.dt_enabled())
        self.assertFalse(any(m.is_dt for m in pool.maps))

    def test_empty_room_reset_restores_auto_policy(self):
        pool = self.pool()
        pool.set_dt_override(True)
        self.assertGreater(pool.reset_session_pool(5.2,5.8,240), 0)
        self.assertIsNone(pool.dt_override)
        self.assertFalse(pool.dt_enabled())

    def test_both_variants_have_actual_stars_lengths_labels(self):
        pool = self.pool()
        dt = pool.get_variant_map(1, [{'acronym':'DT'}])
        nm = pool.get_map(1)
        self.assertEqual((dt.sr,dt.hit_length,dt.clock_rate), (5.8,120,1.5))
        self.assertEqual((nm.sr,nm.hit_length,nm.is_dt), (5.8,180,False))
        self.assertIn('+DT', dt.label)
        self.assertNotIn('+DT', nm.label)

    def test_mix_and_no_consecutive_dt_when_nm_fresh(self):
        pool = self.pool()
        with patch('bot.standard_pool.random.random', return_value=0):
            self.assertTrue(pool._weighted_pick(pool.maps).is_dt)
            self.assertFalse(pool._weighted_pick(pool.maps).is_dt)
        with patch('bot.standard_pool.random.random', return_value=.99):
            self.assertFalse(pool._weighted_pick(pool.maps).is_dt)

    def test_full_dt_consecutive_picks_and_nm_fallback(self):
        pool = self.pool()
        pool.set_dt_override(True, full=True)
        for _ in range(10):
            self.assertTrue(pool.next_map().is_dt)
        pool.maps = [m for m in pool.maps if not m.is_dt]
        self.assertFalse(pool.next_map().is_dt)

    def test_full_dt_fresh_nm_beats_recent_or_reserved_dt(self):
        pool = self.pool()
        pool.set_dt_override(True, full=True)
        dt, nm = variant(1), pool.get_map(2)
        pool.maps = [dt, nm]
        for blocked in ('recent', 'round', 'reserved', 'excluded'):
            with self.subTest(blocked=blocked):
                pool._recent.clear(); pool._round_history.clear()
                pool._playlist_reserved.clear(); pool._runtime_excluded.clear()
                pool._recent_songs.clear()
                if blocked == 'recent': pool._recent.append(1)
                if blocked == 'round': pool.record_round(1)
                if blocked == 'reserved': pool.reserve_playlist_maps([1])
                if blocked == 'excluded': pool._runtime_excluded.add(1)
                self.assertFalse(pool.next_map().is_dt)

    def test_full_dt_restores_mix_optout_and_auto(self):
        pool = self.pool()
        for override in (True, False, None):
            pool.set_dt_override(True, full=True)
            pool.set_dt_override(override)
            self.assertFalse(pool.dt_full)
            self.assertEqual(pool.dt_override, override)
        pool.set_dt_override(True, full=True)
        pool.reset_session_pool(5.2, 5.8, 240)
        self.assertFalse(pool.dt_full)
        self.assertIsNone(pool.dt_override)

    def test_full_dt_catalog_unavailable_and_native_seed_remain_nm(self):
        pool = self.pool(); pool.set_dt_override(True, full=True)
        self.assertFalse(pool.next_map(native_only=True).is_dt)
        pool._dt_valid_until = 0
        self.assertFalse(pool.next_map().is_dt)

    def test_full_dt_failed_reload_preserves_policy(self):
        pool = self.pool(); pool.set_dt_override(True, full=True)
        with patch.object(pool, 'reload', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError): pool.set_dt_override(True)
        self.assertTrue(pool.dt_full)
        self.assertTrue(pool.dt_override)

    def test_nm_and_dt_share_exact_repeat_history(self):
        pool = self.pool()
        with patch('bot.standard_pool.random.random', return_value=0):
            chosen = pool.next_map()
            other = pool.next_map()
        self.assertNotEqual(chosen.beatmap_id, other.beatmap_id)
        self.assertIn(chosen.beatmap_id, pool._recent)

    def test_disabled_stale_list_never_selects_dt(self):
        pool = self.pool()
        pool.dt_override=False
        for _ in range(10):
            self.assertFalse(pool.next_map().is_dt)

    def test_initial_room_seed_never_uses_dt_and_keeps_future_catalog(self):
        pool = self.pool()
        with patch('bot.standard_pool.random.random',return_value=0):
            self.assertFalse(pool.next_map(native_only=True).is_dt)
        self.assertTrue(any(m.is_dt for m in pool.maps))

    def test_catalog_expiry_removes_cached_dt_without_requiring_reload(self):
        pool = self.pool()
        self.assertTrue(pool.dt_catalog_ready())
        pool._dt_valid_until=0
        with patch('bot.standard_pool.random.random',return_value=0):
            self.assertFalse(pool.next_map().is_dt)
        self.assertFalse(any(m.is_dt for m in pool.maps))

    def test_source_identity_change_removes_cached_dt_without_reload(self):
        pool = self.pool()
        pool._dt_source_signature=(0,0)
        self.assertFalse(pool.dt_catalog_ready())
        self.assertFalse(pool.next_map().is_dt)

    def test_dt_quarantine_does_not_remove_nm(self):
        pool = self.pool()
        pool.exclude_dt_beatmap(1)
        self.assertFalse(any(m.is_dt and m.beatmap_id == 1 for m in pool.maps))
        self.assertTrue(any(not m.is_dt and m.beatmap_id == 1 for m in pool.maps))
        pool.reload()
        self.assertFalse(any(m.is_dt and m.beatmap_id == 1 for m in pool.maps))

    def test_actual_dt_bounds_duration_age_passes_are_hard(self):
        self.edit_dt('UPDATE dt_maps SET dt_sr=4 WHERE beatmap_id=1')
        self.edit_dt('UPDATE dt_maps SET hit_length=241 WHERE beatmap_id=2')
        self.edit_dt('UPDATE dt_maps SET ranked_date="2000-01-01" WHERE beatmap_id=3')
        self.edit_dt('UPDATE dt_maps SET passcount=999 WHERE beatmap_id=4')
        pool = self.pool(max_map_age_years=8)
        self.assertTrue({1,2,3,4}.isdisjoint(m.beatmap_id for m in pool.maps if m.is_dt))

    def test_dt_preferred_duration_relaxes_lower_bound_only(self):
        pool = self.pool()
        pool.configure_duration_policy(POLICY)
        for length, preferred in ((59,False),(60,True),(89,True),(90,True),
                                  (100,True),(120,True),(150,True),(151,False),(180,False)):
            with self.subTest(length=length):
                item = variant(); item.hit_length = length
                self.assertEqual(pool._is_preferred_duration(item), preferred)

    def test_nm_preferred_duration_is_unchanged(self):
        pool = self.pool()
        pool.configure_duration_policy(POLICY)
        for length, preferred in ((60,False),(89,False),(90,True),(150,True),(151,False)):
            with self.subTest(length=length):
                item = variant(); item.is_dt=False; item.hit_length=length
                self.assertEqual(pool._is_preferred_duration(item), preferred)

    def test_dt_preference_follows_configured_bounds_and_rounding(self):
        pool = self.pool()
        for minimum, maximum in ((91,151),(120,180),(0,150),(240,240)):
            pool.configure_duration_policy(POLICY | dict(preferred_min_length=minimum,
                                                         preferred_max_length=maximum))
            lo = math.ceil(minimum/1.5)
            for length in (lo-1,lo,maximum,maximum+1):
                with self.subTest(minimum=minimum,maximum=maximum,length=length):
                    item=variant(); item.hit_length=length
                    self.assertEqual(pool._is_preferred_duration(item), lo<=length<=maximum)

    def test_dt_shorter_and_previously_preferred_maps_share_preferred_bucket(self):
        pool=self.pool(); pool.configure_duration_policy(POLICY)
        maps=[]
        for bid,length in enumerate((59,60,100,150,151,180,240,241),1):
            item=variant(bid); item.hit_length=length; maps.append(item)
        with patch('bot.pool.random.random',return_value=.79):
            self.assertEqual([m.hit_length for m in pool._duration_candidates(maps)], [60,100,150])
        with patch('bot.pool.random.random',return_value=.8):
            self.assertEqual([m.hit_length for m in pool._duration_candidates(maps)], [59,151,180,240])

    def test_dt_source_over_nm_cap_is_admitted_using_actual_duration(self):
        self.edit_dt('UPDATE dt_maps SET source_hit_length=270,hit_length=180 WHERE beatmap_id=1')
        self.edit_dt('UPDATE dt_maps SET source_hit_length=360,hit_length=240 WHERE beatmap_id=2')
        self.edit_dt('UPDATE dt_maps SET source_hit_length=361,hit_length=241 WHERE beatmap_id=3')
        pool=self.pool(); pool.configure_duration_policy(POLICY)
        maps={m.beatmap_id:m for m in pool.maps if m.is_dt}
        self.assertEqual((maps[1].source_hit_length,maps[1].hit_length), (270,180))
        self.assertEqual((maps[2].source_hit_length,maps[2].hit_length), (360,240))
        self.assertNotIn(3,maps)
        self.assertFalse(pool._is_preferred_duration(maps[1]))

    def test_dt_long_map_can_be_selected_in_occasional_bucket(self):
        self.edit_dt('UPDATE dt_maps SET source_hit_length=270,hit_length=180 WHERE beatmap_id=1')
        pool=self.pool(); pool.configure_duration_policy(POLICY)
        maps=[m for m in pool.maps if m.is_dt and m.beatmap_id in (1,2)]
        with patch('bot.pool.random.random',return_value=.99):
            self.assertEqual(pool._weighted_pick(maps).beatmap_id,1)

    def test_dt_sub_minimum_maps_remain_selectable(self):
        pool=self.pool(); pool.configure_duration_policy(POLICY)
        short=variant(1); short.hit_length=45; short.source_hit_length=67
        preferred=variant(2)
        with patch('bot.pool.random.random',return_value=.99):
            self.assertEqual(pool._weighted_pick([short,preferred]).beatmap_id,1)

    def test_dt_only_short_bucket_does_not_block_selection(self):
        pool=self.pool(); pool.configure_duration_policy(POLICY)
        short=variant(); short.hit_length=45
        with patch('bot.pool.random.random') as draw:
            self.assertEqual(pool._duration_candidates([short]),[short])
            draw.assert_not_called()

    def test_dt_soft_preference_is_not_implicitly_enabled(self):
        pool=self.pool()
        self.assertIsNone(pool.real_max_length)
        short=variant(1); short.hit_length=60
        maps=[short,variant(2)]
        with patch('bot.pool.random.random') as draw:
            self.assertIs(pool._duration_candidates(maps),maps)
            draw.assert_not_called()

    def test_dt_duration_preference_preserves_feedback_weights(self):
        pool=self.pool(); pool.configure_duration_policy(POLICY)
        short=variant(1); short.hit_length=60
        preferred=variant(2); outside=variant(3); outside.hit_length=180
        pool._rating_tallies=Mock(return_value={1:(2,0),2:(0,2)})
        with patch('bot.pool.random.random',return_value=0), \
                patch('bot.pool.random.choices',return_value=[short]) as choices:
            pool._weighted_pick([short,preferred,outside])
        self.assertEqual(choices.call_args.args[0],[short,preferred])
        self.assertGreater(choices.call_args.kwargs['weights'][0],choices.call_args.kwargs['weights'][1])

    def test_dt_evidence_does_not_borrow_wrong_style(self):
        self.edit_dt('UPDATE dt_maps SET dominant_mods="NM",stream_score=0')
        pool = self.pool(stream_enabled=False)
        self.assertFalse(any(m.is_dt for m in pool.maps))

    def test_dt_stream_bpm_is_adjusted_and_dt_farm_is_retained(self):
        pool = self.pool()
        self.assertTrue(all(m.is_farm and m.is_stream and m.stream_bpm==180
                            for m in pool.maps if m.is_dt))

    def test_mismatched_and_invalid_catalogs_fail_closed(self):
        for key,value in [('source_size','1'),('clock_rate','2'),('calculator','wrong'),('built_at','0')]:
            with self.subTest(key=key):
                with sqlite3.connect(self.dt) as c:
                    old = c.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()[0]
                self.edit_dt('UPDATE metadata SET value=? WHERE key=?',(value,key))
                self.assertFalse(any(m.is_dt for m in self.pool().maps))
                self.edit_dt('UPDATE metadata SET value=? WHERE key=?',(old,key))

    def test_old_verified_catalog_remains_usable_and_is_due_for_refresh(self):
        self.edit_dt('UPDATE metadata SET value=? WHERE key=?',
                     (str(time.time()-45*86400),'built_at'))
        pool=self.pool()
        self.assertTrue(pool.dt_catalog_ready())
        self.assertTrue(pool.dt_catalog_refresh_due())
        self.assertTrue(any(m.is_dt for m in pool.maps))
        pool.set_dt_override(True,full=True)
        self.assertTrue(pool.next_map().is_dt)

    def test_fresh_catalog_refresh_not_due(self):
        pool=self.pool()
        self.assertTrue(pool.dt_catalog_ready())
        self.assertFalse(pool.dt_catalog_refresh_due())

    def test_invalid_future_or_nonfinite_build_dates_fail_closed(self):
        for value in ('nan','inf',str(time.time()+86400),'-1'):
            with self.subTest(value=value):
                self.edit_dt('UPDATE metadata SET value=? WHERE key=?',(value,'built_at'))
                self.assertFalse(any(m.is_dt for m in self.pool().maps))

    def test_bad_config_rejected(self):
        for value in (True,'0.25',0,1,float('nan')):
            with self.assertRaises(ValueError):
                self.pool(dt_pick_share=value)
        for value in (True,'5.5',-1,float('inf')):
            with self.assertRaises(ValueError):
                self.pool(dt_auto_threshold=value)

    def test_factory_does_not_mix_with_fixed_hr(self):
        cfg = dict(farm_db=str(self.db), standard_dt_db=str(self.dt), pool_limit=400,
                   sr_min=5.5,sr_max=6.1,max_length=240,min_passcount=10000,
                   _instance_name='test',allowed_mods=[],required_mods=['HR'],freestyle=False)
        self.assertFalse(getattr(make_worker_pool(cfg), 'mixed_dt', False))
        cfg['required_mods']=[]
        self.assertTrue(make_worker_pool(cfg).mixed_dt)


class DTPlaylistTests(unittest.TestCase):
    def bot(self):
        bot = make_bot()
        bot.pool = SimpleNamespace(mixed_dt=True,dt_enabled=Mock(return_value=True),dt_catalog_ready=Mock(return_value=True),
            set_dt_override=Mock(),exclude_dt_beatmap=Mock(),sr_min=5.5,sr_max=6.1,max_length=240,
            get_variant_map=Mock(side_effect=lambda bid,mods: variant(bid) if 'DT' in
                [m.get('acronym') if isinstance(m,dict) else m for m in mods] else self.nm(bid)))
        bot.api=SimpleNamespace(beatmap=Mock(return_value=dict(checksum='a'*32,mode_int=0,status='ranked')))
        bot.current_map=self.nm(200)
        bot.queued=[]
        bot._playlist_maps={}
        bot._server_current_item_id=200
        bot._is_admin=Mock(return_value=False)
        bot._is_moderator=Mock(return_value=False)
        bot._allow_public_command_burst=Mock(return_value=True)
        bot.starting_map=None
        bot.playing_map=None
        return bot

    @staticmethod
    def nm(bid):
        return PoolMap(bid,'Artist',f'Title{bid}','Expert',5.8,180,100,20000,is_farm=True)

    def test_required_dt_is_per_item_and_nm_restores_configured_mods(self):
        bot=self.bot()
        dt_rules=bot._playlist_rules_for_map(variant())
        self.assertEqual(dt_rules['required_mods'], ['DT'])
        self.assertTrue(dt_rules['freestyle'])
        self.assertEqual(bot._playlist_rules_for_map(self.nm(2))['required_mods'], [])
        self.assertEqual(bot.cfg['required_mods'], [])

    def test_dt_and_nm_preserve_explicit_freestyle_preference(self):
        for enabled in (False, True):
            with self.subTest(freestyle=enabled):
                bot = self.bot()
                bot.cfg['freestyle'] = enabled
                self.assertIs(bot._playlist_rules_for_map(variant())['freestyle'], enabled)
                self.assertIs(bot._playlist_rules_for_map(self.nm(2))['freestyle'], enabled)
                bot.hub.edit_current_item = Mock(return_value={'id': 200})
                bot._prefetch_map_pp = Mock()
                bot._notify_discord_lobby_event = Mock()
                bot.apply_map(variant())
                self.assertIs(bot.hub.edit_current_item.call_args.kwargs['freestyle'], enabled)
                self.assertEqual(bot.hub.edit_current_item.call_args.kwargs['required_mods'], ['DT'])
                bot._append_future_map(variant(3))
                self.assertIs(bot.hub.add_playlist_item.call_args.kwargs['freestyle'], enabled)

    def test_dt_freestyle_audits_and_partial_events_preserve_variant(self):
        bot = self.bot()
        packet = dict(id=201, beatmap_id=1, ruleset_id=0,
                      required_mods=['DT'], freestyle=True, star_rating=5.8)
        self.assertFalse(bot._remember_playlist_item(packet)['standard_invalid'])
        self.assertTrue(bot._map_for_item_id(201).is_dt)
        self.assertFalse(bot._remember_playlist_item(dict(id=201))['standard_invalid'])
        self.assertTrue(bot._server_playlist_items[201]['freestyle'])
        for value in ('true', 1, None):
            with self.subTest(malformed_freestyle=value):
                bad = self.bot()
                self.assertTrue(bad._remember_playlist_item(packet | {'freestyle': value})['standard_invalid'])

    def test_dt_source_length_above_cap_is_safe_to_queue_when_actual_length_fits(self):
        bot=self.bot(); item=variant(); item.source_hit_length=270; item.hit_length=180
        self.assertEqual(bot._playlist_rules_for_map(item)['required_mods'],['DT'])
        item.source_hit_length=360; item.hit_length=240
        self.assertEqual(bot._playlist_rules_for_map(item)['required_mods'],['DT'])
        item.source_hit_length=361; item.hit_length=241
        with self.assertRaises(ValueError):
            bot._playlist_rules_for_map(item)

    def test_invalid_ratings_calculators_speed_and_disabled_dt_rejected(self):
        bot=self.bot()
        for field,value in [('sr',6.2),('sr',math.nan),('dt_calculator','old'),('clock_rate',2),
                            ('dt_checksum','broken'),('hit_length',241)]:
            item=variant(); setattr(item,field,value)
            with self.assertRaises(ValueError): bot._playlist_rules_for_map(item)
        bot.pool.dt_enabled.return_value=False
        with self.assertRaises(ValueError): bot._playlist_rules_for_map(variant())

    def test_fixed_extra_mods_reject_plain_dt_rating(self):
        bot=self.bot(); bot.cfg['required_mods']=['HR']
        with self.assertRaises(ValueError): bot._playlist_rules_for_map(variant())

    def test_revision_check_cache_and_force(self):
        bot=self.bot(); item=variant()
        self.assertTrue(bot._check_dt_revision(item))
        self.assertTrue(bot._check_dt_revision(item))
        self.assertEqual(bot.api.beatmap.call_count,1)
        self.assertTrue(bot._check_dt_revision(item,force=True))
        self.assertEqual(bot.api.beatmap.call_count,2)

    def test_unknown_api_never_admits_dt(self):
        bot=self.bot(); bot.api.beatmap.side_effect=RuntimeError('offline')
        self.assertFalse(bot._check_dt_revision(variant()))
        bot.pool.exclude_dt_beatmap.assert_not_called()

    def test_mismatched_checksum_ruleset_status_excludes_dt_only(self):
        for change in [dict(checksum='b'*32),dict(mode_int=3),dict(status='loved')]:
            bot=self.bot(); info=dict(checksum='a'*32,mode_int=0,status='ranked'); info.update(change)
            bot.api.beatmap.return_value=info
            self.assertFalse(bot._check_dt_revision(variant()))
            bot.pool.exclude_dt_beatmap.assert_called_once_with(1)

    def test_same_beatmap_nm_and_dt_bind_by_item_and_server_mods(self):
        bot=self.bot(); bot.current_map=self.nm(1)
        for item_id,mods in ((200,[]),(201,[{'acronym':'DT'}])):
            norm=bot._remember_playlist_item(dict(id=item_id,beatmap_id=1,required_mods=mods,freestyle=False))
            self.assertFalse(norm['standard_invalid'])
        self.assertFalse(bot._map_for_item_id(200).is_dt)
        self.assertTrue(bot._map_for_item_id(201).is_dt)

    def test_partial_event_retains_previous_mods(self):
        bot=self.bot()
        bot._remember_playlist_item(dict(id=201,beatmap_id=1,required_mods=['DT'],freestyle=False))
        norm=bot._remember_playlist_item(dict(id=201,beatmap_id=1,played=False))
        self.assertFalse(norm['standard_invalid'])
        self.assertTrue(bot._map_for_item_id(201).is_dt)

    def test_partial_event_retains_chart_order_and_played_history(self):
        bot=self.bot()
        bot._remember_playlist_item(dict(id=201,beatmap_id=1,required_mods=['DT'],freestyle=False,
                                        was_played=True,order=7))
        norm=bot._remember_playlist_item(dict(id=201))
        self.assertFalse(norm['standard_invalid'])
        self.assertTrue(norm['played'])
        self.assertEqual((norm['beatmap_id'],norm['order']),(1,7))

    def test_partial_event_cannot_clear_custom_speed_invalidation(self):
        bot=self.bot()
        bot._remember_playlist_item(dict(id=201,beatmap_id=1,required_mods=[
            dict(acronym='DT',settings=dict(speed_change=1.25))],freestyle=False))
        self.assertTrue(bot._remember_playlist_item(dict(id=201))['standard_invalid'])

    def test_revision_outage_has_bounded_retry_without_quarantine(self):
        bot=self.bot(); bot.api.beatmap.side_effect=RuntimeError('offline')
        self.assertFalse(bot._check_dt_revision(variant(1)))
        self.assertFalse(bot._check_dt_revision(variant(2)))
        self.assertEqual(bot.api.beatmap.call_count,1)
        bot.pool.exclude_dt_beatmap.assert_not_called()

    def test_unknown_mods_custom_speed_wrong_sr_and_expected_mismatch_hold_start(self):
        for changes in ({},dict(required_mods=['DT'],star_rating=7),
                        dict(required_mods=['DT'],freestyle='true'),
                        dict(required_mods=[dict(acronym='DT',settings=dict(speed_change=1.25))])):
            bot=self.bot(); item=dict(id=201,beatmap_id=1,freestyle=False); item.update(changes)
            self.assertTrue(bot._remember_playlist_item(item)['standard_invalid'])
            self.assertIsNone(bot._map_for_item_id(201))
        bot=self.bot(); bot._playlist_maps[201]=variant(1)
        self.assertTrue(bot._remember_playlist_item(dict(id=201,beatmap_id=1,required_mods=[]))['standard_invalid'])

    def test_optout_removes_future_dt_preserves_nm_and_history(self):
        bot=self.bot(); bot.pool.dt_enabled.return_value=False
        bot.queued=[(201,variant(1)),(202,self.nm(2))]
        bot._playlist_maps.update(dict(bot.queued))
        bot._server_playlist_items[199]=dict(id=199,required_mods=['DT'],played=True)
        self.assertTrue(bot._cleanup_disabled_dt())
        bot.hub.remove_playlist_item.assert_called_once_with(44,201)
        self.assertEqual([qid for qid,m in bot.queued],[202])
        self.assertIn(199,bot._server_playlist_items)

    def test_optout_replaces_upcoming_dt_not_reinterpreting_same_chart(self):
        bot=self.bot(); bot.pool.dt_enabled.return_value=False
        bot.current_map=variant(1)
        bot._next_downloadable_map=Mock(return_value=self.nm(2))
        bot.apply_map=Mock()
        self.assertTrue(bot._cleanup_disabled_dt())
        bot.apply_map.assert_called_once_with(bot._next_downloadable_map.return_value,announce=True)

    def test_optout_never_mutates_active_or_uncertain_match(self):
        for state in (PLAYING,STARTING):
            bot=self.bot(); bot.state=state; bot.pool.dt_enabled.return_value=False
            bot.current_map=variant(1); bot.queued=[(201,variant(2))]
            self.assertTrue(bot._cleanup_disabled_dt())
            bot.hub.remove_playlist_item.assert_not_called()

    def test_optout_missing_nm_holds_start(self):
        bot=self.bot(); bot.pool.dt_enabled.return_value=False; bot.current_map=variant()
        bot._next_downloadable_map=Mock(return_value=None)
        self.assertFalse(bot._cleanup_disabled_dt())

    def test_stale_catalog_cleans_unplayed_dt_even_when_policy_requests_dt(self):
        bot=self.bot(); bot.pool.dt_catalog_ready.return_value=False
        bot.queued=[(201,variant(1))]
        self.assertTrue(bot._cleanup_disabled_dt())
        bot.hub.remove_playlist_item.assert_called_once_with(44,201)

    def test_optout_remove_failure_preserves_identity_for_audit(self):
        bot=self.bot(); bot.pool.dt_enabled.return_value=False
        bot.queued=[(201,variant(1))]; bot.hub.remove_playlist_item.side_effect=RuntimeError('offline')
        self.assertFalse(bot._cleanup_disabled_dt())
        self.assertEqual(bot.queued[0][0],201)

    def test_dt_command_exact_reply_no_playlist_rebuild(self):
        bot=self.bot(); bot.handle_command(1,'.dt')
        bot.pool.set_dt_override.assert_called_once_with(True)
        bot.say.assert_called_once_with('dt enabled, buckle up gamers | Disable with .nm')
        bot.reconcile_playlist.assert_not_called()

    def test_full_dt_command_refreshes_unplayed_only_when_safe(self):
        for state in (IDLE, SCORING, PLAYING, STARTING):
            with self.subTest(state=state):
                bot=self.bot(); bot.state=state
                bot.handle_command(1, '.dt 100')
                bot.pool.set_dt_override.assert_called_once_with(True, full=True)
                self.assertTrue(bot._dt_full_refresh_pending)
                if state in (IDLE, SCORING):
                    bot.reconcile_playlist.assert_called_once_with('DT 100 opt-in', fetch_remote=True)
                else:
                    bot.reconcile_playlist.assert_not_called()
                bot.hub.remove_playlist_item.assert_not_called()
                bot.set_state.assert_not_called()

    def test_full_dt_percentage_alias_and_invalid_arguments(self):
        bot=self.bot(); bot.handle_command(1, '.dt 100%')
        bot.pool.set_dt_override.assert_called_once_with(True, full=True)
        for command in ('.dt 50', '.dt 0', '.dt foo', '.dt 100 extra', '.nm 100'):
            bot=self.bot(); bot.handle_command(1, command)
            bot.pool.set_dt_override.assert_not_called()
            self.assertIn('Usage:', bot.say.call_args.args[0])

    def test_mix_and_nm_cancel_pending_full_refresh(self):
        for command in ('.dt', '.nm'):
            bot=self.bot(); bot.state=PLAYING; bot._dt_full_refresh_pending=True
            bot.handle_command(1, command)
            self.assertFalse(bot._dt_full_refresh_pending)

    def full_bot(self):
        bot=self.bot(); bot.pool.dt_full=True; bot._dt_full_refresh_pending=True
        bot.apply_map=Mock(); bot._next_downloadable_map=Mock(return_value=variant(3))
        return bot

    def test_full_refresh_replaces_nm_keeps_dt_and_history(self):
        bot=self.full_bot()
        bot.queued=[(201,self.nm(1)),(202,variant(2)),(203,self.nm(4))]
        bot._server_playlist_items[203]=dict(played=True)
        self.assertTrue(bot._refresh_full_dt_playlist())
        bot.apply_map.assert_called_once()
        bot.hub.remove_playlist_item.assert_called_once_with(44,201)
        self.assertEqual([m.is_dt for _,m in bot.queued], [True,False,True])
        self.assertFalse(bot._dt_full_refresh_pending)
        self.assertEqual(bot.hub.add_playlist_item.call_args.kwargs['required_mods'], ['DT'])

    def test_full_refresh_nm_fallback_does_not_remove_existing_entries(self):
        bot=self.full_bot(); bot._next_downloadable_map.return_value=self.nm(3)
        bot.queued=[(201,self.nm(1))]
        self.assertTrue(bot._refresh_full_dt_playlist())
        bot.apply_map.assert_not_called(); bot.hub.remove_playlist_item.assert_not_called()
        self.assertEqual(bot.queued[0][0],201)

    def test_full_refresh_unknown_ids_running_and_played_current_are_untouched(self):
        for problem in ('playing','starting','anonymous','played','expired'):
            with self.subTest(problem=problem):
                bot=self.full_bot()
                if problem=='playing': bot.state=PLAYING
                if problem=='starting': bot.state=STARTING
                if problem=='anonymous': bot.queued=[(None,self.nm(1))]
                if problem in ('played','expired'):
                    bot._server_playlist_items[bot.current_item_id]={problem:True}
                bot._refresh_full_dt_playlist()
                bot.apply_map.assert_not_called(); bot.hub.remove_playlist_item.assert_not_called()
                self.assertTrue(bot._dt_full_refresh_pending)

    def test_full_refresh_remove_failure_retains_identity_for_audit(self):
        bot=self.full_bot(); bot.current_map=variant(200)
        bot.queued=[(201,self.nm(1))]
        bot.hub.remove_playlist_item.side_effect=RuntimeError('offline')
        self.assertFalse(bot._refresh_full_dt_playlist())
        self.assertEqual(bot.queued[0][0],201)
        self.assertTrue(bot._dt_full_refresh_pending)
        bot.hub.add_playlist_item.assert_not_called()

    def test_full_dt_unavailable_revision_falls_back_to_nm(self):
        bot=self.bot(); bot.pool.dt_full=True
        bot.pool.next_map=Mock(side_effect=lambda **kwargs: self.nm(2) if kwargs else variant(1))
        bot._map_is_downloadable=Mock(return_value=True)
        bot.api.beatmap.side_effect=RuntimeError('unavailable')
        self.assertFalse(bot._next_downloadable_map().is_dt)

    def test_full_refresh_unknown_remote_room_never_mutates(self):
        bot=self.full_bot(); bot.api.room_probe=Mock(return_value=('unknown',None))
        self.assertFalse(LobbyBot.reconcile_playlist(bot, 'DT 100', fetch_remote=True))
        bot.apply_map.assert_not_called(); bot.hub.remove_playlist_item.assert_not_called()

    def test_nm_during_playing_defers_cleanup(self):
        bot=self.bot(); bot.state=PLAYING; bot.playing_map=variant()
        bot.handle_command(1,'.nm')
        bot.pool.set_dt_override.assert_called_once_with(False)
        bot.reconcile_playlist.assert_not_called()
        bot.hub.remove_playlist_item.assert_not_called()

    def test_nm_cancels_dt_countdown_only_after_ack(self):
        bot=self.bot(); bot.state=STARTING; bot.starting_map=variant()
        bot._stop_match_countdown=Mock(return_value=False)
        bot.handle_command(1,'.nm')
        bot.set_state.assert_not_called(); bot.reconcile_playlist.assert_not_called()
        bot._stop_match_countdown.return_value=True
        bot._clear_start_recovery_tracking=Mock()
        bot.set_state.side_effect=lambda state:setattr(bot,'state',state)
        bot.handle_command(1,'.nm')
        bot.set_state.assert_called_once_with(IDLE)
        bot.reconcile_playlist.assert_called_once_with('DT opt-out',fetch_remote=True)

    def test_mania_rejects_dt_command(self):
        bot=self.bot(); bot.cfg['ruleset_id']=3; bot._handle_mania_command=Mock(return_value=False)
        bot.handle_command(1,'.dt')
        bot.pool.set_dt_override.assert_not_called()

    def test_pp_prefetch_uses_dt_without_changing_nm_call_shape(self):
        bot=self.bot()
        bot._prefetch_map_pp(variant(1)); bot._prefetch_map_pp(self.nm(2))
        self.assertEqual(bot.pp_projection.prefetch.call_args_list[0].kwargs,dict(mods=('DT',)))
        self.assertEqual(bot.pp_projection.prefetch.call_args_list[1].kwargs,{})

    def test_pp_command_automatically_uses_dt(self):
        bot=self.bot(); bot.current_map=variant(1)
        bot.pp_projection.get=Mock(return_value=None)
        bot._show_current_map_pp(mods=('HD',))
        self.assertEqual(bot.pp_projection.get.call_args.kwargs['mods'],('HD','DT'))

    def test_dt_replay_disabled_rejected_before_any_mutation(self):
        bot=self.bot(); bot.pool.dt_enabled.return_value=False
        self.assertFalse(bot.replay_last_map(variant()))
        bot.reconcile_playlist.assert_not_called()

    def test_time_command_uses_adjusted_dt_length_and_frozen_playing_map(self):
        bot=self.bot(); bot.state=PLAYING; bot.playing_map=variant(1)
        bot._match_started_at=1000; bot._state_since=1000
        with patch('bot.bot.time.time',return_value=1030):
            self.assertEqual(bot._match_remaining_seconds(),90)

    def test_future_append_preserves_dt_mods_and_pp_variant(self):
        bot=self.bot(); bot._append_future_map(variant(1))
        self.assertEqual(bot.hub.add_playlist_item.call_args.kwargs['required_mods'],['DT'])
        self.assertTrue(bot.hub.add_playlist_item.call_args.kwargs['freestyle'])
        self.assertEqual(bot.pp_projection.prefetch.call_args.kwargs,dict(mods=('DT',)))


if __name__ == '__main__':
    unittest.main()
