"""Private rooms contribute aggregate stats; unknown history and hidden players do not."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import json,sqlite3,unittest
from unittest.mock import patch
from tests import test_activity_collector as collector_fixture
from tests import test_activity_personal as personal_fixture
from tests import test_reporting_integration as reporting_fixture
from poolrotater_host.activity.collector import Collector
from poolrotater_host.activity import Repository
from poolrotater_host.activity.reader import Reader
from poolrotater_host.activity.replica import Replica


class PrivateParticipationTests(unittest.TestCase):
    setUp=collector_fixture.ActivityCollectorTests.setUp
    consent=collector_fixture.ActivityCollectorTests.consent
    public=collector_fixture.ActivityCollectorTests.public
    presence=collector_fixture.ActivityCollectorTests.presence
    join=collector_fixture.ActivityCollectorTests.join
    events=collector_fixture.ActivityCollectorTests.events
    ack=collector_fixture.ActivityCollectorTests.ack
    seed_pp=personal_fixture.ActivityPersonalCollectorTests.seed_pp

    def private(self):self.native.record_room_visibility(1,public=False,observed_at=50,mode='standard')

    def test_known_private_presence_joins_leaves_and_gains_count(self):
        self.private();self.consent();self.presence(start=101,end=110);self.join();self.seed_pp()
        self.native.record_room_player_leave(room_id=1,user_id=2,username='Invented',left_at=110)
        self.collector.collect()
        self.assertEqual({e['kind'] for e in self.events()},{'presence','join','leave','achievement'})
        raw=json.dumps(self.events());self.assertNotIn('password',raw);self.assertNotIn('room_name',raw)
        first=self.events();self.collector.collect();self.assertEqual(self.events(),first)
        self.ack();self.collector.collect();self.assertEqual(self.events(),[])

    def test_private_completed_match_counts_and_aborted_does_not(self):
        self.private();self.consent()
        for item,result in [(3,'completed'),(4,'aborted')]:
            key=self.native.record_match_start(room_id=1,playlist_item_id=item,beatmap_id=4,map_label='Secret room map',star_rating=4,participants=[{'user_id':2,'username':'Invented'}],started_at=101)
            self.native.record_match_end(match_key=key,room_id=1,playlist_item_id=item,result=result,ended_at=110)
        self.collector.collect();items=self.events()
        self.assertEqual(len(items),1);self.assertEqual(items[0]['kind'],'match')
        self.assertNotIn('Secret room map',json.dumps(items))

    def test_privacy_transition_does_not_lose_a_completed_match(self):
        self.public();self.consent();self.native.record_room_visibility(1,public=False,observed_at=105,mode='mania4')
        key=self.native.record_match_start(room_id=1,playlist_item_id=3,beatmap_id=4,map_label='Secret',star_rating=4,participants=[{'user_id':2,'username':'Invented'}],started_at=101)
        self.native.record_match_end(match_key=key,room_id=1,playlist_item_id=3,result='completed',ended_at=110)
        self.collector.collect();self.assertEqual([e['kind'] for e in self.events()],['match'])

    def test_hidden_player_private_activity_is_withheld(self):
        self.private();self.consent();self.presence();self.join();self.seed_pp()
        with sqlite3.connect(self.ledger) as db:db.execute('INSERT INTO autohost_stats_privacy VALUES(2,1,111)')
        self.collector.collect();self.assertEqual([e['kind'] for e in self.events()],['privacy'])

    def test_independent_cursor_backfills_older_skipped_private_time_once(self):
        self.private();self.consent();self.presence(start=101,end=110);self.join()
        native=Collector._visibility_ranges
        # Reproduce an earlier client that advanced ordinary cursors past private records.
        with patch.object(Collector,'_visibility_ranges',side_effect=lambda *a,public: native(*a,public=True) if public else []):self.collector.collect()
        self.assertEqual(self.events(),[])
        with sqlite3.connect(self.collector.path) as db:db.execute("DELETE FROM meta WHERE key LIKE 'cursor_private_%' OR key LIKE 'anchor_private_%'")
        self.collector.collect(limit=1);first=self.events();self.assertEqual(len(first),2)
        self.collector.collect(limit=1);self.assertEqual(self.events(),first)
        self.ack();self.collector.collect();self.assertEqual(self.events(),[])


class PrivateStatisticsIntegrationTests(unittest.TestCase):
    setUpClass=classmethod(reporting_fixture.ReportingIntegrationTests.setUpClass.__func__)
    tearDownClass=classmethod(reporting_fixture.ReportingIntegrationTests.tearDownClass.__func__)
    setUp=reporting_fixture.ReportingIntegrationTests.setUp
    stop=reporting_fixture.ReportingIntegrationTests.stop
    enable=reporting_fixture.ReportingIntegrationTests.enable

    def test_private_playtime_reaches_shared_reader_and_public_aggregate(self):
        import time
        from website.statistics_v71.network_statistics import compose,groups
        self.enable();now=time.time()
        self.ledger.record_room_visibility(99,public=False,observed_at=now-1,mode='standard')
        self.ledger.record_room_player_join(room_id=99,user_id=7,username='Private Player',joined_at=now)
        self.ledger.add_room_player_time(room_id=99,user_id=7,username='Private Player',seconds=2,left_at=now+2)
        for _ in range(5):self.reporter.step()
        snapshot=Repository(self.store).snapshot()
        self.assertEqual(len(snapshot['presence']),1)
        self.server.statistics.stamp=None;Replica(self.root).sync(self.client,force=True)
        self.assertGreater(Reader(self.root).user_stats(7)['total_lobby_seconds'],0)
        # Website projection materializes player totals, never raw room IDs/chats.
        result=compose(groups(), {}, set(), snapshot, set(), now+3)
        self.assertGreater(result[0]["all"]["all"]["totals"]["seconds"],0)
        encoded=json.dumps(result)
        self.assertIn('Private Player',encoded)
        self.assertNotIn('room_id',encoded);self.assertNotIn('password',encoded)
