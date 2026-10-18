"""Network statistics use full owner rows before public top-ranking truncation."""
from __future__ import annotations
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from website.statistics_v71.network_statistics import compose, empty_player, groups, read_snapshot, union_seconds

NOW = 2_000_000_000


def fixture():
    return {"format": 1, "generated_at": NOW,
            "hosts": [{"host_id": "alpha", "label": "Alpha", "consent_at": NOW - 1000, "updated_at": NOW, "enabled": True},
                      {"host_id": "beta", "label": "Beta", "consent_at": NOW - 1000, "updated_at": NOW, "enabled": True}],
            "presence": [], "joins": [], "matches": [], "hidden_user_ids": []}


def presence(host="alpha", uid=1, start=NOW-200, stop=NOW-100, **kw):
    return dict(host_id=host, user_id=uid, room_id=20, mode="standard", username=f"Community {uid}", started_at=start, ended_at=stop, **kw)


def match(host="alpha", uid=1, key="room:20:item:1", **kw):
    return dict(host_id=host, user_id=uid, room_id=20, mode="standard", username=f"Community {uid}",
                match_key=key, beatmap_id=10, started_at=NOW-200, ended_at=NOW-100, score_submitted=True, accuracy=95, **kw)


def owner(uid=1):
    rows = groups()
    p = empty_player(uid)
    p.update(username="Owner", seconds=5000, maps=10, pp=55, unique={10, 11}, days={20000})
    rows["all"]["all"][uid] = p
    rows["standard"]["all"][uid] = deepcopy(p)
    return rows


