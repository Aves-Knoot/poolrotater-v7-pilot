"""Invented-data qualification and selection tests for Taiko production catalogs."""
import hashlib
import sqlite3
import tempfile
from pathlib import Path
import json
import zlib
import unittest

from poolrotater_host.catalog_taiko import (MAP_COLUMNS, _projection, derive_auto_calibration,
                                              CALIBRATION_DICTIONARY, ANALYSIS, CAPABILITIES,
                                              READER, EXCLUDED, _canonical, validate_manifest)
from bot.taiko_difficulty import range_for_pp, target_sr_for_pp
from bot.taiko_pool import TaikoPool


COMMIT = 'b11d0d87824f33984587438074f3791ce06b159b'
ENGINE = 'eb723269bb9affbec0421497848e514ee12194a3e44b536a1bb330d16d8098c4'
CALCULATOR = 'taiko-official-nm-1'
SHA = 'a' * 64
MD5 = 'b' * 32
VARIANT = 'c' * 64


def qualified_result(map_id, original_mode=1, status='ranked', stars=4.0):
    return {
        'phase': 'complete', 'map_id': map_id, 'sha256': SHA,
        'original_mode': original_mode, 'status': status, 'ruleset': 1,
        'converted': original_mode == 0, 'mods': [], 'clock_rate': 1.0,
        'calculator': CALCULATOR, 'difficulty_version': 20251020,
        'source_commit': COMMIT, 'engine': ENGINE, 'source_original_preserved': True,
        'qualification': {
            'judgements_from_official_hit_objects': True,
            'pp_wrapper_matches_direct_difficulty': True,
            'pp_wrapper_matches_direct_performance': True,
            'public_difficulty_matches_skill_replay': True,
        },
        'official': {'attributes': {'StarRating': stars}, 'pp_scenarios': [{'pp': 50.0}]},
        'descriptive': {'duration_seconds': 90.2, 'hit_count': 100, 'big_hits': 2,
                        'centre_hits': 55, 'rim_hits': 45,
                        'peak_hits_per_second': 5, 'mean_hits_per_second': 2.0,
                        'colour_change_fraction': .5, 'maximum_mono_streak': 8},
        'timing': {'finite_bpm_min': 120., 'finite_bpm_max': 180.},
    }


def invented_selection(map_id, original_mode=1, status='ranked'):
    return {'map_id': map_id, 'sha256': SHA, 'original_mode': original_mode,
            'status': status,
            'descriptive': {'duration_seconds': 90.2, 'hit_count': 100, 'big_hits': 2,
                            'centre_hits': 55, 'rim_hits': 45, 'peak_hits_per_second': 5,
                            'mean_hits_per_second': 2.0, 'colour_change_fraction': .5,
                            'maximum_mono_streak': 8},
            'metadata': {'map_id': map_id, 'set_id': 200 + map_id, 'original_mode': original_mode,
                        'status': status, 'artist': 'Test Artist', 'title': 'Test Song',
                        'difficulty_name': 'Oni'}}


def invented_source(map_id):
    map_record = {'file_sha256': {'value': SHA}}
    version_record = {'file_sha256': {'value': SHA}, 'local_md5': {'value': MD5},
                      'native_nm_variant': {'value': VARIANT}}
    return map_record, version_record


def map_tuple(map_id, *, mode=1, sr=4.0, length=100, artist=None, title=None):
    values = {
        'beatmap_id': map_id, 'beatmapset_id': 1000 + map_id,
        'artist': artist or f'Artist {map_id}', 'title': title or f'Title {map_id}',
        'version': 'Oni', 'status': 'ranked', 'original_mode': mode,
        'is_convert': int(mode == 0), 'source_md5': MD5,
        'source_sha256': SHA, 'taiko_nm_variant': VARIANT, 'sr': sr,
        'hit_length': length, 'max_pp': 100.0, 'calculator_id': CALCULATOR,
        'difficulty_version': 20251020, 'source_commit': COMMIT,
        'background_url': None, 'bpm_min': 120.0, 'bpm_max': 180.0,
        'hit_count': 100, 'big_hits': 2, 'centre_hits': 55, 'rim_hits': 45,
        'peak_hits_per_second': 5.0, 'mean_hits_per_second': 2.0,
        'colour_change_fraction': .5, 'maximum_mono_streak': 8,
    }
    return tuple(values[name] for name, _ in MAP_COLUMNS)


