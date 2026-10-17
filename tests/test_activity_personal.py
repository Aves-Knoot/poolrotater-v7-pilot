"""Real tracked PP evidence, reviewed history and nonblocking personal-stat cache."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
import uuid
from bot.ppstats import PPStatsStore
from poolrotater_host.activity.query import player, union_seconds
from poolrotater_host.activity.reader import Reader
from poolrotater_host.activity.contracts import event
from poolrotater_host.network.contracts import NetworkError
import tests.test_activity_collector as collector_tests
import tests.test_activity_repository as repository_tests


class ActivityPersonalCollectorTests(unittest.TestCase):
    setUp=collector_tests.ActivityCollectorTests.setUp
    consent=collector_tests.ActivityCollectorTests.consent
    public=collector_tests.ActivityCollectorTests.public
    events=collector_tests.ActivityCollectorTests.events
    ack=collector_tests.ActivityCollectorTests.ack
    def seed_pp(self):
        with sqlite3.connect(self.ledger) as db:
            db.execute('CREATE TABLE autohost_pp_achievements(achievement_key TEXT PRIMARY KEY,user_id INTEGER,username TEXT,beatmap_id INTEGER,room_id INTEGER,playlist_item_id INTEGER,score_pp REAL,profile_gain_pp REAL,top_rank INTEGER,accuracy REAL,created_at REAL)')
            db.execute("INSERT INTO autohost_pp_achievements VALUES('room:1:item:10:user:2',2,'Invented',5,1,10,123,4.5,20,98,110)")

    def test_existing_tracked_gains_are_preserved_and_accuracy_percent_valid(self):
        self.public();self.consent();self.seed_pp();self.collector.collect()
        pp=[e for e in self.events() if e['kind']=='achievement'];self.assertEqual(len(pp),1)
        self.assertEqual(pp[0]['payload']['profile_gain_pp'],4.5);self.assertEqual(pp[0]['payload']['accuracy'],98)
        self.ack();self.collector.collect();self.collector.collect();self.assertEqual(self.events(),[])

    def test_historical_private_unknown_rows_require_explicit_review(self):
        self.consent();self.seed_pp()
        with sqlite3.connect(self.ledger) as db:db.execute('UPDATE autohost_pp_achievements SET created_at=50')
        self.collector.collect();self.assertEqual(self.events(),[])
        with self.assertRaisesRegex(NetworkError,'review_required'):
            self.collector.import_history(public_room_ids=[1])
        self.collector.import_history(public_room_ids=[],public_history_confirmed=True);self.assertEqual(self.events(),[])
        self.collector.import_history(public_room_ids=[1],public_history_confirmed=True)
        # Bounded scan cycles; a reviewed different list can need the next pass.
        self.collector.import_history(public_room_ids=[1],public_history_confirmed=True)
        self.assertEqual(len(self.events()),1);self.assertTrue(self.events()[0]['payload']['historical'])

    def test_reviewed_cumulative_snapshot_is_frozen_replay_safe_and_has_persisted_cutoff(self):
        self.consent()
        with sqlite3.connect(self.ledger) as db:
            db.execute('CREATE TABLE autohost_lobby_time(user_id INTEGER PRIMARY KEY,username TEXT,total_seconds REAL,visit_count INTEGER,first_join_at REAL,last_join_at REAL,last_leave_at REAL,updated_at REAL)')
            db.execute("INSERT INTO autohost_lobby_time VALUES(2,'Invented',50,3,20,80,90,90)")
        self.collector.import_history(public_room_ids=[1],public_history_confirmed=True,include_lobby_totals=True)
        first=self.events();self.assertEqual(len(first),1);self.assertEqual(first[0]['payload']['total_seconds'],50)
        self.assertEqual(first[0]['payload']['cutoff'],90)
        self.now=120
        with sqlite3.connect(self.ledger) as db:db.execute('UPDATE autohost_lobby_time SET total_seconds=70')
        self.collector.import_history(public_room_ids=[1],public_history_confirmed=True,include_lobby_totals=True)
        self.assertEqual(self.events(),first)


    def test_reviewed_history_larger_than_queue_freezes_then_drains_without_lost_users(self):
        from unittest.mock import patch
        self.consent()
        with sqlite3.connect(self.ledger) as db:
            db.execute('CREATE TABLE autohost_lobby_time(user_id INTEGER PRIMARY KEY,username TEXT,total_seconds REAL,visit_count INTEGER,first_join_at REAL,last_join_at REAL,last_leave_at REAL,updated_at REAL)')
            db.executemany("INSERT INTO autohost_lobby_time VALUES(?,'Invented',50,3,20,80,90,90)",[(uid,) for uid in range(2,6)])
        with patch('poolrotater_host.activity.collector.MAX_OUTBOX_EVENTS',2):
            first=self.collector.import_history(public_room_ids=[1],public_history_confirmed=True,include_lobby_totals=True)
            self.assertTrue(first['history_capture_paused']);self.assertEqual(len(self.events()),2)
            self.ack()
            with sqlite3.connect(self.ledger) as db:db.execute('UPDATE autohost_lobby_time SET total_seconds=999')
            second=self.collector.import_history(public_room_ids=[1],public_history_confirmed=True,include_lobby_totals=True)
            self.assertFalse(second['history_capture_paused']);self.assertEqual(len(self.events()),2)
            self.assertEqual({e['payload']['user_id'] for e in self.events()},{4,5})
            self.assertTrue(all(e['payload']['total_seconds']==50 for e in self.events()))


class ActivityPersonalRepositoryTests(unittest.TestCase):
    setUp=repository_tests.ActivityRepositoryTests.setUp
    enroll=repository_tests.ActivityRepositoryTests.enroll
    enable=repository_tests.ActivityRepositoryTests.enable
    event=repository_tests.ActivityRepositoryTests.event
    ingest=repository_tests.ActivityRepositoryTests.ingest
    def achievement(self,**changes):
        p={'room_id':1,'mode':'standard','user_id':2,'username':'Invented','achievement_key':'room:1:item:10:user:2',
           'beatmap_id':5,'playlist_item_id':10,'created_at':950,'profile_gain_pp':4.5,'score_pp':123,'accuracy':98,'top_rank':20,'historical':False}
        p.update(changes);item=self.event();item.update(kind='achievement',payload=p);event(item);return item

    def test_personal_query_deduplicates_same_gain_across_hosts_and_overlapping_sessions(self):
        self.enable();pp=self.achievement();self.ingest(pp,self.event(started_at=910,ended_at=930))
        token,binding,host=self.enroll(456);self.repo.consent(token,self.policy)
        duplicate=self.achievement();duplicate.update(installation_id=binding['installation_id'],generation=binding['generation'])
        presence=self.event(started_at=920,ended_at=940);presence.update(installation_id=binding['installation_id'],generation=binding['generation'])
        self.repo.ingest(token,{'protocol_version':1,'events':[duplicate,presence]})
        reply=self.repo.query(self.token,{'protocol_version':1,'user_ids':[2],'usernames':[],'ruleset_id':0})
        row=reply['players'][0];self.assertEqual(row['total_gain'],4.5);self.assertEqual(row['achievement_count'],1)
        self.assertEqual(row['total_lobby_seconds'],30);self.assertEqual(row['requester_host_totals']['total_lobby_seconds'],20)
        self.assertEqual(len(self.repo.snapshot()['achievements']),2)

    def test_casefold_name_index_handles_non_ascii_and_rejects_disabled_sources(self):
        self.enable();self.ingest(self.achievement(username='ŻÓŁĆ Player'))
        request={'protocol_version':1,'user_ids':[],'usernames':['żółć player'],'ruleset_id':0}
        self.assertEqual(self.repo.query(self.token,request)['players'][0]['user_id'],2)
        self.repo.consent(self.token,{**self.policy,'enabled':False,'consent_id':str(uuid.uuid4()),'consent_at':960})
        self.assertEqual(self.repo.query(self.token,request)['players'],[])

    def test_personal_mode_and_username_queries_preserve_hidden_privacy(self):
        self.enable();self.ingest(self.achievement(mode='mania4'))
        request={'protocol_version':1,'user_ids':[],'usernames':['invented'],'ruleset_id':3}
        self.assertEqual(self.repo.query(self.token,request)['players'][0]['total_gain'],4.5)
        request['ruleset_id']=0;self.assertEqual(self.repo.query(self.token,request)['players'][0]['total_gain'],0)
        self.ingest(self.event('privacy'))
        self.assertEqual(self.repo.query(self.token,request)['players'],[])
        request['user_ids']=[2];self.assertEqual(self.repo.query(self.token,request)['players'],[{'user_id':2,'hidden':True}])

    def test_baseline_snapshot_replaces_its_source_and_clips_future_time(self):
        self.enable();p={'source_id':str(uuid.uuid4()),'mode':'standard','user_id':2,'username':'Invented','total_seconds':100,'visit_count':5,'cutoff':915,'first_join_at':50,'last_join_at':910,'last_leave_at':915}
        item=self.event();item.update(kind='baseline',payload=p)
        self.ingest(item,self.event(started_at=910,ended_at=925))
        stats=player(self.repo.snapshot(),2);self.assertEqual(stats['total_lobby_seconds'],110)
        self.assertTrue(stats['coverage']['historical_time_overlaps_unknown'])
        newer={**item,'event_id':str(uuid.uuid4()),'payload':{**p,'total_seconds':110,'cutoff':925}}
        self.ingest(newer);self.assertEqual(player(self.repo.snapshot(),2)['total_lobby_seconds'],110)


class ActivityReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);(self.root/'state').mkdir()
        self.now=1000;self.reader=Reader(self.root,clock=lambda:self.now)
        self.remote=ActivityPersonalRepositoryTests('test_baseline_snapshot_replaces_its_source_and_clips_future_time');self.remote.setUp();self.addCleanup(self.remote.doCleanups)
        self.remote.enable();self.remote.ingest(self.remote.achievement())
        outer=self
        class Network:
            def profile(self):return {}
            def credential(self):return {'credential':outer.remote.token}
            def transport(self,profile):return self
            def request(self,method,path,token,body):
                outer.assertEqual(path,'/v1/activity/query')
                return outer.remote.repo.query(token,body)
        self.network=Network()

    def test_commands_only_read_sqlite_and_refresh_uses_agent_queue(self):
        self.assertIsNone(self.reader.user_stats(2));self.assertIsNone(self.reader.lookup('Invented'))
        self.assertGreater(self.reader.status()['pending_queries'],0)
        self.reader.refresh(self.network);stats=self.reader.user_stats(2)
        self.assertEqual(stats['total_gain'],4.5);self.assertEqual(self.reader.lookup('Invented'),(2,'Invented'))
        self.now+=121;self.assertIsNone(self.reader.user_stats(2))

    def test_deferred_optional_cache_fails_closed_then_recovers_locally(self):
        from poolrotater_host.activity.reader import DeferredReader
        from unittest.mock import patch
        deferred=DeferredReader(self.root)
        with patch('poolrotater_host.activity.reader.Reader',side_effect=OSError('invented unavailable cache')):
            self.assertIsNone(deferred.user_stats(2));self.assertIsNone(deferred.hidden(2));self.assertIsNone(deferred.lookup('Invented'))
        self.reader.clock=__import__('time').time
        self.reader.request(user_id=2);self.reader.refresh(self.network)
        self.assertEqual(deferred.user_stats(2)['total_gain'],4.5)

    def test_confirmed_name_miss_is_bounded_and_expires_after_history_arrives(self):
        self.assertIsNone(self.reader.lookup('Historical Player'))
        self.assertEqual(self.reader.lookup_status('Historical Player'),'pending')
        self.reader.refresh(self.network)
        self.assertEqual(self.reader.lookup_status('historical player'),'not_found')
        for _ in range(5):self.assertIsNone(self.reader.lookup('Historical Player'))
        self.assertEqual(self.reader.status()['pending_queries'],0)
        self.remote.ingest(self.remote.achievement(username='Historical Player'))
        self.now+=121
        self.assertIsNone(self.reader.lookup('Historical Player'))
        self.assertEqual(self.reader.lookup_status('Historical Player'),'pending')
        self.reader.refresh(self.network)
        self.assertEqual(self.reader.lookup('Historical Player'),(2,'Historical Player'))

    def test_hidden_name_and_absent_name_have_same_completed_lookup_result(self):
        self.remote.ingest(self.remote.event('privacy'))
        for name in ('Invented','Nobody'):self.reader.lookup(name)
        self.reader.refresh(self.network)
        for name in ('Invented','Nobody'):
            self.assertEqual(self.reader.lookup_status(name),'not_found')
            self.assertIsNone(self.reader.lookup(name))

    def test_failed_reply_never_becomes_a_confirmed_miss(self):
        from unittest.mock import patch
        self.reader.lookup('Historical Player')
        with patch.object(self.network,'request',side_effect=NetworkError('hub_unavailable',503)):
            with self.assertRaises(NetworkError):self.reader.refresh(self.network)
        self.assertEqual(self.reader.lookup_status('Historical Player'),'pending')
        self.assertEqual(self.reader.status()['pending_queries'],1)

    def test_cache_reopen_is_additive_and_keeps_confirmed_miss(self):
        self.reader.lookup('Nobody');self.reader.refresh(self.network)
        reopened=Reader(self.root,clock=lambda:self.now)
        self.assertEqual(reopened.lookup_status('Nobody'),'not_found')
        self.assertIsNone(reopened.lookup('Nobody'))
        self.assertEqual(reopened.status()['pending_queries'],0)

    def test_hub_outage_retains_requests_and_does_not_discard_cache(self):
        self.reader.request(user_id=2)
        class Offline:
            def profile(self):return {}
            def credential(self):return {'credential':'unused'}
            def transport(self,profile):return self
            def request(self,*a,**kw):raise NetworkError('hub_unavailable',503)
        with self.assertRaises(NetworkError):self.reader.refresh(Offline())
        self.assertEqual(self.reader.status()['pending_queries'],1)

    def test_hidden_result_is_account_wide_across_ruleset_caches(self):
        self.reader.request(user_id=2,ruleset_id=0);self.reader.refresh(self.network)
        self.remote.ingest(self.remote.event('privacy'))
        self.reader.request(user_id=2,ruleset_id=3);self.reader.refresh(self.network)
        self.assertTrue(self.reader.hidden(2,ruleset_id=0))
        self.assertTrue(self.reader.hidden(2,ruleset_id=3))

    def test_ppstats_preserves_local_history_separately_without_inventing_scalar_sum(self):
        local=PPStatsStore(str(self.root/'local.sqlite'),reader=self.reader)
        with local._connect() as db:
            db.execute("INSERT INTO autohost_lobby_time VALUES(2,'Invented',70,2,1,2,3,3)")
        self.assertEqual(local.user_stats(2)['total_lobby_seconds'],70)
        self.reader.refresh(self.network)
        # Confirmed shared totals remain canonical. Unuploaded local history is
        # retained separately because other-host participation could overlap.
        self.assertEqual(local.user_stats(2)['total_lobby_seconds'],0)
        self.assertEqual(local.user_stats(2)['local_tracked_lobby_seconds'],70)
        self.assertEqual(local.user_stats(2)['local_unreconciled_lobby_seconds'],70)
        self.assertTrue(local.user_stats(2)['network_statistics'])

    def test_pending_local_overlap_does_not_inflate_shared_confirmed_time(self):
        self.remote.ingest(self.remote.event(started_at=910,ended_at=930))
        token,binding,host=self.remote.enroll(456);self.remote.repo.consent(token,self.remote.policy)
        item=self.remote.event(started_at=920,ended_at=950)
        item.update(installation_id=binding['installation_id'],generation=binding['generation'])
        self.remote.repo.ingest(token,{'protocol_version':1,'events':[item]})
        local=PPStatsStore(str(self.root/'local-overlap.sqlite'),reader=self.reader)
        with local._connect() as db:db.execute("INSERT INTO autohost_lobby_time VALUES(2,'Invented',30,2,910,940,950,950)")
        self.reader.request(user_id=2);self.reader.refresh(self.network)
        before=local.user_stats(2)
        self.assertEqual(before['total_lobby_seconds'],40)
        self.assertEqual(before['local_unreconciled_lobby_seconds'],10)
        # The pending own slice overlaps the other host: upload and refresh must
        # leave the exact canonical union at 40, not 50 seconds.
        self.remote.ingest(self.remote.event(started_at=940,ended_at=950))
        self.reader.refresh(self.network)
        after=local.user_stats(2)
        self.assertEqual(after['total_lobby_seconds'],40)
        self.assertEqual(after['local_unreconciled_lobby_seconds'],0)

    def test_hidden_cache_never_exposes_cached_stats_and_bound_queue(self):
        self.reader.request(user_id=2);self.reader.refresh(self.network)
        self.remote.ingest(self.remote.event('privacy'))
        self.reader.request(user_id=2)
        self.reader.refresh(self.network)
        self.assertTrue(self.reader.hidden(2));self.assertIsNone(self.reader.lookup('Invented'))
        for uid in range(100,400):self.reader.request(user_id=uid)
        self.assertLessEqual(self.reader.status()['pending_queries'],200)

if __name__=='__main__':unittest.main()
