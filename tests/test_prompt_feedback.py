"""Prompt owner/community feedback uses canonical counts, never a local overlay."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.bot import LobbyBot
from bot.feedback import FeedbackReplies
from poolrotater_host.layout import read_json, atomic_json
from poolrotater_host.network.contracts import NetworkError
from poolrotater_host.ratings.agent import Scheduler, FetchScheduler
from poolrotater_host.ratings.delivery import DeliveryBudget
from tests.test_v71_ratings_parity import V71RatingsParityTests as Fixtures


class PromptFeedbackTests(unittest.TestCase):
    setUp = Fixtures.setUp
    host = Fixtures.host
    enable = Fixtures.enable
    download = Fixtures.download
    reader = Fixtures.reader

    def ready(self, host):
        self.enable(host)
        self.now = getattr(self, 'now', [1000.0])
        host['local'].reader = self.reader(host)
        host['schedule'] = Scheduler(host['sync'], clock=lambda:self.now[0], jitter=lambda *_:0)
        host['fetch'] = FetchScheduler(host['sync'], clock=lambda:self.now[0], jitter=lambda *_:0)
        host['schedule'].tick()
        with patch('poolrotater_host.ratings.reader.profile', return_value={'mode':'network'}):
            host['fetch'].tick()
        bot = LobbyBot.__new__(LobbyBot)
        bot.cfg = {'ruleset_id':0}; bot.prefix = '.'
        for name in ('_is_admin', '_is_moderator', '_is_project_banned'):
            setattr(bot, name, Mock(return_value=False))
        bot._allow_public_command_burst = Mock(return_value=True)
        bot.say = Mock(); bot.ratings = host['local']; bot._rating_window_until = float('inf')
        bot.last_played_map = SimpleNamespace(beatmap_id=1, title='Invented chart', map_origin='standard-nm')
        bot._feedback_replies = FeedbackReplies(bot.ratings, clock=lambda:self.now[0])
        return bot

    def command(self, bot, uid, text):
        with patch('bot.ratings.time.time', return_value=self.now[0]):
            bot.handle_command(uid, text)

    def sync(self, host):
        receipt = host['schedule'].tick()
        with patch('poolrotater_host.ratings.reader.profile', return_value={'mode':'network'}):
            fetched = host['fetch'].tick(uploaded_at=receipt['attempted_at'] if receipt['uploaded'] else None)
        return receipt, fetched

    def test_owner_and_community_have_familiar_current_counts_without_waiting_half_an_hour(self):
        bots = [self.ready(host) for host in (self.owner, self.community)]
        self.now[0] = 1001
        self.command(bots[0], 7, '.good'); self.command(bots[1], 8, '.b')
        for bot in bots:
            bot._drain_feedback_replies(); bot.say.assert_not_called()
        self.now[0] = 1005
        for host in (self.owner, self.community):
            receipt, fetched = self.sync(host)
            self.assertEqual((receipt['uploaded'], fetched['phase']), (1, 'activated'))
        # Refresh the owner's cache after the community's contribution too.
        self.download(self.owner)
        for bot in bots:
            bot._drain_feedback_replies()
            self.assertEqual(bot.say.call_args.args[0],
                'Thanks for the feedback! Invented chart — 1 good / 1 bad.')
            self.assertNotIn('sync', bot.say.call_args.args[0])
            self.assertEqual(bot._feedback_replies.pending, {})
            self.assertIsNotNone(bot.ratings.reader)

    def test_same_player_on_both_hosts_counts_once_then_changed_opinion_replaces_it(self):
        a = self.ready(self.owner); b = self.ready(self.community)
        self.now[0] = 1001
        for bot in (a,b): self.command(bot,7,'.g')
        self.now[0] = 1005
        for host in (self.owner,self.community):self.sync(host)
        self.download(self.owner)
        for bot in (a,b):
            bot._drain_feedback_replies()
            self.assertIn('1 good / 0 bad', bot.say.call_args.args[0])
        self.now[0] = 1006
        self.command(a,7,'.bad')
        self.now[0] = 1010
        self.sync(self.owner);a._drain_feedback_replies()
        self.assertIn('0 good / 1 bad',a.say.call_args.args[0])
        self.download(self.community)
        self.assertEqual(self.reader(self.community).tallies([1]),{1:(0,1)})

    def test_reply_retains_original_map_after_rotation(self):
        bot = self.ready(self.owner);self.now[0]=1001;self.command(bot,7,'.good')
        bot.last_played_map=SimpleNamespace(beatmap_id=2,title='Different next chart')
        self.now[0]=1005;self.sync(self.owner);bot._drain_feedback_replies()
        self.assertIn('Invented chart — 1 good / 0 bad',bot.say.call_args.args[0])

    def test_accepted_upload_without_including_snapshot_does_not_show_stale_zero(self):
        bot=self.ready(self.owner);self.now[0]=1001;self.command(bot,7,'.bad')
        self.now[0]=1005;self.owner['schedule'].tick();bot._drain_feedback_replies()
        bot.say.assert_not_called()
        self.download(self.owner);self.now[0]=1006;bot._drain_feedback_replies()
        self.assertIn('0 good / 1 bad',bot.say.call_args.args[0])

    def test_outage_keeps_vote_without_waiting_in_the_chat_handler_or_showing_wrong_counts(self):
        bot=self.ready(self.owner);self.now[0]=1001;self.command(bot,7,'.bad')
        self.owner['network'].offline=True;self.now[0]=1005
        receipt=self.owner['schedule'].tick();self.assertEqual(receipt['phase'],'retrying')
        bot._drain_feedback_replies();bot.say.assert_not_called()
        self.now[0]=1013;bot._drain_feedback_replies()
        self.assertEqual(bot.say.call_args.args[0],'Thanks for the feedback! Invented chart — noted as bad.')
        with sqlite3.connect(bot.ratings.path) as db:
            self.assertEqual(db.execute('SELECT phase FROM rating_outbox').fetchall(),[('pending',)])
        self.owner['network'].offline=False;self.now[0]=1015;self.sync(self.owner)
        self.assertEqual(self.reader(self.owner).tallies([1]),{1:(0,1)})

    def test_local_cache_or_ledger_failure_after_commit_cannot_kill_worker(self):
        bot=self.ready(self.owner);self.now[0]=1001;self.command(bot,7,'.good')
        self.now[0]=1013
        with patch.object(bot._feedback_replies,'_connect',side_effect=sqlite3.OperationalError('invented busy')):
            bot._drain_feedback_replies()
        self.assertIn('noted as good',bot.say.call_args.args[0])
        self.assertNotIn('0 good',bot.say.call_args.args[0])
        self.assertEqual(len(bot._feedback_replies.pending),0)

    def test_neutral_duplicates_and_filtered_players_preserve_native_policies(self):
        bot=self.ready(self.owner);self.now[0]=1001;self.command(bot,7,'.ok')
        self.assertIn('noted as neutral',bot.say.call_args.args[0])
        bot.say.reset_mock();self.command(bot,7,'.ok');bot.say.assert_not_called()
        bot.ratings.set_rating_ignored(user_id=8,username='Invented filtered',ignored=True)
        self.command(bot,8,'.b')
        self.assertEqual(bot._feedback_replies.pending,{})
        with sqlite3.connect(bot.ratings.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM rating_outbox').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM shadow_map_ratings').fetchone()[0],1)

    def test_quick_changed_vote_coalesces_and_queue_stays_bounded(self):
        bot=self.ready(self.owner);self.now[0]=1001
        self.command(bot,7,'.good');self.command(bot,7,'.bad')
        self.assertEqual(len(bot._feedback_replies.pending),1)
        self.now[0]=1005;self.sync(self.owner);bot._drain_feedback_replies()
        self.assertIn('0 good / 1 bad',bot.say.call_args.args[0])
        bot._feedback_replies.LIMIT=2
        self.now[0]=1006
        for uid in (8,9,10):self.command(bot,uid,'.good')
        self.assertEqual(len(bot._feedback_replies.pending),2)
        self.assertIn('noted as good',bot.say.call_args.args[0])

    def test_new_vote_does_not_override_authority_backoff_or_disabled_sharing(self):
        bot=self.ready(self.owner);self.now[0]=1001;self.command(bot,7,'.bad')
        self.owner['network'].offline=True;self.now[0]=1005;first=self.owner['schedule'].tick()
        self.now[0]=1006;self.command(bot,8,'.good')
        calls=len(self.owner['network'].requests)
        self.assertEqual(self.owner['schedule'].tick(),first)
        self.assertEqual(len(self.owner['network'].requests),calls)
        self.owner['sync'].configure(share_ratings=False)
        self.now[0]=1010;self.assertEqual(self.owner['schedule'].tick()['phase'],'sharing-disabled')
        self.now[0]=1011;self.command(bot,9,'.bad')
        calls=len(self.owner['network'].requests);self.now[0]=1015
        self.owner['schedule'].tick();self.assertEqual(len(self.owner['network'].requests),calls)


    def test_combined_agent_notices_a_live_vote_while_normal_deadlines_are_idle(self):
        from poolrotater_host.ratings.agent import run_combined
        bot=self.ready(self.owner)
        outer=self
        class Heartbeat:
            client=outer.owner['network']
            def clock(self):return outer.now[0]
            def step(self):return {'retry_seconds':60}
        class Stop:
            injected=False
            def is_set(self):return outer.now[0]>=1007
            def wait(self,seconds):
                if not self.injected:
                    outer.now[0]=1001;outer.command(bot,7,'.bad');self.injected=True
                outer.now[0]+=seconds
                bot._drain_feedback_replies()
        with patch('poolrotater_host.ratings.reader.profile',return_value={'mode':'network'}):
            run_combined(Heartbeat(),self.owner['schedule'],Stop(),snapshots=self.owner['fetch'])
        self.assertIn('0 good / 1 bad',bot.say.call_args.args[0])
        self.assertLess(self.now[0],1014)

    def test_conflict_is_not_disguised_as_an_extra_shared_vote(self):
        bot=self.ready(self.owner);other=self.ready(self.community)
        self.now[0]=1001;self.command(other,7,'.good')
        self.now[0]=1005;self.sync(self.community)
        self.now[0]=1006;self.command(bot,7,'.bad')
        self.now[0]=1010;self.sync(self.owner);bot._drain_feedback_replies()
        self.assertEqual(bot.say.call_args.args[0],'Thanks for the feedback! Invented chart — noted as bad.')
        self.assertEqual(self.reader(self.owner).tallies([1]),{1:(1,0)})

    def test_complete_prepared_standard_4k_and_7k_catalogs_keep_selection_and_feedback_in_agreement(self):
        from pathlib import Path
        from bot.standard_pool import StandardPool
        from bot.mania_pool import ManiaPool
        root=Path(__file__).resolve().parents[1]
        prepared=root/'validation/owner-pilot/conversion-catalog-qualification/prepared'
        bot=self.ready(self.owner)
        for index,(mode,keys,minimum) in enumerate((('standard',None,100000),('mania4',4,19000),('mania7',7,4000))):
            path=prepared/mode/'catalog.db'
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                table='beatmaps' if keys is None else 'maps'
                self.assertGreater(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],minimum)
            options=dict(sr_min=3,sr_max=6,max_length=240,min_passcount=1000,limit=200,
                         ratings_db=bot.ratings.path,ratings_reader=bot.ratings.reader)
            if keys is None:
                pool=StandardPool(str(path),standard_dt_db=str(prepared/mode/'dt.db'),**options)
            else:
                pool=ManiaPool(str(path),mania_keys=keys,mania_farm_enabled=True,
                               mania_farm_db=str(prepared/mode/'farm.db'),**options)
            self.assertTrue(pool.maps)
            bot.last_played_map=pool.maps[0]
            self.now[0]=1001+index*5;self.command(bot,700+index,'.bad')
            self.now[0]=1005+index*5;self.sync(self.owner);bot._drain_feedback_replies()
            self.assertIn('0 good / 1 bad',bot.say.call_args.args[0])
            bid=bot.last_played_map.beatmap_id
            self.assertEqual(pool._rating_tallies([bid]),{bid:(0,1)})


class DeliveryBudgetTests(unittest.TestCase):
    setUp=Fixtures.setUp
    host=Fixtures.host

    def budget(self,now):
        return DeliveryBudget(self.owner['root'],self.owner['sync'].marker,now)

    def events(self,start,count):
        import uuid
        return [{'event_id':str(uuid.UUID(int=index+1))} for index in range(start,start+count)]

    def test_restart_retry_refill_and_backwards_clock_preserve_budget(self):
        events=self.events(0,100)
        self.assertEqual(len(self.budget(1000).select(events)),100)
        self.assertEqual(self.budget(1001).select(events),events)
        self.assertEqual(self.budget(1002).select(self.events(100,1)),[])
        self.assertEqual(self.budget(999).select(self.events(100,1)),[])
        self.assertEqual(len(self.budget(1018).select(self.events(100,2))),1)
        self.assertEqual(len(self.budget(2800).select(self.events(101,100))),99)

    def test_fast_flood_stays_below_hub_rolling_day_cap_across_restarts(self):
        sent=0
        for now in range(1000,1000+86400,5):
            batch=self.events(sent,100)
            sent+=len(self.budget(now).select(batch))
        self.assertLessEqual(sent,4900)
        self.assertGreaterEqual(sent,4800)
        value=read_json(self.budget(now).path)
        self.assertLessEqual(len(value['reserved']),100)

    def test_foreign_or_corrupt_budget_refuses_without_reinitializing(self):
        budget=self.budget(1000);budget.select(self.events(0,1))
        value=read_json(budget.path);value['tokens']=101;atomic_json(budget.path,value)
        with self.assertRaises(NetworkError):self.budget(1010).select(self.events(1,1))
        self.assertEqual(read_json(budget.path),value)


if __name__=='__main__':unittest.main()
