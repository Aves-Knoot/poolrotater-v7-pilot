import unittest
from types import SimpleNamespace

from poolrotater_host.bookings.selection import (
    SelectionError,
    SelectionPreview,
    UnsupportedFilterError,
    apply_booking_settings,
    apply_candidates,
    clear_selection,
    install_selection,
    preview,
    validate_filters,
    weights,
)


def candidate(beatmap_id, *, ar=None, length=120, is_dt=False, clock_rate=1.0, sr=4.0):
    return SimpleNamespace(beatmap_id=beatmap_id, approach_rate=ar, hit_length=length,
                           is_dt=is_dt, clock_rate=clock_rate, sr=sr,
                           title=f'private title {beatmap_id}')


class NativePoolFake:
    def __init__(self, maps):
        self.maps = list(maps)
        self._recent = []
        self._playlist_reserved = set()

    def next_map(self):
        if not self.maps:
            return None
        item = self.maps[0]
        self._recent.append(item.beatmap_id)
        return item


class MixedDTFake(NativePoolFake):
    mixed_dt = True

    def __init__(self, maps):
        super().__init__(maps)
        self.dt_override = None
        self.dt_full = False
        self._last_dt_pick = False
        self.crash_count = 0

    def dt_enabled(self):
        return self.dt_override is not False

    def dt_catalog_ready(self):
        return True

    def set_dt_override(self, enabled, *, full=False):
        self.dt_override, self.dt_full = enabled, full

    def _selection_weight(self, item, good, bad):
        return float(item.native_weight)

    def _weighted_pick(self, candidates):
        nm = [item for item in candidates if not item.is_dt]
        dt = [item for item in candidates if item.is_dt and self.dt_enabled()]
        group = dt if dt and not nm else nm
        item = group[0] if group else None
        # Mirrors StandardPool's `item.is_dt` receipt update if a bad empty
        # input makes it past its ordinary next_map candidate guard.
        if item is None:
            self.crash_count += 1
            return item.is_dt
        self._last_dt_pick = item.is_dt
        return item

    def next_map(self):
        candidates = [item for item in self.maps if not item.is_dt or self.dt_enabled()]
        return self._weighted_pick(candidates)


class ManiaFallbackFake(NativePoolFake):
    mania_mode = True
    mania_style_fallback_enabled = True
    mania_source_mode = 'noodle'

    def __init__(self, maps, fallback_rows):
        super().__init__(maps)
        self.fallback_rows = list(fallback_rows)

    def _rows(self, beatmap_id=None, *, ignore_style=False):
        return self.fallback_rows

    def next_map(self):
        if self.maps:
            return self.maps[0]
        rows = self._rows(ignore_style=True)
        return rows[0] if rows else None


class DeterministicRandom:
    def __init__(self):
        self.last_weights = None

    def choices(self, population, weights=None, *, cum_weights=None, k=1):
        self.last_weights = list(weights or [1] * len(population))
        return [population[max(range(len(population)), key=lambda i: self.last_weights[i])]]


class CatchPoolFake(NativePoolFake):
    catch_mode = True

    def __init__(self, maps):
        super().__init__(maps)
        self._all_maps = {item.beatmap_id: item for item in maps}
        self._base_random = DeterministicRandom()
        self.selector = SimpleNamespace()

    def next_map(self):
        values = [item.selection_reference for item in self.maps]
        return self._base_random.choices(values, weights=[1.0] * len(values), k=1)[0] if values else None


