"""Taiko engine admission and familiar player behavior over the retained bot."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from bot.bot import LobbyBot
from bot.mode import merge_room_config,validate_taiko_config,ruleset_id
from bot.taiko_pool import TaikoPool
from bot.taiko_runtime import playlist_valid,handle_command
from poolrotater_host.config import PRESETS
from poolrotater_host.catalog_store import mode_for
from poolrotater_host.gameplay_defaults import settings_for
from poolrotater_host.managed_host import settings
from poolrotater_host.catalog_taiko import MAP_COLUMNS
from tests.test_taiko_catalog733 import map_tuple


class TaikoEngine733Tests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'catalog.db'
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE taiko_maps ('+','.join(n+' '+t for n,t in MAP_COLUMNS)+')')
            db.executemany('INSERT INTO taiko_maps VALUES('+','.join('?' for _ in MAP_COLUMNS)+')',
                           [map_tuple(1,mode=1),map_tuple(2,mode=0)])
        self.pool=TaikoPool(self.path,purpose='production',sr_min=3,sr_max=5,max_length=240,ratings_db=None)
        self.bot=LobbyBot.__new__(LobbyBot)
        self.bot.cfg={'ruleset_id':1,'allowed_mods':[],'required_mods':[],'freestyle':False}
        self.bot.pool=self.pool;self.bot.prefix='.';self.bot.say=Mock()

    def test_ruleset_validation_accepts_taiko_and_other_known_integer_modes(self):
        for value in (0,1,2,3):
            with self.subTest(value=value):
                self.assertEqual(ruleset_id({'ruleset_id':value}),value)
        for value in ('3',True,4,5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ruleset_id({'ruleset_id':value})

    def test_presets_and_default_profiles_never_inherit_standard_variants(self):
        for preset in ('taiko-fixed','taiko-auto'):
            self.assertEqual(PRESETS[preset]['ruleset'],1)
            self.assertEqual(mode_for({'preset':preset}),'taiko')
            cfg=settings_for(preset);validate_taiko_config(cfg)
            self.assertEqual(PRESETS[preset]['auto_difficulty'],preset=='taiko-auto')
            settings.validate({'preset':preset,'sr_min':3.,'sr_max':4.})
            with self.assertRaises(ValueError):settings.validate({'freestyle':True},preset=preset)
        merged=merge_room_config({},dict(ruleset_id=1,auto_difficulty=True,taiko_catalog='catalog.db'),
                                 dict(allowed_mods=['HR'],freestyle=True,stream_enabled=True))
        self.assertEqual(merged['allowed_mods'],[]);self.assertFalse(merged['freestyle'])
        self.assertTrue(merged['auto_difficulty'])

    def test_taiko_auto_room_tuning_roundtrips_without_standard_universal_leakage(self):
        universal={'auto_sr_bias':1.25,'auto_sr_half_width':1.2,
                   'map_pp_display_enabled':False,'map_pp_auto_announce_enabled':False,
                   'preferred_min_length':40,'preferred_max_length':180,
                   'preferred_length_share':.9,
                   'allowed_mods':['HR'],'required_mods':['DT'],'freestyle':True,
                   'loved_mode':True,'stream_enabled':True,'mania_farm_enabled':True}
        room={'ruleset_id':1,'preset':'taiko-auto','auto_difficulty':True,
              'auto_sr_bias':.25,'auto_sr_half_width':.35,
              'preferred_min_length':80,'preferred_max_length':120,
              'preferred_length_share':.4,
              'map_pp_display_enabled':False,'map_pp_auto_announce_enabled':False}

        merged=merge_room_config({},room,universal)
        self.assertEqual((merged['auto_sr_bias'],merged['auto_sr_half_width']),(.25,.35))
        self.assertFalse(merged['map_pp_display_enabled'])
        self.assertFalse(merged['map_pp_auto_announce_enabled'])
        self.assertEqual((merged['preferred_min_length'],merged['preferred_max_length'],
                          merged['preferred_length_share']),(80,120,.4))
        self.assertEqual((merged['allowed_mods'],merged['required_mods'],merged['freestyle']),([],[],False))
        self.assertEqual((merged['loved_mode'],merged['stream_enabled'],merged['mania_farm_enabled']),
                         (False,False,False))

        # A second load sees the values persisted in the room file and keeps
        # them local even when Standard's universal tuning remains different.
        reloaded=merge_room_config({},merged,universal)
        self.assertEqual((reloaded['auto_sr_bias'],reloaded['auto_sr_half_width']),(.25,.35))
        self.assertFalse(reloaded['map_pp_display_enabled'])
        self.assertFalse(reloaded['map_pp_auto_announce_enabled'])
        self.assertEqual((reloaded['preferred_min_length'],reloaded['preferred_max_length'],
                          reloaded['preferred_length_share']),(80,120,.4))

        defaulted=merge_room_config({}, {'ruleset_id':1,'preset':'taiko-auto'}, universal)
        self.assertEqual((defaulted['auto_sr_bias'],defaulted['auto_sr_half_width']),(0.,.5))
        self.assertTrue(defaulted['map_pp_display_enabled'])
        self.assertTrue(defaulted['map_pp_auto_announce_enabled'])

    def test_taiko_room_rejects_a_non_normal_clock_rate(self):
        with self.assertRaisesRegex(ValueError,'clock rate 1.0 only'):
            merge_room_config({}, {'ruleset_id':1,'clock_rate':1.5}, {})

    def test_taiko_merge_rejects_unsupported_room_passcount_and_map_age(self):
        for key in ('min_passcount','max_map_age_years'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                merge_room_config({}, {'ruleset_id':1,key:1}, {})

    def test_taiko_passcount_and_age_filters_must_remain_zero(self):
        for key in ('min_passcount','max_map_age_years'):
            with self.subTest(key=key):
                validate_taiko_config({key:0})
                with self.assertRaises(ValueError):
                    validate_taiko_config({key:1})

    def test_managed_taiko_settings_reject_unsupported_filter_patches(self):
        current={'preset':'taiko-auto','min_passcount':0,'max_map_age_years':0,
                 'allowed_mods':[],'required_mods':[],'freestyle':False,
                 'loved_mode':False,'stream_enabled':False,'mania_farm_enabled':False,
                 'high_star_relax_enabled':False}
        for key in ('min_passcount','max_map_age_years'):
            with self.subTest(key=key):
                self.assertEqual(settings.validate({key:0},current=current),{key:0})
                with self.assertRaises(ValueError):
                    settings.validate({key:1},current=current)

    def test_taiko_make_rules_keep_exact_native_and_conversion_identity(self):
        for mid in (1,2):
            m=self.pool.get_map(mid)
            self.assertFalse(m.is_mania_convert)
            self.assertEqual(self.bot._playlist_rules_for_map(m),
                             dict(ruleset_id=1,allowed_mods=[],required_mods=[],freestyle=False))
            item={'ruleset_id':1,'beatmap_id':mid,'required_mods':[],'allowed_mods':[],
                  'freestyle':False,'checksum':m.file_md5,'star_rating':m.sr,
                  'beatmap':{'mode_int':m.original_mode}}
            self.assertTrue(playlist_valid(self.bot,item))
            self.assertFalse(playlist_valid(self.bot,item|{'ruleset_id':0}))
            self.assertFalse(playlist_valid(self.bot,item|{'required_mods':['DT']}))
            self.assertFalse(playlist_valid(self.bot,item|{'star_rating':9.}))

    def test_real_worker_guard_allows_only_reading_the_declared_taiko_catalog(self):
        from rolling.storage import install_guard
        from bot.taiko_preferences import prepare,preference
        state=self.path.parent/'ratings.db'
        prepare(state)
        original=sqlite3.connect
        try:
            install_guard({'ruleset_id':1,'taiko_catalog':str(self.path),'ratings_db':str(state)})
            pool=TaikoPool(self.path,purpose='production',sr_min=3,sr_max=5,max_length=240,ratings_db=None)
            self.assertEqual(len(pool.maps),2)
            preference(state,'taiko:ranked',7,(3.,4.))
            self.assertEqual(preference(state,'taiko:ranked',7),(3.,4.))
            with sqlite3.connect(str(self.path)) as db:
                with self.assertRaises(sqlite3.DatabaseError):db.execute('DELETE FROM taiko_maps')
            with self.assertRaises(sqlite3.OperationalError):sqlite3.connect(str(self.path.parent/'other.db'))
        finally:sqlite3.connect=original

    def test_missing_calibration_preserves_configured_range(self):
        self.assertEqual(self.bot._auto_range_for_pp(5000,half_width=.3,bias=.2),(3.,5.))
        self.assertEqual(self.bot._target_sr_for_pp(5000),4.)

    def test_difficulty_preferences_and_map_pp_do_not_use_standard_state(self):
        from bot.taiko_preferences import preference_scope,map_type_label
        self.assertEqual(preference_scope(self.bot.cfg,self.pool),'taiko:ranked')
        self.assertNotEqual(preference_scope(self.bot.cfg,self.pool),preference_scope({'ruleset_id':0},self.pool))
        item=self.pool.get_map(1)
        self.assertEqual(map_type_label(item),'Taiko')
        self.bot.pp_projection=Mock()
        self.bot.current_map=item
        self.bot._post_match_pp_announce_pending=True
        self.bot._post_match_pp_completed_beatmap_id=2
        self.assertTrue(self.bot._maybe_announce_post_match_pp())
        self.bot.pp_projection.get.assert_not_called()
        self.assertIn('Taiko NM',self.bot.say.call_args.args[0])

    def test_unsupported_commands_reply_without_modifying_the_pool(self):
        self.bot._pp_command_map=lambda:self.pool.get_map(1)
        for cmd in ('freedomdive','stream','dt','mods','freestyle','pp'):
            before=(self.pool.sr_min,self.pool.sr_max,len(self.pool.maps))
            self.assertTrue(handle_command(self.bot,cmd,[cmd]))
            self.assertTrue(self.bot.say.called)
            self.assertEqual(before,(self.pool.sr_min,self.pool.sr_max,len(self.pool.maps)))
