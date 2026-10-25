"""Exercise Taiko selection and the retained live lobby wiring together."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bot.pp_projection import MapPPProjection
from bot.taiko_pool import TaikoPool
from tests.test_taiko_catalog733 import MAP_COLUMNS, map_tuple


class TaikoWiring733Tests(unittest.TestCase):
    def make_pool(self, *, calibration=True, rows=None):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'taiko.db'
        values=rows or [
            map_tuple(101,sr=3.7,length=100,artist='Shared Artist',title='Shared Song'),
            map_tuple(102,sr=3.8,length=100,artist='Shared Artist',title='Shared Song'),
            map_tuple(103,sr=3.9,length=105,artist='Shared Artist',title='Shared Song'),
            map_tuple(104,sr=4.0,length=110,artist='Other Artist',title='Other Song'),
            map_tuple(105,sr=4.1,length=115,artist='Another Artist',title='Another Song'),
            map_tuple(106,sr=4.2,length=120,artist='Last Artist',title='Last Song'),
        ]
        with sqlite3.connect(path) as db:
            columns=','.join(f'{name} {kind}'+(' PRIMARY KEY' if name=='beatmap_id' else '')
                             for name,kind in MAP_COLUMNS)
            db.execute(f'CREATE TABLE taiko_maps ({columns})')
            db.executemany('INSERT INTO taiko_maps VALUES ('+
                           ','.join('?' for _ in MAP_COLUMNS)+')',values)
            db.execute('CREATE INDEX taiko_song_spacing ON taiko_maps(artist,title)')
            if calibration:
                db.execute('CREATE TABLE taiko_auto_calibration '
                    '(pp REAL PRIMARY KEY,sr REAL NOT NULL,band_floor INTEGER NOT NULL,'
                    'players INTEGER NOT NULL,scores INTEGER NOT NULL,charts INTEGER NOT NULL)')
                db.executemany('INSERT INTO taiko_auto_calibration VALUES (?,?,?,?,?,?)',[
                    (500.,3.,0,8,32,12),(1500.,5.,1000,8,32,12),(2500.,6.,2000,8,32,12)])
        return TaikoPool(str(path),purpose='production',sr_min=3.,sr_max=5.,
            max_length=180,min_passcount=0,max_map_age_years=0,limit=100,
            taiko_min_length=0,ratings_db=None)

    def make_live_bot(self,pool):
        # Reuse the actual multi-room LobbyBot/referee fixture, then bind a
        # qualified Taiko catalog as the production selector for this room.
        from tests.test_headless_multiroom import ProductionControllerTests
        fixture=ProductionControllerTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        bot=fixture.bots[0]
        bot.pool=pool
        pool.ratings_reader=bot.rating_reader
        bot.cfg.update({'ruleset_id':1,'preset':'taiko-auto','auto_difficulty':True,
            'sr_min':3.,'sr_max':5.,'max_length':180,'min_players':1,
            'countdown_seconds':10,'auto_pool_min_maps':6,'queue_ahead':2,
            'auto_sr_half_width':.4,'auto_sr_bias':0.,'allowed_mods':[],
            'required_mods':[],'freestyle':False,'loved_mode':False,'stream_enabled':False,
            'mania_farm_enabled':False,
            'min_passcount':0,'max_map_age_years':0,'high_star_relax_enabled':False})
        bot.manual_range=False
        return fixture,bot

    def test_taiko_pool_spaces_same_song_and_relaxes_only_when_sparse(self):
        pool=self.make_pool()
        with patch.object(pool,'_weighted_pick',side_effect=lambda candidates,**_kw:candidates[0]):
            first=pool.next_map()
            second=pool.next_map()
            self.assertEqual(first.beatmap_id,101)
            self.assertEqual(second.beatmap_id,104)
            self.assertNotEqual(pool._song_key(first),pool._song_key(second))

            # Once only the same-song family remains, the shared selector
            # relaxes old song history instead of returning an empty pick.
            pool.maps=[pool.get_map(101),pool.get_map(102),pool.get_map(103)]
            pool.record_song_play(first)
            fallback=pool.next_map()
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.ruleset_id,1)
        self.assertEqual(pool._song_key(fallback),pool._song_key(first))

    def test_taiko_profile_pp_drives_auto_range_and_missing_calibration_falls_back(self):
        pool=self.make_pool()
        _fixture,bot=self.make_live_bot(pool)
        uid=7654321
        profile={'id':uid,'username':'Taiko profile','statistics':{'pp':1000.}}
        bot.api.user=Mock(return_value=profile)
        bot.api.user_top_scores=Mock(return_value=[])
        bot._record_lobby_join=Mock()
        bot.on_user_joined(uid)
        bot.api.user.assert_called_once_with(uid,mode='taiko')
        bot.api.user_top_scores.assert_called_once_with(uid,limit=100,mode='taiko')
        self.assertEqual(bot.players[uid]['pp'],1000.)
        bot.players[uid]['status']='ready'
        with patch.object(bot,'update_room_name_for_range'):
            bot.apply_pending_auto_retune(force=True)
        self.assertAlmostEqual(pool.sr_min,3.6)
        self.assertAlmostEqual(pool.sr_max,4.4)

        current=(pool.sr_min,pool.sr_max)
        pool._taiko_calibration=[]
        with patch.object(bot,'update_room_name_for_range'):
            bot.apply_pending_auto_retune(force=True)
        self.assertEqual((pool.sr_min,pool.sr_max),current)

    def test_real_taiko_lobby_uses_shared_ready_countdown_skip_greeting_and_map_output(self):
        pool=self.make_pool();fixture,bot=self.make_live_bot(pool)
        first=pool.get_map(101);replacement=pool.get_map(104)
        bot.say=Mock();bot.players={7:{'name':'One','status':'ready'},
                                    8:{'name':'Two','status':'idle'}}
        bot.ready={7};bot.state='IDLE';bot.current_map=first;bot.current_item_id=7001
        bot.reconcile_playlist=Mock(return_value=True)
        bot._map_is_downloadable=Mock(return_value=True)
        bot.apply_pending_room_name_update=Mock()
        bot._active_chat_countdown_extension=Mock(return_value=0)
        fixture.hub.calls.clear()

        # In a Taiko room, one ready player lets the shared two-player AFK
        # grace start; with no ready players the shared gate holds the start.
        bot.ready.clear();bot.start_match()
        self.assertFalse(any(call[0]=='StartMatch' for call in fixture.hub.calls))
        bot.ready={7};bot.start_match()
        start=[call for call in fixture.hub.calls if call[0]=='StartMatch']
        self.assertEqual(start[-1][1][1]['countdown'],15)
        self.assertEqual(bot.starting_map.ruleset_id,1)

        bot.set_state('IDLE');bot.current_map=first;bot.current_item_id=7001
        bot.starting_map=None;bot.ready={7}
        bot.players[7]['status']='ready';bot.players[8]['status']='idle'
        bot.queued=[(7002,replacement)]
        bot.apply_map.reset_mock()
        with patch.object(bot,'_allow_public_command_burst',return_value=True):
            bot.handle_command(7,'.skip')
            self.assertEqual(bot._skip_votes,{7})
            bot.handle_command(8,'.skip')
        bot.apply_map.assert_called_once_with(replacement,announce=True)
        self.assertEqual(bot.apply_map.call_args.args[0].ruleset_id,1)
        self.assertEqual(bot._skip_vote_until,0)

        bot.cfg['custom_join_greetings']={str(7654322):'Welcome {name} to {room}'}
        bot.cfg['_instance_name']='taiko-room'
        bot.players[7654322]={'name':'TaikoGuest','status':'ready'}
        self.assertTrue(bot._send_custom_join_greeting(7654322))
        self.assertIn('TaikoGuest',bot.say.call_args.args[0])
        self.assertIn('taiko-room',bot.say.call_args.args[0])

        bot.current_map=replacement;bot.current_item_id=7002
        bot.handle_command(7,'.map')
        self.assertIn('Taiko NM',bot.say.call_args.args[0])
        bot._announce_map(replacement)
        self.assertIn('Next (Taiko)',bot.say.call_args.args[0])
        self.assertIn('PP NM SS',bot.say.call_args.args[0])

    def test_taiko_afk_countdown_uses_shared_ready_gate_and_grace(self):
        pool=self.make_pool();fixture,bot=self.make_live_bot(pool)
        bot.players={7:{'name':'Ready','status':'ready'},
                     8:{'name':'AFK','status':'idle'}}
        bot.ready=set();bot.state='IDLE';bot.current_map=pool.get_map(101)
        bot.reconcile_playlist=Mock(return_value=True)
        bot._map_is_downloadable=Mock(return_value=True)
        bot.apply_pending_room_name_update=Mock()
        bot._active_chat_countdown_extension=Mock(return_value=0)
        fixture.hub.calls.clear()

        # Nobody ready holds the shared start gate; one ready player starts
        # with the shared AFK grace applied to the idle Taiko player.
        bot.start_match()
        self.assertFalse(any(call[0]=='StartMatch' for call in fixture.hub.calls))
        bot.ready={7};bot.start_match()
        starts=[call for call in fixture.hub.calls if call[0]=='StartMatch']
        self.assertEqual(starts[-1][1][1]['countdown'],15)
        self.assertEqual(bot._unready_start_grace_users,{8})
        self.assertEqual(bot.starting_map.ruleset_id,1)

        # When the second Taiko player is ready there is no AFK grace.
        bot.set_state('IDLE');bot.players[8]['status']='ready';bot.ready={7,8}
        fixture.hub.calls.clear();bot.start_match()
        starts=[call for call in fixture.hub.calls if call[0]=='StartMatch']
        self.assertEqual(starts[-1][1][1]['countdown'],10)
        # The set records that player 8 already received this one-time grace;
        # becoming ready removes the countdown extension, not the history.
        self.assertEqual(bot._unready_start_grace_users,{8})
        self.assertEqual(bot.starting_map.ruleset_id,1)

    def test_taiko_difficulty_vote_changes_real_fixed_pool_range(self):
        pool=self.make_pool();_fixture,bot=self.make_live_bot(pool)
        bot.cfg.update({'preset':'taiko-fixed','auto_difficulty':False,
            'sr_min':3.0,'sr_max':5.0,'max_length':180,
            'difficulty_vote_ratio':0.5,'difficulty_vote_step':0.25,
            'difficulty_vote_max_steps':3})
        bot.manual_range=True
        bot.players={7:{'name':'One','status':'idle'},8:{'name':'Two','status':'idle'}}
        bot.say=Mock();bot.update_room_name_for_range=Mock()

        bot.cast_difficulty_vote(7,'harder')
        self.assertEqual((pool.sr_min,pool.sr_max),(3.0,5.0))
        self.assertIn('1/2',bot.say.call_args.args[0])
        bot.cast_difficulty_vote(8,'harder')

        self.assertEqual((pool.sr_min,pool.sr_max),(3.25,5.25))
        self.assertEqual(bot._difficulty_bias_layers[-1]['supporters'],{7,8})
        selected=pool.next_map()
        self.assertIsNotNone(selected)
        self.assertEqual(selected.ruleset_id,1)
        pool.verify_variant(selected,in_range=True)

    def test_taiko_missed_start_recovery_completes_the_same_map_without_restart(self):
        pool=self.make_pool();fixture,bot=self.make_live_bot(pool)
        completed=pool.get_map(101);bot.state='STARTING';bot._state_since=0.0
        bot.starting_map=completed;bot.starting_item_id=7001
        bot._start_snapshot_players={7,8}
        bot.api.room_probe=Mock(return_value=('ok',{
            'room_id':bot.room_id,'playlist_item_id':7002,
            'playlist':[{'id':7001,'beatmap_id':completed.beatmap_id,
                         'was_played':True,'order':1}] }))
        bot._sync_from_room_snapshot=Mock(return_value=False)
        fixture.hub.calls.clear()

        self.assertTrue(bot._recover_missed_start_event())

        self.assertEqual(bot.state,'SCORING')
        self.assertEqual(bot.playing_item_id,7001)
        self.assertEqual(bot.last_played_map.beatmap_id,completed.beatmap_id)
        self.assertEqual(bot.last_played_map.ruleset_id,1)
        self.assertEqual(bot.scoring_item_id,7001)
        self.assertIsNone(bot.starting_item_id)
        self.assertIsNone(bot.starting_map)
        self.assertFalse(any(call[0]=='StartMatch' for call in fixture.hub.calls))
        self.assertEqual(pool._song_key(completed),pool._song_key(bot.last_played_map))

    def test_taiko_room_recovers_transport_in_place_and_keeps_taiko_selection(self):
        pool=self.make_pool();fixture,bot=self.make_live_bot(pool)
        room_id=bot.room_id;fixture.hub._connect_generation+=1
        bot.api.room_probe=Mock(return_value=('ok',{'room_id':room_id}))
        bot._await_referee_roster=Mock()
        bot._sync_from_room_snapshot=Mock(return_value=False)
        bot._join_room_chat=Mock(return_value=True)
        bot.reconcile_playlist=Mock(return_value=True)
        makes_before=len([call for call in fixture.hub.calls if call[0]=='MakeRoom'])
        joins_before=len([call for call in fixture.hub.calls if call[0]=='JoinRoom'])

        bot._recover_hub_transport('Taiko room reconnect test')

        self.assertEqual(bot.room_id,room_id)
        bot.api.room_probe.assert_called_once_with(room_id)
        bot.reconcile_playlist.assert_called_once()
        self.assertEqual(len([call for call in fixture.hub.calls if call[0]=='MakeRoom']),makes_before)
        self.assertEqual(len([call for call in fixture.hub.calls if call[0]=='JoinRoom']),joins_before+1)
        selected=pool.next_map()
        self.assertEqual(selected.ruleset_id,1)
        pool.verify_variant(selected,in_range=True)

    def test_taiko_playback_and_catalog_pp_survive_optional_calculator_failure(self):
        pool=self.make_pool();_fixture,bot=self.make_live_bot(pool)
        item=pool.get_map(101)
        cache=tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        projection=MapPPProjection({'ruleset_id':1,'map_pp_display_enabled':True,
            'map_pp_cache_dir':cache.name})
        self.addCleanup(projection.stop)
        projection._download_osu=lambda _bid:Path('/tmp/invented-taiko-map.osu')
        projection._calculator=lambda:(_ for _ in ()).throw(RuntimeError('test calculator unavailable'))
        projection.prefetch(item.beatmap_id)
        key=projection._request_key(item.beatmap_id,None,None)
        event=projection._events[key]
        self.assertTrue(event.wait(2.0))
        self.assertTrue(projection._thread.is_alive())
        self.assertIsNone(projection.get(item.beatmap_id))

        projection.get=Mock(side_effect=RuntimeError('optional calculator unavailable'))
        bot.pp_projection=projection;bot.say=Mock();bot.current_map=item
        bot.current_item_id=9001;bot.state='IDLE'
        bot.handle_command(7,'.pp')
        self.assertIn('Taiko NM reference',bot.say.call_args.args[0])
        bot._post_match_pp_announce_pending=True
        bot._post_match_pp_completed_beatmap_id=9000
        self.assertTrue(bot._maybe_announce_post_match_pp())
        self.assertIn('(Taiko NM) PP',bot.say.call_args.args[0])
        bot._announce_map(item)
        self.assertIn('PP NM SS',bot.say.call_args.args[0])
        projection.get.assert_not_called()


if __name__=='__main__':
    unittest.main()
