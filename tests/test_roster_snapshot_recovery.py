"""Replay lost departure deltas with real broker, worker roster and vote logic."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from bot.hub import HubDisconnected
from rolling.adapters import Hub
import test_headless_multiroom as native
import test_rolling as rolling

class RosterRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = native.ProductionControllerTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.bot = fixture.bots[0]
        self.bot.say = Mock()
        self.bot.cfg['auto_difficulty'] = True
        self.bot.players = {uid: {'name': f'Invented-{uid}', 'pp': 1000, 'status': 'idle'}
                            for uid in range(10, 17)}
        self.bot.ready = {10, 11}
        self.bot._pending_player_profiles = {11: {'player': self.bot.players[11]}}
        self.bot._save_departing_preference = Mock()
        self.bot._flush_player_lobby_time = Mock()
        self.bot._close_if_armed_and_empty = Mock()
        self.bot._apply_passed_difficulty_vote = Mock()
        self.bot._complete_vote_skip = Mock()
        self.bot._defer_player_profile = Mock(wraps=self.bot._defer_player_profile)

    def snapshot(self, players=None):
        return {'room_id': self.bot.room_id, 'chat_channel_id': self.bot.channel_id,
                'players': [{'user_id': 10, 'status': 'ready'}] if players is None else players,
                'referees': [{'user_id': 1}]}

    def test_seven_cached_players_become_one_and_who_and_votes_use_same_roster(self):
        bot = self.bot; survivor = bot.players[10]
        bot._skip_vote_until = 99999999999; bot._skip_votes = {10, 11}
        bot._difficulty_vote_required = 4
        bot._difficulty_vote_harder = {10, 11}; bot._difficulty_vote_easier = {12}
        bot._sync_referee_roster(self.snapshot())
        self.assertEqual(set(bot.players), {10}); self.assertIs(bot.players[10], survivor)
        self.assertEqual(bot.ready, {10}); self.assertEqual(bot._pending_player_profiles, {})
        self.assertEqual(bot._skip_votes, {10}); self.assertEqual(bot._skip_vote_required, 1)
        self.assertEqual(bot._difficulty_vote_harder, {10}); self.assertEqual(bot._difficulty_vote_required, 1)
        self.assertEqual(bot._flush_player_lobby_time.call_count, 6)
        bot._complete_vote_skip.assert_called()  # real quorum drops to the one remaining player
        self.assertEqual(bot._apply_passed_difficulty_vote.call_args.args[2:], (1, 1))
        bot._discord_command_reply_channel_id = 999
        bot._is_moderator = Mock(return_value=True)
        bot._is_admin = Mock(return_value=True)
        bot.handle_command(-1, '.who')
        reply = bot.say.call_args.args[0]
        self.assertIn('1 player', reply); self.assertIn('Invented-10', reply)
        self.assertNotIn('Invented-11', reply)

    def test_entire_replacement_lands_before_quorum_recalculation(self):
        bot = self.bot; bot._skip_vote_until = 99999999999; bot._skip_votes = {10}
        members = [{'user_id': uid, 'status': 'idle'} for uid in (10, 20, 21, 22, 23, 24, 25)]
        bot._sync_referee_roster(self.snapshot(members))
        self.assertEqual(set(bot.players), {10, 20, 21, 22, 23, 24, 25})
        self.assertEqual(bot._skip_vote_required, 4); bot._complete_vote_skip.assert_not_called()
        self.assertEqual(bot._defer_player_profile.call_count, 6)
        self.assertEqual(bot.ready, set())

    def test_incomplete_wrong_room_and_duplicate_snapshots_never_imply_empty(self):
        cases = [None, {}, {'players': []}, self.snapshot()]
        cases[-1]['room_id'] += 1
        for field, value in [('players', None), ('referees', None), ('players', [{'user_id': 10}]),
                             ('players', [{'user_id': True, 'status': 'idle'}]),
                             ('players', [{'user_id': 10, 'status': 'idle'}] * 2)]:
            snapshot = self.snapshot(); snapshot[field] = value; cases.append(snapshot)
        for snapshot in cases:
            with self.subTest(snapshot=snapshot):
                before = copy.deepcopy(self.bot.players)
                self.assertFalse(self.bot._sync_referee_roster(snapshot))
                self.assertEqual(self.bot.players, before)
        self.bot._close_if_armed_and_empty.assert_not_called()

    def test_spectators_and_referee_are_excluded_from_ready_and_voting(self):
        self.bot._sync_referee_roster(self.snapshot([
            {'user_id': 1, 'status': 'ready'}, {'user_id': 10, 'status': 'idle'},
            {'user_id': 20, 'status': 'spectating'}]))
        self.assertEqual(set(self.bot.players), {10, 20})
        self.assertEqual(self.bot.skip_voter_ids(), {10}); self.assertEqual(self.bot.ready, set())

    def test_duplicate_snapshot_keeps_survivor_and_does_not_repeat_join_or_departure(self):
        snapshot = self.snapshot(); self.bot._sync_referee_roster(snapshot)
        self.bot._flush_player_lobby_time.reset_mock(); self.bot._defer_player_profile.reset_mock()
        survivor = self.bot.players[10]
        self.bot._sync_referee_roster(snapshot)
        self.assertIs(self.bot.players[10], survivor)
        self.bot._flush_player_lobby_time.assert_not_called(); self.bot._defer_player_profile.assert_not_called()

    def test_delta_after_barrier_wins_and_late_profile_cannot_restore_departed_user(self):
        bot = self.bot
        bot.events.put(('RoomRosterSnapshot', self.snapshot()))
        bot.events.put(('UserLeft', {'room_id': bot.room_id, 'user_id': 10}))
        bot._drain_hub_events()
        self.assertEqual(bot.players, {}); self.assertEqual(bot.ready, set())
        bot.cfg['background_player_profiles'] = True
        result = queue.Queue(); result.put(({'id': 11, 'username': 'Late'}, [], None))
        bot._background_profile_job = {'uid': 11, 'work': {}, 'result': result}
        bot._flush_background_player_profiles()
        self.assertEqual(bot.players, {})

    def test_recovery_preserves_active_match_and_clears_ready_download_holds(self):
        bot = self.bot; bot.state = 'PLAYING'
        bot.playing_player_ids = {10, 11}; bot.playing_item_id = 888
        bot._single_player_start_hold_uid = 10; bot._first_player_download_wait_uid = 10
        bot._sync_referee_roster(self.snapshot())
        self.assertEqual(bot.state, 'PLAYING'); self.assertEqual(bot.playing_item_id, 888)
        self.assertEqual(bot.playing_player_ids, {10, 11})  # actual match history stays frozen
        self.assertIsNone(bot._single_player_start_hold_uid)
        self.assertIsNone(bot._first_player_download_wait_uid)

    def test_reply_before_outbox_delivery_waits_for_its_barrier_and_later_delta(self):
        bot = self.bot; snapshot = self.snapshot(); snapshot['_worker_roster_barrier'] = 5
        def deliver():
            bot.events.put(('RoomRosterSnapshot', snapshot))
            bot.events.put(('UserStatusChanged', {'room_id': bot.room_id, 'user_id': 10, 'status': 'idle'}))
        timer = threading.Timer(0.03, deliver); timer.start(); self.addCleanup(timer.join)
        import time
        bot._await_referee_roster({'_worker_roster_barrier': 5}, time.monotonic() + 5)
        self.assertEqual(set(bot.players), {10}); self.assertEqual(bot.ready, set())
        self.assertEqual(bot._last_referee_roster_barrier, 5)

    def test_missing_barrier_stays_required_even_when_retry_uses_cached_join(self):
        bot = self.bot
        for joined in ({'_worker_roster_barrier': 5}, {}):
            tick = iter(range(100, 200, 2))
            with patch('bot.bot.time.monotonic', side_effect=lambda: next(tick)), patch.object(bot.events, 'get', side_effect=queue.Empty):
                with self.assertRaisesRegex(HubDisconnected, 'ordered referee roster'):
                    bot._await_referee_roster(joined, 200)
            self.assertEqual(bot._required_referee_roster_barrier, 5)
            self.assertEqual(len(bot.players), 7)

    def test_room_replacement_drops_delivery_requirement_from_retired_room(self):
        bot = self.bot
        bot._required_referee_roster_barrier = 9; bot._last_referee_roster_barrier = 8
        bot._reset_room_runtime_state_for_reopen()
        self.assertEqual(bot._required_referee_roster_barrier, 0)
        self.assertEqual(bot._last_referee_roster_barrier, 0)

    def test_fresh_broker_join_repairs_real_worker_before_recovery_finishes(self):
        fixture = rolling.BrokerTests(); fixture.setUp(); broker = fixture.b
        bot = self.bot
        self.assertEqual(bot.room_id, fixture.rid)
        fixture.hub.rooms[fixture.rid]['players'] = [{'user_id': 10, 'status': 'ready'}]
        fixture.hub._connect_generation += 1
        bot.hub = Hub(SimpleNamespace(call=broker.handle, closed=threading.Event()))
        bot.api.room_probe.return_value = ('ok', {})
        bot._join_room_chat = Mock(return_value=True)
        bot.reconcile_playlist = Mock(side_effect=lambda **_: self.assertEqual(set(bot.players), {10}))
        # Real RPC event queue receives preceding deltas, barrier, following deltas.
        original = broker.enqueue
        def enqueue(name, value):
            original(name, value)
            if name == 'hub': bot.events.put(value)
        broker.enqueue = enqueue
        bot._recover_hub_transport('invented disconnect with missed departures')
        self.assertEqual(broker.players, {10}); self.assertEqual(set(bot.players), {10})
        self.assertFalse(bot._hub_rejoin_pending)
        bot.reconcile_playlist.assert_called_once()

class BrokerRosterBarrierTests(unittest.TestCase):
    def setUp(self):
        fixture = rolling.BrokerTests(); fixture.setUp()
        self.fixture, self.broker = fixture, fixture.b

    def test_only_fresh_join_emits_barrier_without_password_and_preserves_event_order(self):
        f, b = self.fixture, self.broker
        f.owner.shared.route('UserJoined', {'room_id': f.rid, 'user_id': 12})
        f.hub.rooms[f.rid]['players'] = [{'user_id': 10, 'status': 'idle'}]
        f.hub._connect_generation += 1
        b.handle('hub.invoke', 'JoinRoom', [f.rid])
        f.owner.shared.route('UserLeft', {'room_id': f.rid, 'user_id': 10})
        events = []
        while not b.outbox.empty(): events.append(b.outbox.get_nowait())
        self.assertEqual([v[1][0] for v in events], ['UserJoined', 'RoomRosterSnapshot', 'UserLeft'])
        self.assertNotIn('password', events[1][1][1])
        b.handle('hub.invoke', 'JoinRoom', [f.rid])
        self.assertTrue(b.outbox.empty())  # cached creation/join snapshot is stale after deltas
        self.assertEqual(b.players, set())

    def test_failed_fresh_join_never_emits_roster_barrier(self):
        f, b = self.fixture, self.broker; f.hub._connect_generation += 1
        with patch.object(f.hub, 'invoke', side_effect=HubDisconnected('invented failure')):
            with self.assertRaises(HubDisconnected): b.handle('hub.invoke', 'JoinRoom', [f.rid])
        self.assertTrue(b.outbox.empty())
