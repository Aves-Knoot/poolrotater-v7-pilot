"""Reviewed stopped backfill preserves consent, private history and replay safety."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.import_v71_public_history import review, history_action, pairs, binding, stopped_units
from poolrotater_host.layout import setup, load, owner_lease
from poolrotater_host.activity_migration import initialize
from poolrotater_host.reporting import configure, profile
from poolrotater_host.activity import Collector
from bot.ratings import RatingStore


class HistoryHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'host'; setup(self.root)
        self.mask = os.umask(0o077); self.addCleanup(os.umask, self.mask)
        initialize(self.root)
        self.marker, _ = load(self.root)
        # This helper tests preservation of an explicit opt-out. New v7.1.1+
        # installations have default-on sharing, which is not a history review.
        from poolrotater_host.layout import atomic_json
        atomic_json(self.root/'config/reporting.json',{'format':1,**self.marker,
            'share_statistics':False,'consume_statistics':False,
            'operational_logs':False,'public_chat':False})
        self.native = RatingStore(str(self.root/'state/controller-ratings.db'))
        self.native.record_room_open(room_id=71, room_name='Invented public history', created_at=1)
        self.native.record_room_open(room_id=72, room_name='Invented private history', created_at=1)
        self.value = {**{key:self.marker[key] for key in ('installation_id','generation')},
                      'public_room_ids':[71], 'public_history_confirmed':True, 'include_lobby_totals':False}

    def enable(self):
        configure(self.root,dict(share_statistics=True,consume_statistics=False,operational_logs=False,public_chat=False))

    def test_plan_lists_ids_for_review_without_opting_in_or_importing(self):
        answer = history_action(self.root, None, apply=False, batches=1)
        self.assertEqual(answer['source_room_ids_for_review'], [71,72])
        self.assertFalse(answer['public_history_inferred'])
        self.assertFalse(answer['import_applied'])
        self.assertFalse(profile(self.root)['share_statistics'])
        self.assertFalse((self.root/'state/activity-outbox.sqlite3').exists())

    def test_explicit_installation_bound_review_and_no_sharing_no_import(self):
        with self.assertRaisesRegex(ValueError, 'independently_enabled'):
            history_action(self.root,self.value,apply=True,batches=1)
        self.enable()
        import uuid
        with self.assertRaisesRegex(ValueError, 'foreign_history_review'):
            history_action(self.root,{**self.value,'generation':str(uuid.uuid4())},apply=True,batches=1)
        self.assertEqual(Collector(self.root).batch()['events'],[])

    def test_cumulative_totals_refuse_incomplete_public_provenance(self):
        self.enable()
        with self.assertRaisesRegex(Exception, 'incomplete_public_provenance'):
            history_action(self.root,{**self.value,'include_lobby_totals':True},apply=True,batches=1)
        self.assertEqual(Collector(self.root).batch()['events'],[])

    def test_reviewed_cumulative_baselines_replay_once_without_changing_local_history(self):
        self.enable()
        with sqlite3.connect(self.root/'state/controller-ratings.db') as db:
            db.execute("INSERT INTO autohost_lobby_time VALUES(9,'Invented',50,3,20,80,90,90)")
        value={**self.value,'public_room_ids':[71,72],'include_lobby_totals':True}
        first=history_action(self.root,value,apply=True,batches=20)
        self.assertEqual(first['historical_events_queued'],1)
        self.assertFalse(first['import_may_need_more_batches'])
        second=history_action(self.root,value,apply=True,batches=20)
        self.assertEqual(second['historical_events_queued'],0)
        events=Collector(self.root).batch()['events']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['payload']['total_seconds'],50)
        with sqlite3.connect(self.root/'state/controller-ratings.db') as db:
            self.assertEqual(db.execute('SELECT total_seconds FROM autohost_lobby_time').fetchone()[0],50)
        receipt=json.loads((self.root/'state/activity-history-review.json').read_text())
        self.assertEqual(receipt['review'],value)
        self.assertNotIn('Invented',json.dumps(receipt))

    def test_owner_lease_and_unknown_ownership_refuse(self):
        with owner_lease(self.root):
            with self.assertRaises(Exception):history_action(self.root,None,apply=False,batches=1)
        from poolrotater_host.layout import atomic_json
        # Test the real journal validator rather than manufacture an unknown
        # schema: a malformed journal must also refuse, never be reset.
        (self.root/'state/rolling').mkdir(mode=0o700,exist_ok=True)
        atomic_json(self.root/'state/rolling/ownership.json',{'ownership_unknown':True})
        with self.assertRaises(Exception):history_action(self.root,None,apply=False,batches=1)
        self.assertEqual(json.loads((self.root/'state/rolling/ownership.json').read_text()),{'ownership_unknown':True})

    def test_bad_reviews_and_duplicate_json_fields_refuse(self):
        for changed in ({'public_history_confirmed':False},{'include_lobby_totals':1},
                        {'public_room_ids':[True]},{'public_room_ids':[]},
                        {'public_room_ids':[71,71]},{'generation':'invalid'}, {'extra':'field'}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):review({**self.value,**changed})
        with self.assertRaisesRegex(ValueError,'duplicate'):pairs([('generation','a'),('generation','b')])

    def seed_achievements(self, rooms):
        with sqlite3.connect(self.root/'state/controller-ratings.db') as db:
            db.execute('CREATE TABLE IF NOT EXISTS autohost_pp_achievements(achievement_key TEXT PRIMARY KEY,user_id INTEGER,username TEXT,beatmap_id INTEGER,room_id INTEGER,playlist_item_id INTEGER,score_pp REAL,profile_gain_pp REAL,top_rank INTEGER,accuracy REAL,created_at REAL)')
            for item,room in enumerate(rooms,1):
                db.execute('INSERT INTO autohost_pp_achievements VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (f'room:{room}:item:{item}:user:9',9,'Invented',5,room,item,123,4.5,20,98,10))

    def test_zero_queued_private_first_chunk_does_not_hide_later_public_pp(self):
        self.seed_achievements([72]*500+[71]);self.enable()
        first=history_action(self.root,self.value,apply=True,batches=1)
        self.assertEqual(first['historical_events_queued'],0)
        self.assertTrue(first['import_may_need_more_batches']);self.assertFalse(first['history_scan_complete'])
        second=history_action(self.root,self.value,apply=True,batches=3)
        self.assertEqual(second['historical_events_queued'],1);self.assertTrue(second['history_scan_complete'])
        self.assertEqual(Collector(self.root).batch()['events'][0]['payload']['room_id'],71)

    def test_changed_review_rescans_skipped_rows_without_duplicate_pp(self):
        self.seed_achievements([72,71]);self.enable()
        first=history_action(self.root,self.value,apply=True,batches=2)
        self.assertEqual(first['historical_events_queued'],1)
        second=history_action(self.root,{**self.value,'public_room_ids':[71,72]},apply=True,batches=2)
        self.assertEqual(second['historical_events_queued'],1)
        self.assertEqual(len(Collector(self.root).batch()['events']),2)

    def test_pp_only_capacity_pause_is_explicit_and_resumes_after_drain(self):
        self.seed_achievements([71]*3);self.enable()
        with patch('poolrotater_host.activity.collector.MAX_OUTBOX_EVENTS',2):
            first=history_action(self.root,self.value,apply=True,batches=3)
            self.assertTrue(first['history_capture_paused']);self.assertFalse(first['history_scan_complete'])
            collector=Collector(self.root);events=collector.batch()['events']
            collector.acknowledge({'ok':True,'protocol_version':1,'received_at':100,'outcomes':[{'event_id':e['event_id'],'phase':'accepted'} for e in events]})
            second=history_action(self.root,self.value,apply=True,batches=3)
            self.assertEqual(second['historical_events_queued'],1);self.assertTrue(second['history_scan_complete'])

    def test_unit_gate_rejects_active_units_and_update_timers(self):
        import subprocess
        def states(command,**kwargs):
            name=command[2]
            active=name.endswith('.timer')
            return subprocess.CompletedProcess(command,0,'LoadState=loaded\nActiveState='+('active' if active else 'inactive')+'\nMainPID=0\n','')
        with patch('scripts.import_v71_public_history.subprocess.run',side_effect=states):
            with self.assertRaisesRegex(ValueError,'must_be_stopped'):stopped_units('first')
        absent=subprocess.CompletedProcess([],4,'LoadState=not-found\n','')
        with patch('scripts.import_v71_public_history.subprocess.run',return_value=absent)as commands:
            stopped_units('first');self.assertEqual(commands.call_count,6)

    def test_loaded_inactive_timers_without_mainpid_are_stopped(self):
        import subprocess
        def states(command,**kwargs):
            return subprocess.CompletedProcess(command,0,'LoadState=loaded\nActiveState=inactive\n'+('' if command[2].endswith('.timer') else 'MainPID=0\n'),'')
        with patch('scripts.import_v71_public_history.subprocess.run',side_effect=states):
            stopped_units('first')

    def test_bootstrap_cannot_target_other_paths_or_unprotected_code(self):
        with self.assertRaisesRegex(ValueError,'invalid_instance'):binding('../owner')
        with patch('scripts.import_v71_public_history.protected',side_effect=ValueError('unprotected_installed_code')):
            with self.assertRaisesRegex(ValueError,'unprotected'):binding('first')


if __name__ == '__main__':unittest.main()