class BookingSelectionTests(unittest.TestCase):
    def test_hard_filter_excludes_out_of_range_and_unknown_metadata(self):
        values = [candidate(1, ar=7), candidate(2, ar=9), candidate(3, ar=None)]
        selected = apply_candidates(values, {'ar': {'min': 6, 'max': 8, 'mode': 'hard'}}, 'standard-fixed')
        self.assertEqual([item.beatmap_id for item in selected], [1])
        self.assertEqual(len(values), 3)

    def test_dt_length_is_already_clock_adjusted(self):
        values = [candidate(1, ar=9, length=100, is_dt=True, clock_rate=1.5)]
        selected = apply_candidates(values, {'length': {'min': 95, 'max': 105, 'mode': 'hard'}}, 'standard-fixed')
        self.assertEqual(selected, values)
        self.assertEqual(weights(values, {'length': {'min': 95, 'max': 105, 'mode': 'prefer'}}, 'standard-fixed'), [2.0])

    def test_preference_weights_do_not_exclude_outside_or_unknown(self):
        values = [candidate(1, ar=7), candidate(2, ar=9), candidate(3, ar=None)]
        filters = {'ar': {'min': 6, 'max': 8, 'mode': 'prefer'}}
        self.assertEqual(apply_candidates(values, filters, 'standard-fixed'), values)
        self.assertEqual(weights(values, filters, 'standard-fixed'), [2.0, 1.0, 1.0])

    def test_hr_ez_and_dt_transform_base_ar_once(self):
        base = [candidate(1, ar=9), candidate(2, ar=8), candidate(3, ar=9, is_dt=True, clock_rate=1.5)]
        hr_capped = apply_candidates(base[:1], {'ar': {'min': 10, 'max': 10, 'mode': 'hard'}},
                                     'standard-fixed', ('HR',))
        self.assertEqual(len(hr_capped), 1)
        hr = apply_candidates([candidate(4, ar=7)], {'ar': {'min': 9.7, 'max': 9.9, 'mode': 'hard'}},
                              'standard-fixed', ('HR',))
        self.assertEqual(len(hr), 1)
        ez = apply_candidates(base[1:2], {'ar': {'min': 3.9, 'max': 4.1, 'mode': 'hard'}},
                              'standard-fixed', ('EZ',))
        self.assertEqual(len(ez), 1)
        dt = apply_candidates(base[2:], {'ar': {'min': 10.3, 'max': 10.4, 'mode': 'hard'}},
                              'standard-fixed')
        self.assertEqual(len(dt), 1)
        dt_required = apply_candidates([candidate(5, ar=9)],
                                       {'ar': {'min': 10.3, 'max': 10.4, 'mode': 'hard'}},
                                       'standard-fixed', ({'acronym': 'DT'},))
        self.assertEqual(len(dt_required), 1)

    def test_mania_rejects_ar_but_allows_length(self):
        with self.assertRaises(UnsupportedFilterError):
            validate_filters({'ar': {'min': 5, 'max': 8, 'mode': 'hard'}}, 'mania-4k')
        values = [candidate(1, ar=None, length=80), candidate(2, ar=None, length=140)]
        self.assertEqual([m.beatmap_id for m in apply_candidates(
            values, {'length': {'min': 60, 'max': 100, 'mode': 'hard'}}, 'mania-4k')], [1])

    def test_catch_ar_is_not_advertised_without_qualified_catalog_metadata(self):
        with self.assertRaises(UnsupportedFilterError):
            validate_filters({'ar': {'min': 5, 'max': 7, 'mode': 'hard'}}, 'catch-fixed')
        values = [candidate(1, ar=6, length=90), candidate(2, ar=None, length=180)]
        self.assertEqual([m.beatmap_id for m in apply_candidates(
            values, {'length': {'min': 60, 'max': 120, 'mode': 'hard'}}, 'catch-fixed')], [1])

    def test_od_and_bpm_fail_with_explicit_unavailable_reason(self):
        for key in ('od', 'bpm'):
            with self.subTest(key=key), self.assertRaises(UnsupportedFilterError) as caught:
                validate_filters({key: {'min': 2, 'max': 6, 'mode': 'hard'}}, 'standard-fixed')
            self.assertIn('unavailable', str(caught.exception))

    def test_preview_is_bounded_title_free_and_does_not_mutate_pool(self):
        values = [candidate(index, ar=7, length=90 + index) for index in range(1, 9)]
        pool = SimpleNamespace(maps=values)
        before = list(pool.maps)
        result = preview(pool, {'length': {'min': 0, 'max': 3600, 'mode': 'prefer'}}, 'standard-fixed')
        self.assertEqual(result['count'], 8)
        self.assertLessEqual(len(result['samples']), 5)
        self.assertNotIn('title', result['samples'][0])
        self.assertEqual(pool.maps, before)
        facade = SelectionPreview({'standard-fixed': object()})
        self.assertEqual(facade.preview(pool, {}, 'standard-fixed')['count'], 8)
        with self.assertRaises(SelectionError):
            facade.preview(pool, {}, 'mania-4k')

    def test_invalid_modes_and_ranges_are_rejected(self):
        with self.assertRaises(SelectionError):
            validate_filters({'length': {'min': 5, 'max': 1, 'mode': 'hard'}}, 'standard-fixed')
        with self.assertRaises(SelectionError):
            validate_filters({'length': {'min': 1, 'max': 5, 'mode': 'soft'}}, 'standard-fixed')

    def test_native_standard_wrapper_filters_before_native_pick_and_multiplies_weight(self):
        preferred = candidate(1, ar=7, length=80)
        outside = candidate(2, ar=9, length=80)
        preferred.native_weight = 3
        outside.native_weight = 4
        pool = MixedDTFake([preferred, outside])
        count = install_selection(pool, {
            'selection_filters': {'ar': {'min': 6, 'max': 8, 'mode': 'prefer'}},
        }, 'standard-fixed')
        self.assertEqual(count, 2)
        self.assertEqual(pool._selection_weight(preferred, 0, 0), 6.0)
        self.assertEqual(pool._selection_weight(outside, 0, 0), 4.0)
        hard_count = install_selection(pool, {
            'selection_filters': {'ar': {'min': 6, 'max': 8, 'mode': 'hard'}},
        }, 'standard-fixed')
        self.assertEqual(hard_count, 1)
        self.assertEqual(pool.next_map().beatmap_id, 1)
        clear_selection(pool)
        self.assertEqual(pool.next_map().beatmap_id, 1)

    def test_mixed_dt_empty_guard_returns_none_without_native_none_crash(self):
        dt_only = candidate(10, ar=7, length=100, is_dt=True)
        pool = MixedDTFake([dt_only])
        self.assertEqual(install_selection(pool, {'dt_mode': 'off'}, 'standard-fixed'), 0)
        self.assertIsNone(pool.next_map())
        self.assertEqual(pool.crash_count, 0)

    def test_mania_fallback_rows_remain_hard_filtered(self):
        fallback = {'beatmap_id': 4, 'hit_length': 80, 'sr': 4.0}
        outside = {'beatmap_id': 5, 'hit_length': 180, 'sr': 4.0}
        pool = ManiaFallbackFake([], [fallback, outside])
        self.assertEqual(install_selection(pool, {
            'selection_filters': {'length': {'min': 60, 'max': 100, 'mode': 'hard'}},
        }, 'mania-4k'), 1)
        self.assertEqual(pool.next_map(), fallback)
        self.assertEqual(pool._rows(ignore_style=True), [fallback])

    def test_catch_native_feedback_and_duration_weights_are_multiplied_after_bucket_scaling(self):
        preferred = candidate(20, ar=6, length=90)
        outside = candidate(21, ar=9, length=180)
        preferred.selection_reference = {'map_id': 20}
        outside.selection_reference = {'map_id': 21}
        pool = CatchPoolFake([preferred, outside])
        self.assertEqual(install_selection(pool, {
            'selection_filters': {'length': {'min': 60, 'max': 120, 'mode': 'prefer'}},
        }, 'catch-fixed'), 2)
        self.assertEqual(pool.next_map()['map_id'], 20)
        self.assertEqual(pool._base_random.last_weights, [2.0, 1.0])

    def test_apply_booking_settings_stages_and_swaps_only_after_valid_materialization(self):
        old = NativePoolFake([candidate(30, length=60)])
        bot = SimpleNamespace(cfg={'ruleset_id': 0, 'sr_min': 3.5, 'sr_max': 4.5,
                                   'auto_difficulty': False, '_supervisor_overrides': {}},
                              pool=old, manual_range=True)
        seen = {}

        def materialize(staged):
            seen.update(staged)
            return NativePoolFake([candidate(31, length=80), candidate(32, length=180)])

        result = apply_booking_settings(bot, {
            'selection_filters': {'length': {'min': 60, 'max': 100, 'mode': 'hard'}},
        }, 'standard-fixed', materialize=materialize)
        self.assertEqual(result['application'], 'next_map')
        self.assertEqual(result['candidate_count'], 1)
        self.assertIsNot(bot.pool, old)
        self.assertEqual(bot.pool.next_map().beatmap_id, 31)
        self.assertIn('selection_filters', seen['_supervisor_overrides'])
        self.assertEqual(bot._booking_session_settings['selection_filters']['length']['max'], 100)

        current_cfg = dict(bot.cfg)
        current_pool = bot.pool
        with self.assertRaises(SelectionError):
            apply_booking_settings(bot, {
                'selection_filters': {'length': {'min': 0, 'max': 10, 'mode': 'hard'}},
            }, 'standard-fixed', materialize=lambda cfg: NativePoolFake([candidate(33, length=90)]))
        self.assertEqual(bot.cfg, current_cfg)
        self.assertIs(bot.pool, current_pool)


if __name__ == '__main__':
    unittest.main()
