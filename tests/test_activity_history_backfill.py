"""Reviewed legacy history drains independently of occupied native workers."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import json
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_v71_history_helper as helper_tests
from poolrotater_host.activity import Collector
from poolrotater_host.activity import history
from poolrotater_host.activity.storage import connection
from poolrotater_host.layout import atomic_json


class HistoryBackfillTests(unittest.TestCase):
    setUp=helper_tests.HistoryHelperTests.setUp
    enable=helper_tests.HistoryHelperTests.enable

    def seed(self, count=3):
        # User9 has public history, 10 has mixed history, 11 is unproven.
        self.enable()
        with sqlite3.connect(self.root/'state/controller-ratings.db') as db:
            for uid in range(9,9+count):
                db.execute("INSERT INTO autohost_lobby_time VALUES(?,'Historical Player',50,3,20,80,90,90)",(uid,))
            db.executemany("INSERT INTO autohost_room_players VALUES(?,?,'Historical Player',20,80,90,3,50)",[(71,9),(71,10),(72,10)])

    def plan(self):
        value={'format':1,**self.value,'include_lobby_totals':True,'exclude_unreviewed_players':True}
        atomic_json(self.root/history.PLAN,value)
        return value

    def test_mixed_and_unproven_users_are_withheld_while_public_user_is_imported(self):
        self.seed();self.plan()
        result=history.step(self.root,limit=1)
        self.assertEqual(result['phase'],'queued')
        events=Collector(self.root).batch()['events']
        self.assertEqual([(e['kind'],e['payload']['user_id']) for e in events],[('baseline',9)])
        self.assertEqual(events[0]['payload']['total_seconds'],50)
        self.assertEqual(events[0]['payload']['cutoff'],90)
        self.assertEqual(history.step(self.root),result)

    def test_private_witness_over_public_manifest_is_still_refused(self):
        self.seed();self.plan()
        self.native.record_room_visibility(71,public=False,observed_at=10)
        self.native.add_room_player_time(room_id=71,user_id=9,username='Historical Player',seconds=5,left_at=30)
        with self.assertRaisesRegex(Exception,'private_time_refused'):history.step(self.root)
        self.assertEqual(Collector(self.root).batch()['events'],[])

    def test_excluded_private_room_does_not_prevent_unrelated_public_backfill(self):
        self.seed();self.plan()
        self.native.record_room_visibility(72,public=False,observed_at=10)
        self.native.add_room_player_time(room_id=72,user_id=10,username='Mixed Player',seconds=5,left_at=30)
        self.assertEqual(history.step(self.root)['phase'],'queued')
        self.assertEqual([e['payload']['user_id'] for e in Collector(self.root).batch()['events']],[9])

    def test_hidden_players_are_not_backfilled(self):
        self.seed();self.plan()
        with sqlite3.connect(self.root/'state/controller-ratings.db') as db:
            db.execute("INSERT INTO autohost_stats_privacy VALUES(9,'Historical Player',1,95)")
        self.assertEqual(history.step(self.root)['phase'],'queued')
        self.assertEqual(Collector(self.root).batch()['events'],[])

    def test_no_manifest_no_import_and_foreign_manifest_refused(self):
        self.seed();self.assertIsNone(history.step(self.root))
        value=self.plan();value['generation']=__import__('uuid').uuid4().__str__()
        atomic_json(self.root/history.PLAN,value)
        with self.assertRaisesRegex(Exception,'foreign_history_review'):history.step(self.root)

    def test_changed_review_and_consent_cannot_silently_replay_frozen_history(self):
        self.seed();value=self.plan();history.step(self.root)
        atomic_json(self.root/history.PLAN,{**value,'public_room_ids':[71,72]})
        with self.assertRaisesRegex(Exception,'review_changed'):history.step(self.root)
        atomic_json(self.root/history.PLAN,value)
        Collector(self.root).configure(share_statistics=False)
        with self.assertRaisesRegex(Exception,'sharing_disabled'):history.step(self.root)
        Collector(self.root).configure(share_statistics=True)
        with self.assertRaisesRegex(Exception,'consent_changed'):history.step(self.root)

    def test_full_queue_and_restart_drain_without_recapturing_changed_native_totals(self):
        self.seed();self.plan()
        with sqlite3.connect(self.root/'state/controller-ratings.db') as db:
            db.execute('DELETE FROM autohost_room_players WHERE room_id=72')
            db.execute("INSERT INTO autohost_room_players VALUES(71,11,'Historical Player',20,80,90,3,50)")
        with patch('poolrotater_host.activity.collector.MAX_OUTBOX_EVENTS',1):
            self.assertEqual(history.step(self.root,limit=2)['phase'],'paused')
            with sqlite3.connect(self.root/'state/controller-ratings.db') as db:
                db.execute('UPDATE autohost_lobby_time SET total_seconds=999')
            for _ in range(3):
                collector=Collector(self.root);events=collector.batch()['events']
                self.assertEqual(len(events),1);self.assertEqual(events[0]['payload']['total_seconds'],50)
                collector.acknowledge({'ok':True,'protocol_version':1,'outcomes':[{'event_id':e['event_id'],'phase':'accepted'} for e in events],'received_at':100})
                result=history.step(self.root,limit=2)
        self.assertEqual(result['phase'],'queued')
        self.assertEqual(result['historical_events_queued'],3)

    def test_failed_receipt_write_does_not_allow_different_review(self):
        self.seed();value=self.plan()
        with patch('poolrotater_host.activity.history.atomic_json',side_effect=OSError('invented interrupted receipt')):
            with self.assertRaises(OSError):history.step(self.root)
        atomic_json(self.root/history.PLAN,{**value,'public_room_ids':[71,72]})
        with self.assertRaisesRegex(Exception,'review_changed'):history.step(self.root)

    def backup_plan(self):
        import hashlib
        value=self.plan()
        backup=self.root/'backups/ratings-before-v71-activity.sqlite'
        backup.parent.mkdir(exist_ok=True)
        with sqlite3.connect(self.root/'state/controller-ratings.db') as source,sqlite3.connect(backup) as target:
            source.backup(target)
        source.close();target.close()
        backup.chmod(0o600)
        with sqlite3.connect(backup) as db:
            names=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'activity_%'")]
            for name in names:db.execute('DROP TABLE '+name)
        db.close()
        value.update(source='migration-backup',source_sha256=hashlib.sha256(backup.read_bytes()).hexdigest())
        atomic_json(self.root/history.PLAN,value)
        return value,backup

    def test_pinned_pre_telemetry_backup_imports_original_totals_and_current_privacy(self):
        self.seed();self.backup_plan()
        with sqlite3.connect(self.root/'state/controller-ratings.db') as db:
            db.execute('UPDATE autohost_lobby_time SET total_seconds=999')
        self.assertEqual(history.step(self.root)['phase'],'queued')
        events=Collector(self.root).batch()['events']
        self.assertEqual(events[0]['payload']['total_seconds'],50)

    def test_current_hidden_choice_is_respected_even_when_backup_was_public(self):
        self.seed();self.backup_plan()
        with sqlite3.connect(self.root/'state/controller-ratings.db') as db:
            db.execute("INSERT INTO autohost_stats_privacy VALUES(9,'Historical Player',1,95)")
        history.step(self.root)
        self.assertEqual(Collector(self.root).batch()['events'],[])

    def test_changed_backup_digest_is_refused_before_queueing(self):
        self.seed();_,backup=self.backup_plan()
        with sqlite3.connect(backup) as db:db.execute('UPDATE autohost_lobby_time SET total_seconds=999')
        db.close()
        with self.assertRaisesRegex(Exception,'source_digest_mismatch'):history.step(self.root)
        self.assertEqual(Collector(self.root).batch()['events'],[])

    def test_history_name_resolves_on_a_different_host_without_double_counting_presence(self):
        from tests.test_activity_personal import ActivityPersonalRepositoryTests
        from poolrotater_host.activity.reader import Reader
        from poolrotater_host.activity.query import player
        import tempfile
        from pathlib import Path
        remote=ActivityPersonalRepositoryTests();remote.setUp();self.addCleanup(remote.doCleanups)
        remote.enable();self.seed();self.plan()
        consent=Collector(self.root).consent()
        # Match the local capture identity to the enrolled source, preserving
        # the actual authenticated hub consent/ingestion/query implementation.
        collector=Collector(self.root)
        history.step(self.root)
        events=collector.batch()['events']
        for event in events:
            event.update(installation_id=remote.binding['installation_id'],generation=remote.binding['generation'],consent_id=remote.policy['consent_id'])
        receipt=remote.repo.ingest(remote.token,{'protocol_version':1,'events':events})
        self.assertTrue(all(o['phase']=='accepted' for o in receipt['outcomes']))
        token,binding,host=remote.enroll(456)
        remote.repo.consent(token,remote.policy)
        class Network:
            def profile(self):return {}
            def credential(self):return {'credential':token}
            def transport(self,p):return self
            def request(self,method,path,token,body):return remote.repo.query(token,body)
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name);(root/'state').mkdir()
        reader=Reader(root)
        self.assertIsNone(reader.lookup('Historical Player'))
        reader.refresh(Network())
        self.assertEqual(reader.lookup('Historical Player'),(9,'Historical Player'))
        self.assertEqual(reader.user_stats(9)['total_lobby_seconds'],50)
        # Replaying the same event cannot multiply a source baseline.
        remote.repo.ingest(remote.token,{'protocol_version':1,'events':events})
        self.assertEqual(player(remote.repo.snapshot(),9)['total_lobby_seconds'],50)


if __name__=='__main__':unittest.main()
