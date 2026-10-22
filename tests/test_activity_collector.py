"""Opt-in participation, exact native evidence and durable offline replay."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from bot.ratings import RatingStore
from poolrotater_host.activity.collector import Collector
from poolrotater_host.activity.contracts import batch
from poolrotater_host.network.contracts import NetworkError


class ActivityCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'state').mkdir();self.now=100
        self.marker={'installation_id':str(uuid.uuid4()),'generation':str(uuid.uuid4())}
        self.ledger=self.root/'native.sqlite';self.native=RatingStore(str(self.ledger))
        with sqlite3.connect(self.ledger) as db:
            db.execute('CREATE TABLE autohost_stats_privacy(user_id INTEGER PRIMARY KEY,hidden INTEGER,updated_at REAL)')
        self.collector=Collector(self.root,self.ledger,clock=lambda:self.now,marker=self.marker)

    def consent(self):return self.collector.configure(share_statistics=True)
    def public(self,room=1):self.native.record_room_visibility(room,public=True,observed_at=50,mode='mania4')
    def presence(self,start=90,end=110,room=1,user=2):
        self.native.add_room_player_time(room_id=room,user_id=user,username='Invented',seconds=end-start,left_at=end)
    def join(self,at=101,room=1,user=2):self.native.record_room_player_join(room_id=room,user_id=user,username='Invented',joined_at=at)
    def events(self):return self.collector.batch()['events']
    def ack(self):
        self.collector.acknowledge({'ok':True,'protocol_version':1,'received_at':self.now,'outcomes':[{'event_id':e['event_id'],'phase':'accepted'} for e in self.events()]})

    def test_no_consent_means_no_capture_and_enable_clips_ongoing_session(self):
        self.public();self.presence();self.join(at=90)
        self.assertFalse(self.collector.collect()['capture_enabled']);self.assertEqual(self.events(),[])
        self.consent();self.collector.collect();items=self.events()
        self.assertEqual(len(items),1);self.assertEqual(items[0]['kind'],'presence')
        self.assertEqual(items[0]['payload']['started_at'],100)
        self.assertEqual(items[0]['payload']['ended_at'],110)
        self.assertEqual(items[0]['payload']['mode'],'mania4')

    def test_known_private_intervals_count_but_missing_visibility_stays_excluded(self):
        self.consent();self.presence(room=2);self.join(room=2)
        self.public();self.native.record_room_visibility(1,public=False,observed_at=105)
        self.presence(start=101,end=110);self.join(at=105)
        self.collector.collect();items=self.events()
        self.assertEqual(len(items),3)
        presence=[e['payload'] for e in items if e['kind']=='presence']
        self.assertEqual([(r['started_at'],r['ended_at']) for r in presence],[(101,105),(105,110)])
        self.assertEqual([e['kind'] for e in items].count('join'),1)
        self.assertEqual({e['payload']['room_id'] for e in items},{1})

    def test_restart_offline_replay_and_ack_are_durable_without_duplicates(self):
        self.consent();self.public();self.presence(start=101,end=110);self.join()
        self.collector.collect();first=self.events();self.assertEqual(len(first),2)
        self.collector=Collector(self.root,self.ledger,clock=lambda:self.now,marker=self.marker)
        self.collector.collect();self.assertEqual(self.events(),first);self.ack()
        self.collector.collect();self.assertEqual(self.events(),[])
        self.assertEqual(self.native._conn().execute('SELECT total_seconds FROM autohost_room_players').fetchone()[0],9)

    def test_disable_discards_unsent_old_epoch_and_reenable_starts_new_boundary(self):
        self.public();old=self.consent();self.presence(start=101,end=110);self.collector.collect()
        self.assertEqual(len(self.events()),1);self.now=120
        self.collector.configure(share_statistics=False);self.assertEqual(self.events(),[])
        self.now=130;new=self.consent();self.assertNotEqual(new['consent_id'],old['consent_id'])
        self.collector.collect();self.assertEqual(self.events(),[])
        self.presence(start=125,end=140);self.collector.collect();self.assertEqual(self.events()[0]['payload']['started_at'],130)

    def test_hidden_player_removes_pending_participation_and_emits_private_preference(self):
        self.public();self.consent();self.presence();self.collector.collect()
        with sqlite3.connect(self.ledger) as db:db.execute('INSERT INTO autohost_stats_privacy VALUES(2,1,111)')
        self.collector.collect();self.assertEqual([e['kind'] for e in self.events()],['privacy'])
        self.assertTrue(self.events()[0]['payload']['hidden'])

    def test_completed_match_revisited_for_late_scores_and_aborted_not_counted(self):
        self.public();self.consent()
        key=self.native.record_match_start(room_id=1,playlist_item_id=3,beatmap_id=4,map_label='Invented',star_rating=4,participants=[{'user_id':2,'username':'Invented'}],started_at=101)
        self.collector.collect();self.assertEqual(self.events(),[])
        self.native.record_match_end(match_key=key,room_id=1,playlist_item_id=3,result='completed',ended_at=110)
        self.collector.collect();self.assertEqual(len(self.events()),1);self.assertIsNone(self.events()[0]['payload']['accuracy'])
        self.ack();self.native.record_match_scores(match_key=key,scores=[{'user_id':2,'username':'Invented','accuracy':.98}])
        self.collector.collect();self.collector.collect()
        self.assertEqual(len(self.events()),1);self.assertEqual(self.events()[0]['payload']['accuracy'],.98)

    def test_bounded_collection_keeps_cursor_until_next_scan(self):
        self.public();self.consent()
        for i in range(6):self.join(at=101+i,user=10+i)
        self.collector.collect(limit=2);self.assertEqual(len(self.events()),2)
        self.collector.collect(limit=2);self.assertEqual(len(self.events()),4)
        self.collector.collect(limit=2);self.assertEqual(len(self.events()),6)

    def test_restore_that_rewinds_append_cursor_requires_review(self):
        self.public();self.consent();self.join();self.collector.collect()
        with sqlite3.connect(self.ledger) as db:db.execute('DELETE FROM activity_joins')
        with self.assertRaisesRegex(NetworkError,'restore_requires_review'):self.collector.collect()

    def test_same_size_replaced_source_does_not_silently_skip_changed_history(self):
        self.public();self.consent();self.join();self.collector.collect()
        with sqlite3.connect(self.ledger) as db:db.execute("UPDATE activity_joins SET user_id=99")
        with self.assertRaisesRegex(NetworkError,'restore_requires_review'):self.collector.collect()

    def test_leave_is_distinct_from_periodic_presence_and_wire_is_closed(self):
        self.public();self.consent();self.presence(start=101,end=110)
        self.native.record_room_player_leave(room_id=1,user_id=2,username='Invented',left_at=111)
        self.collector.collect();self.assertEqual({e['kind'] for e in self.events()},{'presence','leave'})
        altered=copy.deepcopy(self.collector.batch());altered['events'][0]['payload']['access_token']='never valid'
        with self.assertRaises(NetworkError):batch(altered)

    def test_full_outbox_pauses_capture_without_losing_cursor_and_can_drain(self):
        from unittest.mock import patch
        self.public();self.consent()
        for uid in range(2,6):self.join(user=uid)
        with patch('poolrotater_host.activity.collector.MAX_OUTBOX_EVENTS',2):
            first=self.collector.collect();self.assertTrue(first['capture_paused']);self.assertTrue(first['privacy_safe'])
            self.assertEqual(len(self.events()),2)
            with sqlite3.connect(self.collector.path) as db:self.assertEqual(db.execute("SELECT value FROM meta WHERE key='cursor_activity_joins'").fetchone()[0],'2')
            self.ack();self.assertEqual(self.collector.status()['pending_events'],0)
            self.collector.collect();self.assertEqual(len(self.events()),2)
            self.assertEqual({e['payload']['user_id'] for e in self.events()},{4,5})
            self.ack();self.collector.collect();self.assertEqual(self.events(),[])

    def test_full_ordinary_queue_commits_hidden_purge_and_prioritizes_privacy(self):
        from unittest.mock import patch
        self.public();self.consent();self.join(user=2);self.join(user=3)
        with patch('poolrotater_host.activity.collector.MAX_OUTBOX_EVENTS',2):
            self.collector.collect()
            with sqlite3.connect(self.ledger) as db:db.execute('INSERT INTO autohost_stats_privacy VALUES(2,1,111)')
            self.join(user=4)
            result=self.collector.collect();self.assertTrue(result['privacy_safe'])
            events=self.events();self.assertEqual(events[0]['kind'],'privacy')
            self.assertNotIn(2,{e['payload']['user_id'] for e in events if e['kind']!='privacy'})
            self.assertEqual({e['payload']['user_id'] for e in events if e['kind']!='privacy'},{3,4})

    def test_privacy_capacity_blocks_ordinary_upload_but_drains_existing_privacy(self):
        from unittest.mock import patch
        self.public();self.consent();self.join(user=4)
        with sqlite3.connect(self.ledger) as db:db.executemany('INSERT INTO autohost_stats_privacy VALUES(?,1,111)',[(2,),(3,)])
        with patch('poolrotater_host.activity.collector.MAX_PRIVACY_EVENTS',1):
            result=self.collector.collect();self.assertFalse(result['privacy_safe']);self.assertTrue(result['capture_paused'])
            self.assertEqual([e['kind'] for e in self.events()],['privacy'])
            self.ack();result=self.collector.collect();self.assertTrue(result['privacy_safe'])
            self.assertEqual([e['kind'] for e in self.events()],['privacy','join'])
            self.assertEqual(self.events()[0]['payload']['user_id'],3)

    def test_foreign_generation_cannot_reuse_durable_queue(self):
        with self.assertRaisesRegex(NetworkError,'foreign_activity_stream'):
            Collector(self.root,self.ledger,marker={**self.marker,'generation':str(uuid.uuid4())})

if __name__=='__main__':unittest.main()