class NetworkStatisticsMergeTests(unittest.TestCase):
    def compose(self, data=None, raw=None, hidden=(), ids=()):
        return compose(raw or owner(), {1: "Owner"}, hidden, data, set(ids), NOW)

    def test_same_user_is_unique_across_owner_and_two_hosts_and_overlap_union(self):
        data = fixture()
        data['presence'] = [presence(), presence('beta', start=NOW-150, stop=NOW-50)]
        combined, extra = self.compose(data)
        self.assertEqual(combined['all']['all']['totals']['players'], 1)
        self.assertEqual(combined['all']['all']['totals']['seconds'], 5150)
        self.assertEqual(combined['all']['all']['totals']['verified_seconds'], 150)
        self.assertEqual(combined['all']['all']['totals']['legacy_owner_seconds'], 5000)
        self.assertEqual(extra['hosts']['owner']['scopes']['all']['all']['totals']['seconds'], 5000)
        self.assertEqual(extra['hosts']['alpha']['scopes']['all']['all']['totals']['seconds'], 100)
        self.assertEqual(combined['all']['all']['leaders'][0]['username'], 'Owner')

    def test_retries_cross_host_same_match_and_same_room_owner_records_do_not_multiply(self):
        data = fixture()
        data['matches'] = [match(), match(), match('beta')]
        combined, extra = self.compose(data)
        self.assertEqual(combined['all']['all']['totals']['maps'], 11)
        self.assertEqual(extra['hosts']['alpha']['scopes']['all']['all']['totals']['maps'], 1)
        combined, _ = self.compose(data, ids=(20,))
        self.assertEqual(combined['all']['all']['totals']['maps'], 10)

    def test_privacy_from_either_owner_or_remote_hides_every_view_but_counts_totals(self):
        for source in ('owner', 'remote'):
            with self.subTest(source=source):
                data = fixture()
                data['presence'] = [presence()]
                if source == 'remote': data['hidden_user_ids'] = [1]
                combined, extra = self.compose(data, hidden=(1,) if source == 'owner' else ())
                self.assertEqual(combined['all']['all']['totals']['players'], 1)
                for view in [combined, *[h['scopes'] for h in extra['hosts'].values()]]:
                    self.assertEqual(view['all']['all']['leaders'], [])
                text = json.dumps({'scopes': combined, **extra})
                self.assertNotIn('"user_id": 1', text)
                self.assertNotIn('Community 1', text)

    def test_disabled_and_unbound_hosts_and_preconsent_data_excluded(self):
        data = fixture()
        data['hosts'][0]['enabled'] = False
        data['presence'] = [presence(), presence('unknown'), presence('beta', start=NOW-5000, stop=NOW-2000)]
        combined, extra = self.compose(data)
        self.assertEqual(combined['all']['all']['totals']['seconds'], 5000)
        self.assertEqual(combined['all']['all']['totals']['verified_seconds'], 0)
        self.assertNotIn('alpha', extra['hosts'])

    def test_intervals_clip_at_consent_now_and_period_boundary(self):
        data = fixture()
        data['presence'] = [presence(start=NOW-5000, stop=NOW+100)]
        combined, extra = self.compose(data)
        self.assertEqual(combined['all']['all']['totals']['seconds'], 6000)
        self.assertEqual(combined['all']['all']['totals']['verified_seconds'], 1000)
        data['hosts'][0]['consent_at'] = NOW - 15*86400
        data['presence'] = [presence(start=NOW-8*86400, stop=NOW-6*86400)]
        combined, _ = self.compose(data)
        self.assertEqual(combined['all']['7d']['totals']['seconds'], 86400)

    def test_no_pp_is_fabricated_for_community_scores(self):
        data = fixture()
        data['matches'] = [match(uid=2)]
        combined, extra = self.compose(data)
        self.assertEqual(combined['all']['all']['totals']['pp'], 55)
        remote = extra['hosts']['alpha']['scopes']['all']['all']
        self.assertEqual(remote['totals']['pp'], 0)
        self.assertEqual(remote['totals']['scores'], 1)
        self.assertEqual(remote['leaders'][0]['best_gain'], None)
        self.assertEqual(extra['coverage']['pp_basis'], 'owner_recorded_achievements_only')

    def test_nonfinite_and_unknown_modes_are_discarded(self):
        data = fixture()
        data['presence'] = [presence(start=float('nan')), {**presence(), 'mode': 'mystery'}, {**presence(), 'user_id': True}]
        combined, _ = self.compose(data)
        self.assertEqual(combined['all']['all']['totals']['seconds'], 5000)
        self.assertEqual(combined['all']['all']['totals']['verified_seconds'], 0)
        json.dumps(combined, allow_nan=False)

    def test_joins_deduplicate_and_empty_opted_in_host_remains_visible(self):
        data = fixture()
        row = {**presence(), 'occurred_at': NOW-200, 'event_id': 'join-1'}
        data['joins'] = [row, row]
        combined, extra = self.compose(data)
        self.assertEqual(combined['all']['all']['leaders'][0]['visits'], 1)
        self.assertIn('beta', extra['hosts'])

    def test_combined_rankings_merge_all_owner_players_before_top50_limit(self):
        rows = groups()
        for uid in range(1, 102):
            p = empty_player(uid)
            p.update(maps=102-uid, unique={uid})
            rows['all']['all'][uid] = p
        data = fixture()
        data['matches'] = [match(uid=101, key=f'room:20:item:{n}') for n in range(200)]
        combined, _ = self.compose(data, raw=rows)
        self.assertEqual(combined['all']['all']['totals']['players'], 101)
        rank = sorted(combined['all']['all']['leaders'], key=lambda p: -p['maps'])
        self.assertEqual(rank[0]['user_id'], 101)
        self.assertEqual(rank[0]['maps'], 201)

    def test_large_native_sets_are_counted_without_duplicate_full_history_copies(self):
        import tracemalloc
        raw=groups()
        for uid in range(1,1201):
            p=empty_player(uid);p.update(maps=uid,unique=set(range(uid,uid+256)),days={1,2})
            raw['all']['all'][uid]=p
        tracemalloc.start()
        try:
            combined,extra=self.compose(raw=raw)
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertEqual(combined['all']['all']['totals']['players'],1200)
        self.assertLess(peak,12*1024**2)
        self.assertEqual(raw['all']['all'][1200]['unique'],set(range(1200,1456)))
        data=fixture();data['matches']=[{**match(uid=1200),'beatmap_id':999999}]
        merged,_=self.compose(data,raw=raw)
        self.assertNotIn(999999,raw['all']['all'][1200]['unique'])
        selected=next(p for p in merged['all']['all']['leaders']if p['user_id']==1200)
        self.assertEqual(selected['unique_maps'],257)

    def test_mode_scopes_and_unique_map_set_union(self):
        data = fixture()
        data['presence'] = [{**presence(uid=2), 'mode': 'mania4'}]
        data['matches'] = [{**match(), 'beatmap_id': 11}]
        combined, extra = self.compose(data)
        self.assertEqual(combined['all']['all']['leaders'][0]['unique_maps'], 2)
        self.assertEqual(combined['mania_4k']['all']['totals']['seconds'], 100)
        self.assertEqual(extra['hosts']['alpha']['scopes']['standard']['all']['totals']['maps'], 1)

    def test_missing_network_keeps_legacy_history_without_claiming_verified_time(self):
        combined, extra = self.compose()
        self.assertFalse(extra['coverage']['network_available'])
        self.assertEqual(combined['all']['all']['totals']['maps'], 10)
        self.assertEqual(combined['all']['all']['totals']['seconds'], 5000)
        self.assertEqual(combined['all']['all']['totals']['verified_seconds'], 0)
        self.assertEqual(combined['all']['all']['totals']['legacy_owner_seconds'], 5000)

    def test_union_handles_nested_disjoint_touching_and_out_of_order_intervals(self):
        self.assertEqual(union_seconds([(5, 8), (0, 10), (8, 20), (30, 40), (20, 25)]), 35)

    def test_local_snapshot_missing_stale_or_malformed_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'network.json'
            with patch.dict(os.environ, {'POOLROTATER_NETWORK_STATISTICS_SNAPSHOT': str(path)}):
                self.assertIsNone(read_snapshot(NOW))
                for obj in ({}, {**fixture(), 'generated_at': NOW-601}, {**fixture(), 'generated_at': NOW+1}, {**fixture(), 'presence': {}}):
                    path.write_text(json.dumps(obj))
                    path.chmod(0o640)
                    self.assertIsNone(read_snapshot(NOW))
                path.write_text(json.dumps(fixture()))
                path.chmod(0o640)
                self.assertEqual(read_snapshot(NOW), fixture())

    def test_existing_tracked_pp_achievements_are_shared_deduplicated_and_owner_duplicates_excluded(self):
        data = fixture()
        data['coverage'] = {'tracked_pp_supported': True}
        base = dict(host_id='alpha', user_id=2, username='Tracked Player', room_id=20,
                    playlist_item_id=40, mode='standard', achievement_key='original',
                    beatmap_id=10, created_at=NOW-100, profile_gain_pp=12.5,
                    score_pp=220, accuracy=98.5, top_rank=7, historical=False)
        data['achievements'] = [base, base, {**base, 'host_id': 'beta', 'achievement_key': 'reimported-key'}]
        combined, extra = self.compose(data)
        self.assertEqual(combined['all']['all']['totals']['pp'], 67.5)
        self.assertEqual(extra['hosts']['alpha']['scopes']['all']['all']['totals']['pp'], 12.5)
        self.assertEqual(extra['coverage']['pp_basis'], 'observed_tracked_achievements')
        combined, _ = self.compose(data, ids=(20,))
        self.assertEqual(combined['all']['all']['totals']['pp'], 55)

    def test_reviewed_historical_pp_retains_original_date_and_unknown_mania_not_invented_as_4k(self):
        data = fixture()
        data['coverage'] = {'tracked_pp_supported': True}
        data['achievements'] = [dict(host_id='alpha', user_id=2, username='Historic Player', room_id=20,
                    playlist_item_id=None, mode='mania', achievement_key='native-history',
                    beatmap_id=10, created_at=NOW-2000, profile_gain_pp=15,
                    score_pp=200, accuracy=98, top_rank=8, historical=True)]
        combined, extra = self.compose(data)
        self.assertEqual(combined['all']['all']['totals']['pp'], 70)
        self.assertEqual(combined['mania_4k']['all']['totals']['pp'], 0)
        self.assertEqual(combined['mania_7k']['all']['totals']['pp'], 0)
        self.assertTrue(extra['coverage']['pre_opt_in_community_history'])

    def test_cumulative_imports_replace_and_are_separate_from_verified_time(self):
        data = fixture()
        baseline = dict(host_id='alpha', source_id='source-1', user_id=2, username='History',
                        mode='standard', total_seconds=5000, visit_count=3, cutoff=NOW-150)
        data['baselines'] = [baseline, baseline, {**baseline, 'total_seconds': 5500, 'cutoff': NOW-100},
                             {**baseline, 'host_id': 'beta', 'total_seconds': 5500, 'cutoff': NOW-100}]
        data['presence'] = [presence(uid=2, start=NOW-200, stop=NOW-50)]
        combined, extra = self.compose(data)
        self.assertEqual(combined['all']['all']['totals']['legacy_community_seconds'], 5500)
        self.assertEqual(combined['all']['all']['totals']['seconds'], 10550)
        self.assertEqual(combined['all']['all']['totals']['verified_seconds'], 50)
        self.assertEqual(extra['hosts']['alpha']['scopes']['all']['all']['totals']['legacy_community_seconds'], 5500)
        data['owner_host_ids'] = ['alpha', 'beta']
        combined, _ = self.compose(data)
        self.assertEqual(combined['all']['all']['totals']['legacy_community_seconds'], 0)

    def test_snapshot_reader_refuses_symlinks_world_readable_files_and_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'network.json'
            outside = Path(tmp)/'outside.json'
            outside.write_text(json.dumps(fixture()))
            outside.chmod(0o640)
            with patch.dict(os.environ, {'POOLROTATER_NETWORK_STATISTICS_SNAPSHOT': str(path)}):
                path.symlink_to(outside)
                self.assertIsNone(read_snapshot(NOW))
                path.unlink()
                path.write_text(json.dumps(fixture()))
                path.chmod(0o644)
                self.assertIsNone(read_snapshot(NOW))
                path.unlink()
                os.mkfifo(path)
                self.assertIsNone(read_snapshot(NOW))

    def test_no_mutation_of_owner_input(self):
        raw = owner()
        before = deepcopy(raw)
        self.compose(fixture(), raw=raw)
        self.assertEqual(raw, before)


if __name__ == '__main__':
    unittest.main()
