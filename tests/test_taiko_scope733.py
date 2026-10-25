"""Ruleset-separated feedback and personal totals, including shared wire IDs."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from bot.rating_outbox import (CATCH_KEY_BASE, TAIKO_KEY_BASE, MAX_PROVIDER_MAP_ID,
                               map_key, prepare, public_key, storage_key)
from bot.scoped_ratings import ScopedRatingStore
from bot.scoped_ppstats import CatchPPStatsStore, TaikoPPStatsStore
from poolrotater_host.ratings.contracts import event as validate_event
from poolrotater_host.ratings.snapshots import Publisher, snapshot_key, validate_bundle
from poolrotater_host.network.contracts import NetworkError


class TaikoScope733Tests(unittest.TestCase):
    def test_taiko_identity_has_a_safe_disjoint_key_and_closed_origin(self):
        legacy_boundary = 2**51 + 123
        self.assertEqual(map_key(123, 0), 123)
        self.assertEqual(map_key(legacy_boundary), legacy_boundary)
        self.assertEqual(map_key(123, 1), TAIKO_KEY_BASE + 123)
        self.assertEqual(map_key(123, 2), CATCH_KEY_BASE + 123)
        self.assertEqual(public_key(legacy_boundary), (legacy_boundary, None, 'legacy-map'))
        self.assertEqual(public_key(TAIKO_KEY_BASE + 123), (123, 1, 'taiko'))
        self.assertLess(TAIKO_KEY_BASE + MAX_PROVIDER_MAP_ID, 2**53)
        self.assertEqual(TAIKO_KEY_BASE, CATCH_KEY_BASE + 2**33)
        with self.assertRaisesRegex(ValueError, 'map_identity'):
            public_key(CATCH_KEY_BASE + MAX_PROVIDER_MAP_ID + 1)
        with self.assertRaisesRegex(ValueError, 'taiko_identity'):
            storage_key(123, {'ruleset_id': 1}, map_origin='native')
        self.assertEqual(storage_key(legacy_boundary), legacy_boundary)
        with self.assertRaisesRegex(ValueError, 'map_identity'):
            storage_key(TAIKO_KEY_BASE + 123)

    def test_shared_outbox_keeps_taiko_format3_origin_and_ruleset(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'ratings.db')
            standard = ScopedRatingStore(path, initialize=True)
            catch = ScopedRatingStore(path, initialize=True, ruleset_id=2)
            taiko = ScopedRatingStore(path, initialize=True, ruleset_id=1)
            with sqlite3.connect(path) as db:
                db.execute('BEGIN IMMEDIATE')
                prepare(db, installation_id=str(uuid.uuid4()), generation=str(uuid.uuid4()), capture_started=1)
                db.commit()

            self.assertTrue(standard.rate(123, 7, 1, map_origin='standard-native',
                                          context={'ruleset_id': 0}))
            self.assertTrue(catch.rate(123, 7, -1, map_origin='catch-native-nm',
                                       context={'ruleset_id': 2}))
            self.assertTrue(taiko.rate(123, 7, 1, map_origin='taiko-convert-nm',
                                       context={'ruleset_id': 1}))
            taiko.record_room_visibility(13, public=True, observed_at=10, preset='taiko-fixed')
            self.assertEqual(standard.tally(123), (1, 0))
            self.assertEqual(catch.tally(123), (0, 1))
            self.assertEqual(taiko.tally(123), (1, 0))

            with sqlite3.connect(path) as db:
                rows = [json.loads(row[0]) for row in db.execute(
                    'SELECT payload FROM rating_outbox ORDER BY sequence')]
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual([row['format'] for row in rows], [1, 2, 3])
            self.assertEqual(rows[2]['context']['ruleset_id'], 1)
            self.assertEqual(rows[2]['map_origin'], 'taiko-convert-nm')
            self.assertTrue(validate_event(rows[2]))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT mode FROM activity_visibility WHERE room_id=13').fetchone()[0], 'taiko')
            self.assertTrue({'map_ratings', 'map_ratings_catch', 'map_ratings_taiko'} <= tables)

    def test_personal_stats_are_isolated_for_taiko_and_catch(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'stats.db')
            catch = CatchPPStatsStore(path)
            taiko = TaikoPPStatsStore(path)
            score = {'user_id': 44, 'name': 'Player', 'gain': 12.5,
                     'rank': 1, 'pp': 150.0, 'accuracy': 0.98}
            self.assertEqual(catch.record_achievements(room_id=2, playlist_item_id=8,
                            beatmap_id=123, achievements=[score]), 1)
            self.assertEqual(taiko.record_achievements(room_id=2, playlist_item_id=8,
                            beatmap_id=123, achievements=[score]), 1)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM autohost_pp_achievements_catch').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM autohost_pp_achievements_taiko').fetchone()[0], 1)
                self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='autohost_pp_achievements'").fetchone())

    def test_snapshot_versions_keep_taiko_out_of_old_client_schemas(self):
        legacy_boundary = 2**51 + 123
        self.assertEqual(snapshot_key({'beatmap_id': 123, 'good': 1, 'bad': 0,
                                       'neutral': 0}, 1), 123)
        self.assertEqual(snapshot_key({'beatmap_id': legacy_boundary, 'good': 1, 'bad': 0,
                                       'neutral': 0}, 1), legacy_boundary)
        self.assertEqual(snapshot_key({'beatmap_id': legacy_boundary, 'good': 1, 'bad': 0,
                                       'neutral': 0, 'ruleset_id': None,
                                       'variant': 'legacy-map'}, 2), legacy_boundary)
        self.assertEqual(snapshot_key({'beatmap_id': 123, 'good': 1, 'bad': 0,
                                       'neutral': 0, 'ruleset_id': 2,
                                       'variant': 'native-NM'}, 2), CATCH_KEY_BASE + 123)
        taiko_row = {'beatmap_id': 123, 'good': 1, 'bad': 0, 'neutral': 0,
                     'ruleset_id': 1, 'variant': 'taiko'}
        self.assertEqual(snapshot_key(taiko_row, 3), TAIKO_KEY_BASE + 123)
        with self.assertRaises(NetworkError):
            snapshot_key(taiko_row, 2)

    def test_publisher_negotiates_v1_v2_and_taiko_v3_without_key_leakage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / 'hub.db'
            epoch = str(uuid.uuid4())
            with sqlite3.connect(db_path) as db:
                db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.executemany('INSERT INTO meta VALUES(?,?)',
                               [('ratings_watermark', '5'), ('epoch', epoch)])
                db.execute('CREATE TABLE ratings_current(beatmap_id INTEGER,user_id INTEGER,rating INTEGER,suppressed INTEGER)')
                db.executemany('INSERT INTO ratings_current VALUES(?,?,?,0)', [
                    (123, 7, 1), (2**51 + 123, 10, 1), (CATCH_KEY_BASE + 123, 8, -1),
                    (TAIKO_KEY_BASE + 123, 9, 1)])

            class Store:
                def __init__(self):
                    self.root = root
                @staticmethod
                def clock(): return 10.0
                @contextmanager
                def transaction(self):
                    db = sqlite3.connect(db_path)
                    db.row_factory = sqlite3.Row
                    try: yield db
                    finally: db.close()

            class Policy:
                @staticmethod
                def check(_db): return {'operations': []}, None

            class Repository:
                store = Store()
                policy = Policy()

            publisher = Publisher(Repository())
            variants = ((publisher.current(), 1, {123, 2**51 + 123}),
                        (publisher.current(context=True), 2,
                         {123, 2**51 + 123, CATCH_KEY_BASE + 123}),
                        (publisher.current(context=True, taiko=True), 3,
                         {123, 2**51 + 123, CATCH_KEY_BASE + 123, TAIKO_KEY_BASE + 123}))
            for manifest, expected_format, expected_keys in variants:
                self.assertEqual(manifest['format'], expected_format)
                folder = root / ('ratings-snapshots' if expected_format == 1 else
                                 'ratings-snapshots-v2' if expected_format == 2 else
                                 'ratings-snapshots-v3') / manifest['content_id']
                self.assertEqual(set(validate_bundle(folder, manifest)), expected_keys)


if __name__ == '__main__':
    unittest.main()
