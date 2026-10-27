"""Keep the old room through selection/readiness failures, without retry storms."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from bot.bot import LobbyBot
from bot.hub import HubInvocationUncertain
from poolrotater_host.oauth import AuthError
from poolrotater_host.referee_errors import RefereeCommandRejected


class RecreationPreflightTests(unittest.TestCase):
    def bot(self):
        bot=object.__new__(LobbyBot)
        bot.room_id=101;bot.channel_id=1101;bot.playing_item_id=None;bot._activity_match_key=None
        bot.cfg={'ruleset_id':0,'hard_restart_interval_seconds':7200}
        bot.current_map=SimpleNamespace(beatmap_id=1,is_dt=False,is_mania_convert=False)
        bot.queued=[(202,SimpleNamespace(beatmap_id=2))]
        bot.playing_map=None;bot.starting_map=None
        bot.players={};bot._pending_hard_restart_maps=None
        bot._automatic_hard_restart_due_at=time.time()-1
        bot._automatic_hard_restart_waiting_for_empty=False
        bot.hub=SimpleNamespace(refresh_ready=Mock(return_value=True),close_room=Mock())
        bot.ratings=SimpleNamespace(record_match_end=Mock(),record_room_close=Mock())
        bot.say=Mock();bot._update_discord_public_status=Mock()
        bot._reset_room_runtime_state_for_reopen=Mock();bot.set_state=Mock()
        def create(**kwargs):bot.room_id=102
        bot.create_room=Mock(side_effect=create)
        bot._refresh_operator_exclusions=Mock(return_value=True)
        bot._map_is_downloadable=Mock(return_value=True)
        reserved={1,2}
        def reserve(values):reserved.clear();reserved.update(values)
        def pick(**kwargs):return bot.current_map if 1 not in reserved else None
        bot.pool=SimpleNamespace(reserve_playlist_maps=reserve,next_map=Mock(side_effect=pick),
                                 mixed_dt=False,dt_full=False,reserved=reserved)
        return bot

    def test_single_map_pool_is_verified_before_close_and_old_reservations_restored(self):
        bot=self.bot();order=[]
        bot._map_is_downloadable.side_effect=lambda m:order.append('verify')or True
        bot.hub.close_room.side_effect=lambda rid:order.append('close')
        with patch('bot.bot.time.sleep'):
            self.assertTrue(bot.hard_restart_room(announce=False,close_reason='automatic_hard_restart'))
        self.assertEqual(order,['verify','close'])
        bot.create_room.assert_called_once_with(initial_map=bot.current_map)
        self.assertEqual(bot.pool.reserved,{1,2})
        self.assertEqual(bot.cfg['hard_restart_interval_seconds'],7200)
        self.assertEqual(bot.room_id,102)

    def test_selection_failure_keeps_old_room_and_visible_queue(self):
        bot=self.bot();bot.pool.next_map.side_effect=AuthError('provider_deadline_exceeded')
        before=(bot.room_id,bot.current_map,list(bot.queued))
        with patch('bot.bot.time.monotonic',return_value=50):
            self.assertFalse(bot.hard_restart_room(announce=False,close_reason='automatic_hard_restart'))
        self.assertEqual((bot.room_id,bot.current_map,bot.queued),before)
        self.assertEqual(bot.pool.reserved,{1,2});self.assertEqual(bot._hard_restart_admission_retry_at,80)
        bot.hub.close_room.assert_not_called();bot.create_room.assert_not_called()
        bot._reset_room_runtime_state_for_reopen.assert_not_called()
        bot._update_discord_public_status.assert_not_called()

    def test_no_eligible_map_defers_without_close_or_process_exit(self):
        bot=self.bot();bot.pool.next_map.side_effect=None;bot.pool.next_map.return_value=None
        self.assertFalse(bot.hard_restart_room(announce=False,close_reason='automatic_hard_restart'))
        self.assertEqual(bot.room_id,101);self.assertEqual(bot.pool.reserved,{1,2})
        bot.hub.close_room.assert_not_called();bot.create_room.assert_not_called()

    def test_readiness_backoff_is_honored_before_proceeding_log_or_fence(self):
        bot=self.bot();bot.hub.refresh_ready.return_value=False
        with patch('bot.bot.time.monotonic',return_value=10):
            with self.assertLogs('bot',level='INFO') as logs:
                self.assertFalse(bot._maybe_queue_automatic_hard_restart())
                for _ in range(30):self.assertFalse(bot._maybe_queue_automatic_hard_restart())
        self.assertEqual(sum('proceeding'in x for x in logs.output),1)
        bot.hub.refresh_ready.assert_called_once();bot.pool.next_map.assert_not_called()
        bot.hub.close_room.assert_not_called()
        self.assertEqual(bot._hard_restart_admission_retry_at,15)

    def test_failed_close_never_reaches_prepared_creation(self):
        bot=self.bot();bot.hub.close_room.side_effect=HubInvocationUncertain('invented missing close reply')
        with patch('bot.bot.time.sleep'):
            with self.assertRaises(HubInvocationUncertain):bot.hard_restart_room(announce=False)
        self.assertEqual(bot.room_id,101);bot.create_room.assert_not_called()
        bot._reset_room_runtime_state_for_reopen.assert_not_called()

    def test_failed_creation_is_not_replayed_after_successful_close(self):
        bot=self.bot();bot.create_room.side_effect=RefereeCommandRejected(123)
        with patch('bot.bot.time.sleep'):
            with self.assertRaises(RefereeCommandRejected):bot.hard_restart_room(announce=False)
        bot.hub.close_room.assert_called_once_with(101);bot.create_room.assert_called_once()

    def test_automatic_and_manual_discord_indicator_behavior_is_retained(self):
        for reason,rename in [('automatic_hard_restart',False),('manual_hard_restart',True)]:
            with self.subTest(reason=reason):
                bot=self.bot()
                with patch('bot.bot.time.sleep'):self.assertTrue(bot.hard_restart_room(announce=False,close_reason=reason))
                value=bot._update_discord_public_status.call_args.kwargs
                self.assertFalse(value['is_open']);self.assertEqual(value['lifecycle'],'Restarting')
                self.assertEqual(value['rename_channel'],rename)

    def test_native_seed_skips_dt_and_convert_variants_before_destruction(self):
        bot=self.bot();dt=SimpleNamespace(beatmap_id=3,is_dt=True,is_mania_convert=False)
        converted=SimpleNamespace(beatmap_id=4,is_dt=False,is_mania_convert=True)
        bot.pool.next_map.side_effect=[dt,converted,bot.current_map]
        with patch('bot.bot.time.sleep'):self.assertTrue(bot.hard_restart_room(announce=False))
        bot._map_is_downloadable.assert_called_once_with(bot.current_map)
        bot.create_room.assert_called_once_with(initial_map=bot.current_map)

    def test_ordinary_queue_pick_still_reserves_current_and_queued_maps(self):
        bot=self.bot()
        self.assertIsNone(bot._next_downloadable_map(max_attempts=1,native_only=True))
        self.assertEqual(bot.pool.reserved,{1,2})
        bot.hub.close_room.assert_not_called()
