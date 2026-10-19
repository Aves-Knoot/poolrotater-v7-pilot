"""Typed pre-dispatch profile deferral preserves native occupancy and readiness."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import socket,unittest
from unittest.mock import Mock,patch
from bot.api import ApiAdmissionDeferred
from bot.bot import LobbyBot
from bot.hub import HubInvocationUncertain
from poolrotater_host.provider_pacing import ProviderAdmissionDeferred
from rolling.adapters import Api
from rolling.protocol import Peer,RemoteError,UncertainResult

PROFILE={'id':7,'username':'Invented','statistics':{'pp':1234},'country_code':'PL'}
TOP=[{'pp':100,'beatmap':{'id':22}}]

class DeferredProfileTests(unittest.TestCase):
    def bot(self):
        bot=object.__new__(LobbyBot);bot.cfg={'ruleset_id':3};bot.room_id=44;bot.channel_id=55
        bot.state='IDLE';bot.players={};bot.api=Mock();bot.non_gameplay_user_ids=Mock(return_value=set())
        bot._is_project_banned=Mock(return_value=False);bot._record_lobby_join=Mock();bot.mark_auto_retune_pending=Mock()
        bot.api.user.return_value=PROFILE;bot.api.user_top_scores.return_value=TOP
        return bot
    def defer(self,bot):
        bot.api.user.side_effect=ApiAdmissionDeferred('provider_pacing_deferred')
        with patch('bot.bot.time.monotonic',return_value=100):bot.on_user_joined(7)
    def test_join_visible_immediately_and_retries_enrich_without_duplicate_join_or_status_reset(self):
        bot=self.bot();self.defer(bot)
        self.assertEqual(bot.players[7]['pp'],None);self.assertEqual(bot.players[7]['name'],'7')
        bot._record_lobby_join.assert_called_once();bot.mark_auto_retune_pending.assert_not_called()
        bot.players[7]['status']='ready';bot.players[7]['lobby_started_at']=90
        with patch('bot.bot.time.monotonic',return_value=100.5):bot._flush_player_profiles()
        self.assertEqual(bot.api.user.call_count,1)
        bot.api.user.side_effect=None
        with patch('bot.bot.time.monotonic',return_value=102):bot._flush_player_profiles()
        self.assertEqual(bot.players[7]['status'],'ready');self.assertEqual(bot.players[7]['lobby_started_at'],90)
        self.assertEqual(bot.players[7]['pp'],1234);self.assertEqual(bot.players[7]['top'],[(100,22)])
        bot._record_lobby_join.assert_called_once();bot.mark_auto_retune_pending.assert_called_once()
        self.assertEqual(bot._pending_player_profiles,{})
    def test_referee_roster_placeholder_precedes_profile_request_and_deferral_retains_ready(self):
        bot=self.bot()
        def refusing(uid,**kwargs):
            self.assertIn(uid,bot.players);self.assertIsNone(bot.players[uid]['pp'])
            bot.players[uid]['status']='ready'
            raise ApiAdmissionDeferred('deferred')
        bot.api.user.side_effect=refusing
        with patch('bot.bot.time.monotonic',return_value=100):bot.on_user_joined(7)
        self.assertEqual(bot.players[7]['status'],'ready');bot._record_lobby_join.assert_called_once()
        bot.on_user_joined(7);self.assertEqual(bot.api.user.call_count,1)

    def test_partial_user_success_top_defers_then_reuses_user_and_keeps_playing_status(self):
        bot=self.bot();bot.api.user_top_scores.side_effect=ApiAdmissionDeferred('provider_pacing_deferred')
        with patch('bot.bot.time.monotonic',return_value=100):bot.on_user_joined(7)
        self.assertEqual(bot.players[7]['pp'],1234);self.assertNotIn('top',bot.players[7])
        bot.players[7]['status']='playing';bot.api.user_top_scores.side_effect=None
        with patch('bot.bot.time.monotonic',return_value=102):bot._flush_player_profiles()
        self.assertEqual(bot.api.user.call_count,1);self.assertEqual(bot.players[7]['status'],'playing')
        bot._record_lobby_join.assert_called_once()
    def test_unknown_missing_statistics_never_become_inferred_zero_and_invalid_profile_retries(self):
        bot=self.bot();self.defer(bot);bot.api.user.side_effect=None
        bot.api.user.return_value={'id':7,'username':'Unknown'}
        with patch('bot.bot.time.monotonic',return_value=102):bot._flush_player_profiles()
        self.assertIsNone(bot.players[7]['pp']);bot.mark_auto_retune_pending.assert_not_called()
        other=self.bot();self.defer(other);other.api.user.side_effect=None;other.api.user.return_value={'id':99,'username':'Wrong','statistics':{'pp':123}}
        with patch('bot.bot.time.monotonic',return_value=102):other._flush_player_profiles()
        self.assertIsNone(other.players[7]['pp']);other.api.user_top_scores.assert_not_called()
    def test_leave_rejoin_room_channel_change_and_ttl_discard_without_dispatch(self):
        for mutation in ('leave','rejoin','room','channel','expired'):
            bot=self.bot();self.defer(bot)
            if mutation=='leave':bot.players.pop(7)
            if mutation=='rejoin':bot.players[7]=dict(bot.players[7])
            if mutation=='room':bot.room_id=99
            if mutation=='channel':bot.channel_id=99
            with patch('bot.bot.time.monotonic',return_value=401 if mutation=='expired' else 102):bot._flush_player_profiles()
            self.assertEqual(bot.api.user.call_count,1);self.assertEqual(bot._pending_player_profiles,{})
    def test_queue_limit_fairness_one_profile_per_loop_and_bounded_backoff(self):
        bot=self.bot();bot.api.user.side_effect=ApiAdmissionDeferred('provider_pacing_deferred')
        with patch('bot.bot.time.monotonic',return_value=100):
            for uid in range(1,35):bot.on_user_joined(uid)
        self.assertEqual(len(bot.players),34);self.assertEqual(len(bot._pending_player_profiles),32)
        before=bot.api.user.call_count
        with patch('bot.bot.time.monotonic',return_value=102):bot._flush_player_profiles()
        self.assertEqual(bot.api.user.call_count,before+1)
        self.assertEqual(list(bot._pending_player_profiles)[-1],1)
        delays=[];bot._pending_player_profiles={1:bot._pending_player_profiles[1]}
        for now in range(120,200,20):
            with patch('bot.bot.time.monotonic',return_value=now):bot._flush_player_profiles()
            delays.append(bot._pending_player_profiles[1]['retry_at']-now)
        self.assertEqual(delays,[4,8,15,15])
    def test_normal_join_preserves_previous_top100_and_statistics_behavior(self):
        bot=self.bot();bot.on_user_joined(7)
        self.assertEqual(bot.players[7]['pp'],1234);self.assertEqual(bot.players[7]['status'],'idle')
        self.assertEqual(bot.players[7]['top'],[(100,22)]);self.assertEqual(bot.players[7]['threshold'],0)
        bot.api.user.assert_called_once_with(7,mode='mania')
        bot.api.user_top_scores.assert_called_once_with(7,limit=100,mode='mania')
        bot._record_lobby_join.assert_called_once();bot.mark_auto_retune_pending.assert_called_once()
    def test_non_gameplay_and_bans_preserve_existing_early_authority_checks(self):
        bot=self.bot();bot.non_gameplay_user_ids.return_value={7};bot.on_user_joined(7)
        bot.api.user.assert_not_called();self.assertEqual(bot.players,{})
        bot=self.bot();bot._is_project_banned.return_value=True;bot._ban_store=Mock(return_value=Mock(bans={7:{'username':'Banned'}}));bot._enforce_project_bans=Mock()
        bot.on_user_joined(7);bot.api.user.assert_not_called();bot._enforce_project_bans.assert_called_once()
    def test_generic_auth_and_uncertain_errors_propagate_at_join_and_flush(self):
        for error in (RemoteError('AuthError','OAuth: provider_pacing_deferred'),HubInvocationUncertain('unknown')):
            bot=self.bot();bot.api.user.side_effect=error
            with self.assertRaises(type(error)):bot.on_user_joined(7)
            bot=self.bot();self.defer(bot);bot.api.user.side_effect=error
            with patch('bot.bot.time.monotonic',return_value=102),self.assertRaises(type(error)):bot._flush_player_profiles()
    def test_score_report_deferred_retains_scoring_target_and_cursors(self):
        bot=self.bot();bot._match_end_at=90;bot.scoring_item_id=77;bot.api.room_item_scores.side_effect=ApiAdmissionDeferred('deferred')
        with patch('bot.bot.time.time',return_value=100),patch('bot.bot.time.monotonic',return_value=100):self.assertFalse(bot.try_score_report())
        self.assertEqual(bot.scoring_item_id,77);self.assertEqual(bot._match_end_at,90)
        for error in (RemoteError('AuthError','invalid'),HubInvocationUncertain('unknown')):
            bot.api.room_item_scores.side_effect=error
            with patch('bot.bot.time.time',return_value=102),patch('bot.bot.time.monotonic',return_value=102),self.assertRaises(type(error)):bot.try_score_report()
    def test_real_rpc_only_typed_profile_and_score_no_dispatch_refusals_are_converted(self):
        a,b=socket.socketpair()
        def refuse(*args,**kwargs):raise ProviderAdmissionDeferred('provider_pacing_deferred')
        server=Peer(a,handler=refuse).start();client=Peer(b).start();self.addCleanup(server.close);self.addCleanup(client.close)
        for method,args in [('user',(7,)),('user_top_scores',(7,)),('room_item_scores',(44,77))]:
            with self.assertRaises(ApiAdmissionDeferred):getattr(Api(client),method)(*args)
        with self.assertRaises(RemoteError):Api(client).beatmap(7)
        peer=Mock();peer.call.side_effect=UncertainResult('unknown')
        with self.assertRaises(HubInvocationUncertain):Api(peer).user(7)