class TaikoCatalogProjectionTests(unittest.TestCase):
    def test_native_and_conversion_preserve_ruleset_and_source_identity(self):
        for original_mode in (1, 0):
            row = invented_selection(501 + original_mode, original_mode)
            map_record, version_record = invented_source(501 + original_mode)
            projected = _projection(row, map_record, version_record,
                                     qualified_result(501 + original_mode, original_mode),
                                     {'cover': 'https://assets.example/cover.jpg'})
            self.assertEqual(projected[6], original_mode)
            self.assertEqual(projected[7], int(original_mode == 0))
            self.assertEqual(projected[8], MD5)
            self.assertEqual(projected[11], 4.0)
            self.assertEqual(projected[13], 50.0)

    def test_standard_stars_and_unsupported_mods_are_rejected(self):
        row = invented_selection(502)
        map_record, version_record = invented_source(502)
        result = qualified_result(502)
        result['ruleset'] = 0
        with self.assertRaisesRegex(Exception, 'exact source/difficulty'):
            _projection(row, map_record, version_record, result, None)
        result = qualified_result(502)
        result['mods'] = ['DT']
        with self.assertRaisesRegex(Exception, 'exact source/difficulty'):
            _projection(row, map_record, version_record, result, None)

    def test_source_md5_mismatch_is_not_admitted(self):
        row = invented_selection(503)
        map_record, version_record = invented_source(503)
        version_record['local_md5']['value'] = 'd' * 32
        projected = _projection(row, map_record, version_record,
                                qualified_result(503), None)
        self.assertEqual(projected[8], 'd' * 32)


class TaikoPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / 'catalog.db'
        db = sqlite3.connect(self.db_path)
        columns = ','.join(f'{name} {kind}' + (' PRIMARY KEY' if name == 'beatmap_id' else '')
                           for name, kind in MAP_COLUMNS)
        db.execute(f'CREATE TABLE taiko_maps ({columns})')
        db.execute('CREATE TABLE taiko_auto_calibration (pp REAL PRIMARY KEY, sr REAL NOT NULL, band_floor INTEGER NOT NULL, players INTEGER NOT NULL, scores INTEGER NOT NULL, charts INTEGER NOT NULL)')
        db.executemany('INSERT INTO taiko_maps VALUES (' + ','.join('?' for _ in MAP_COLUMNS) + ')', [
            map_tuple(10, sr=3.5, length=100, artist='Same', title='Song'),
            map_tuple(11, mode=0, sr=4.0, length=120, artist='Same', title='Song'),
            map_tuple(12, sr=5.5, length=180),
        ])
        db.executemany('INSERT INTO taiko_auto_calibration VALUES (?,?,?,?,?,?)', [
            (500., 3.0, 0, 10, 50, 20), (1500., 4.0, 1000, 10, 50, 20),
            (2500., 4.5, 2000, 10, 50, 20)])
        db.commit(); db.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_shared_bundle_validate_inspect_and_real_pool_reader(self):
        from poolrotater_host.catalog_export import _pack, inspect_bundle, validate_manifest as validate_shared
        folder = Path(self.temp.name) / 'bundle'
        folder.mkdir()
        db_path = folder / 'catalog.db'
        db = sqlite3.connect(db_path)
        columns = ','.join(f'{name} {kind}' + (' PRIMARY KEY' if name == 'beatmap_id' else '')
                           for name, kind in MAP_COLUMNS)
        db.execute(f'CREATE TABLE taiko_maps ({columns})')
        db.execute('CREATE TABLE taiko_auto_calibration (pp REAL PRIMARY KEY, sr REAL NOT NULL, band_floor INTEGER NOT NULL, players INTEGER NOT NULL, scores INTEGER NOT NULL, charts INTEGER NOT NULL)')
        db.execute('INSERT INTO taiko_maps VALUES (' + ','.join('?' for _ in MAP_COLUMNS) + ')', map_tuple(20, sr=3.5))
        db.executemany('INSERT INTO taiko_auto_calibration VALUES (?,?,?,?,?,?)',
                       [(500., 3.0, 0, 6, 24, 4), (1500., 4.0, 1000, 6, 24, 5)])
        db.execute('CREATE INDEX taiko_selection_window ON taiko_maps(sr,hit_length,status)')
        db.execute('CREATE INDEX taiko_song_spacing ON taiko_maps(artist,title)')
        db.commit(); db.close()
        row = dict(zip((name for name, _ in MAP_COLUMNS), map_tuple(20, sr=3.5)))
        files = [{'name': 'catalog.db', 'role': 'catalog',
                  'sha256': hashlib.sha256(db_path.read_bytes()).hexdigest(),
                  'bytes': db_path.stat().st_size,
                  'tables': {'taiko_maps': 1, 'taiko_auto_calibration': 2}}]
        ac = {'format': 1, 'kind': 'taiko_nm_topplay_exposure_heuristic',
              'qualification': 'ruleset-1 passed no-mod top scores joined to exact-source official Taiko NM chart stars',
              'convergence': 'research panel accepted at 9000 profiles; profile-panel report does not claim convergence',
              'not_a_claim': 'not an enjoyment, mastery, or skill guarantee; configured range remains fallback when unavailable',
              'profiles_available': 12, 'profiles_with_qualified_nm_scores': 12,
              'coverage_fraction': 1.0, 'minimum_band_profiles': 5,
              'minimum_band_scores': 20, 'point_count': 2}
        analysis = {**ANALYSIS, 'qualification': {
            'native_ranked_approved': 1, 'converted_ranked_approved': 0,
            'official_taiko_nm_only': True, 'mods': [], 'clock_rate': 1.0},
            'auto_calibration': ac, 'exclusion_counts': {},
            'catalog_rows_sha256': hashlib.sha256(_canonical(row)).hexdigest()}
        sources = {name: {'sha256': SHA, 'bytes': 1} for name in
                   ('selection_map_research','analysis_maps','calculation_receipts','source_metadata','profile_panel',
                    'native_coverage','conversion_inputs')}
        sources['source_metadata']['bytes'] = 7 * 1024**3
        archive = _pack(folder, files)
        manifest = {'format': 1, 'export_schema': 1, 'reader': READER, 'mode': 'taiko',
                    'ruleset': 1, 'keys': None, 'files': files, 'sources': sources,
                    'analysis': analysis, 'capabilities': CAPABILITIES,
                    'exclusions': {'revision': 1, 'beatmap_ids': list(EXCLUDED)}, 'archive': archive}
        manifest['content_id'] = hashlib.sha256(__import__('poolrotater_host.catalog_export', fromlist=['canonical']).canonical(manifest)).hexdigest()
        self.assertEqual(validate_manifest(manifest)['mode'], 'taiko')
        validate_shared(manifest)
        (folder / 'manifest.json').write_bytes(__import__('poolrotater_host.catalog_export', fromlist=['canonical']).canonical(manifest))
        inspect_bundle(folder)
        pool = __import__('poolrotater_host.catalog_export', fromlist=['pool_from_bundle']).pool_from_bundle(folder)
        self.assertEqual([m.beatmap_id for m in pool.maps], [20])
        self.assertEqual(pool.target_sr_for_pp(1000), 3.5)

    def test_qualified_variants_and_filters(self):
        pool = TaikoPool(str(self.db_path), sr_min=3.0, sr_max=4.5, max_length=130,
                         taiko_min_length=90)
        self.assertEqual({m.beatmap_id for m in pool.maps}, {10, 11})
        self.assertIsNone(pool.get_variant_map(10, ['DT']))
        self.assertEqual(pool.get_variant_map(11, []), pool.get_map(11))
        self.assertEqual(pool.get_map(11).map_origin, 'taiko-convert-nm')
        self.assertTrue(pool.get_map(11).is_taiko_convert)
        self.assertFalse(pool.get_map(11).is_mania_convert)
        with self.assertRaisesRegex(ValueError, 'unqualified or mismatched'):
            changed = pool.get_map(10)
            changed.sr += .1
            pool.verify_variant(changed)
        self.assertTrue(pool.exclude_beatmap(10) is None)
        self.assertIsNone(pool.get_map(10))

    def test_pick_weights_use_taiko_votes_not_standard_legacy_votes(self):
        from bot.scoped_ratings import ScopedRatingStore
        from bot.rating_outbox import map_key, prepare
        from bot.ratings import RatingStore
        import uuid
        ratings = Path(self.temp.name) / 'ratings.db'
        RatingStore(ratings, initialize=True)
        conn = sqlite3.connect(ratings)
        conn.execute('BEGIN IMMEDIATE')
        prepare(conn, installation_id=str(uuid.uuid4()), generation=str(uuid.uuid4()), capture_started=1.0)
        conn.commit(); conn.close()
        ScopedRatingStore(ratings, initialize=True, ruleset_id=1)
        db = sqlite3.connect(ratings)
        # Standard votes on public ID 10 must not leak into the Taiko selector.
        db.executemany('INSERT INTO map_ratings(beatmap_id,user_id,rating,created_at) VALUES(?,?,?,?)',
                       [(10, 1, 1, 1.0), (10, 2, 1, 1.0), (10, 3, -1, 1.0)])
        # One good and one bad Taiko opinion use the scoped encoded map key.
        key = map_key(10, 1)
        db.executemany('INSERT INTO map_ratings_taiko(beatmap_id,user_id,rating,created_at,map_origin,key_count) VALUES(?,?,?,?,?,?)',
                       [(key, 11, 1, 1.0, 'taiko-native-nm', None),
                        (key, 12, -1, 1.0, 'taiko-native-nm', None)])
        db.commit(); db.close()
        pool = TaikoPool(str(self.db_path), ratings_db=str(ratings),
                         sr_min=3.0, sr_max=4.5, max_length=130)
        tallies = pool._rating_tallies([10, 11])
        self.assertEqual(tallies, {10: (1, 1)})
        neutral = pool._rating_weight(0, 0)
        self.assertGreater(pool._selection_weight(pool.get_map(10), *tallies[10]), neutral)
        self.assertLess(pool._selection_weight(pool.get_map(10), 0, 1), neutral)

    def test_shared_taiko_reader_feedback_changes_pick_weights(self):
        class SharedReader:
            def tallies(self, beatmap_ids):
                return {int(bid): (2, 0) for bid in beatmap_ids if int(bid) == 10}
        pool = TaikoPool(str(self.db_path), ratings_reader=SharedReader(),
                         sr_min=3.0, sr_max=4.5, max_length=130)
        tallies = pool._rating_tallies([10, 11])
        self.assertEqual(tallies, {10: (2, 0)})
        self.assertGreater(pool._selection_weight(pool.get_map(10), *tallies[10]),
                           pool._rating_weight(0, 0))

    def test_length_window_and_sparse_failure_preserve_current_pool(self):
        pool = TaikoPool(str(self.db_path), sr_min=3.0, sr_max=4.5, max_length=130)
        before = {m.beatmap_id for m in pool.maps}
        self.assertEqual(pool.set_range(5.0, 6.0), 0)
        self.assertEqual({m.beatmap_id for m in pool.maps}, before)
        self.assertEqual(pool.set_window(3.0, 4.5, 80), 0)
        self.assertEqual({m.beatmap_id for m in pool.maps}, before)

    def test_auto_calibration_uses_only_exact_unmodded_taiko_scores_and_aggregate_output(self):
        panel = Path(self.temp.name) / 'panel.db'
        db = sqlite3.connect(panel)
        db.execute('CREATE TABLE players(uid INTEGER PRIMARY KEY,pp REAL,band INTEGER,phase TEXT)')
        db.execute('CREATE TABLE score_pages(uid INTEGER,offset INTEGER,payload BLOB,source_sha256 TEXT,PRIMARY KEY(uid,offset))')
        for uid, pp, band, sr, bid in [(i, 500., 0, 3.0, 10) for i in range(1, 7)] + [(i, 1500., 1000, 4.0, 11) for i in range(7, 13)]:
            db.execute('INSERT INTO players VALUES(?,?,?,?)', (uid, pp, band, 'complete'))
            scores = ([{'ruleset_id': 1, 'passed': True, 'mods': [], 'beatmap_id': bid,
                        'beatmap': {'mode': 'taiko', 'checksum': MD5}} for _ in range(4)] +
                      [{'ruleset_id': 1, 'passed': True, 'mods': [{'acronym': 'DT'}], 'beatmap_id': bid,
                        'beatmap': {'mode': 'taiko', 'checksum': MD5}}])
            raw = json.dumps(scores, separators=(',', ':')).encode()
            compressor = zlib.compressobj(zdict=CALIBRATION_DICTIONARY)
            blob = b'C3J1' + compressor.compress(raw) + compressor.flush()
            db.execute('INSERT INTO score_pages VALUES(?,?,?,?)',
                       (uid, 0, blob, hashlib.sha256(blob).hexdigest()))
        db.commit(); db.close()
        maps = [{'beatmap_id': 10, 'source_md5': MD5, 'sr': 3.0},
                {'beatmap_id': 11, 'source_md5': MD5, 'sr': 4.0}]
        result = derive_auto_calibration(panel, maps)
        self.assertEqual(result['profiles_available'], 12)
        self.assertEqual(result['profiles_with_qualified_nm_scores'], 12)
        self.assertEqual(result['point_count'], 2)
        self.assertEqual(result['points'][0]['players'], 6)
        self.assertEqual(result['points'][0]['scores'], 24)
        self.assertEqual([p['sr'] for p in result['points']], [3.0, 4.0])
        self.assertNotIn('uid', json.dumps(result))

    def test_taiko_pp_calibration_interpolates_and_falls_back_cleanly(self):
        points = ((500., 3.), (1500., 4.), (2500., 4.5))
        self.assertEqual(target_sr_for_pp(1000., points), 3.5)
        self.assertEqual(target_sr_for_pp(99999., points), 4.5)
        self.assertEqual(range_for_pp(1000., points, half_width=.4), (3.1, 3.9))
        self.assertIsNone(target_sr_for_pp(1000., ()))
        self.assertIsNone(range_for_pp(1000., (), half_width=.4))


if __name__ == '__main__':
    unittest.main()
