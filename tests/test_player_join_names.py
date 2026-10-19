"""Names precede optional scores without stale or numeric greetings."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import queue,threading,unittest
from unittest.mock import Mock,patch
import test_headless_multiroom as native

class JoinNameTests(unittest.TestCase):
    def setUp(self):
        fixture=native.ProductionControllerTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.bot=b=fixture.bots[0];b.players={};b.cfg['background_player_profiles']=True
        b.pp_stats.username_for_user_id=Mock(return_value=None);b.say=Mock()
        b._record_lobby_join=Mock();b._send_custom_join_greeting=Mock(return_value=False)
        b._first_player_welcome=Mock(side_effect=lambda uid,name,recalled:f'Welcome, {name}')
        b.api=Mock();b.api.user.return_value={'id':7,'username':'ActualName','statistics':{'pp':123}}
        b.api.user_top_scores.return_value=[]
    def join(self):
        b=self.bot
        with patch('bot.bot.time.monotonic',return_value=100):b.on_user_joined(7)
        b.players[7]['_pending_join_welcome']=None
    def flush(self):
        with patch('bot.bot.time.monotonic',return_value=102):self.bot._flush_player_profiles()
    def test_local_name_beats_placeholder_without_network(self):
        b=self.bot;b.players[7]={'name':'7'};b.pp_stats.username_for_user_id.return_value='CachedName'
        self.assertEqual(b._known_username(7),'CachedName');b.api.user.assert_not_called()
        b.pp_stats.username_for_user_id.return_value='7';self.assertIsNone(b._known_username(7))
        b.players[7].update(username_resolved=True);self.assertEqual(b._known_username(7),'7')
    def test_name_available_while_top_scores_still_blocked_and_ready_unchanged(self):
        b=self.bot;entered=threading.Event();release=threading.Event()
        def scores(*a,**k):entered.set();release.wait(3);return []
        b.api.user_top_scores.side_effect=scores;self.addCleanup(release.set)
        self.join();b.players[7]['status']='ready';self.flush();self.assertTrue(entered.wait(2))
        self.flush();self.assertEqual(b.players[7]['name'],'ActualName');self.assertEqual(b.players[7]['status'],'ready')
        b.say.assert_called_once_with('Welcome, ActualName',automatic=True)
        thread=b._background_profile_job['thread'];self.assertTrue(thread.is_alive())
        release.set();thread.join(2);self.flush();b.say.assert_called_once();self.assertEqual(b._pending_player_profiles,{})
    def test_unknown_name_never_becomes_numeric_mid_match_greeting(self):
        b=self.bot;b.state='PLAYING';b.players[7]={'name':'7'}
        self.assertFalse(b._send_mid_match_join_intro(7));b.say.assert_not_called()
    def test_late_profile_after_departure_rejoin_or_room_change_cannot_greet(self):
        for change in ('left','rejoined','room'):
            with self.subTest(change=change):
                self.join();b=self.bot;work=b._pending_player_profiles[7];q=queue.Queue();q.put(('profile',b.api.user.return_value,None,None))
                b._background_profile_job={'uid':7,'work':work,'result':q}
                if change=='left':b.players.pop(7)
                elif change=='rejoined':b.players[7]=dict(b.players[7])
                else:
                    old_room=b.room_id;b.room_id+=1
                self.flush();b.say.assert_not_called();self.assertEqual(b._pending_player_profiles,{})
                b._background_profile_job=None;b.players={}
                if change=='room':b.room_id=old_room
    def test_bad_identity_cannot_supply_name_or_trigger_greeting(self):
        self.join();b=self.bot;work=b._pending_player_profiles[7];q=queue.Queue();q.put(('profile',{'id':8,'username':'Wrong'},None,None))
        b._background_profile_job={'uid':7,'work':work,'result':q};self.flush()
        self.assertEqual(b.players[7]['name'],'7');b.say.assert_not_called()
    def test_no_delayed_ready_instruction_after_match_begins(self):
        self.join();b=self.bot;b.state='PLAYING';b.players[7]['name']='ActualName'
        b._send_mid_match_join_intro=Mock(return_value=True);b._send_resolved_join_greetings(7)
        b._first_player_welcome.assert_not_called();b._send_mid_match_join_intro.assert_not_called()
        self.assertNotIn('_pending_join_welcome',b.players[7])

    def test_real_join_event_defers_welcome_but_immediately_keeps_ready_gate(self):
        b=self.bot;b._notify_discord_lobby_event=Mock();b._restore_solo_difficulty=Mock(return_value=None)
        with patch('bot.bot.time.monotonic',return_value=100):b.handle_event('UserJoined',{'user_id':7})
        self.assertEqual(b._first_player_download_wait_uid,7);b.say.assert_not_called()
        self.assertIn('_pending_join_welcome',b.players[7]);b.api.user.assert_not_called()
        self.flush();b._background_profile_job['thread'].join(2);self.flush();self.flush()
        b.say.assert_called_once_with('Welcome, ActualName',automatic=True)

    def test_mid_match_greeting_waits_for_name_and_is_sent_once(self):
        self.join();b=self.bot;b.players[7].pop('_pending_join_welcome');b.state='PLAYING'
        self.assertFalse(b._send_mid_match_join_intro(7));b.say.assert_not_called()
        b._match_remaining_seconds=Mock(return_value=20)
        self.flush();b._background_profile_job['thread'].join(2);self.flush();self.flush()
        b.say.assert_called_once();self.assertIn('ActualName',b.say.call_args.args[0])
        self.assertIn("you're in next",b.say.call_args.args[0]);self.assertNotIn('_pending_mid_match_intro',b.players[7])
    def test_custom_name_greeting_is_not_duplicated_by_partial_and_complete_results(self):
        from bot.bot import LobbyBot
        b=self.bot;b._send_custom_join_greeting=LobbyBot._send_custom_join_greeting.__get__(b)
        b.cfg['custom_join_greetings']={'ActualName':'Hello {name}'};b.cfg['custom_join_greeting_cooldown_seconds']=0
        self.join();self.flush();b._background_profile_job['thread'].join(2);self.flush();self.flush()
        b.say.assert_called_once_with('Hello ActualName',automatic=True)
