from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import json
import copy
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from poolrotater_host.bookings.preview import (
    ExportPreviewProvider,
    PreviewProvider,
    _native_pool_factory,
)
from poolrotater_host.bookings.selection import SelectionError


def item(beatmap_id, *, ar=7.0, length=100, sr=4.0, is_dt=False, clock_rate=1.0):
    return SimpleNamespace(beatmap_id=beatmap_id, approach_rate=ar,
                           hit_length=length, sr=sr, is_dt=is_dt,
                           clock_rate=clock_rate)


class PreviewPool:
    def __init__(self, cfg):
        self.maps = [item(1, length=90), item(2, length=180), item(3, ar=None, length=120)]
        self.cfg = cfg
        self.source_mode = 'variety'
        self._recent = []

    def set_style_selection(self, _styles):
        return len(self.maps)

    def next_map(self):
        if not self.maps:
            return None
        value = self.maps[0]
        self._recent.append(value.beatmap_id)
        return value


class PreviewProviderTests(unittest.TestCase):
    def test_cache_reuses_materialized_native_pool_and_preview_is_pure(self):
        now = [0.0]
        factory_calls = []
        pools = []

        def configs(preset):
            return {'ruleset_id': 0, 'sr_min': 3.5, 'sr_max': 4.5,
                    'auto_difficulty': False, '_preset': preset}

        def factory(cfg):
            factory_calls.append(cfg)
            pool = PreviewPool(cfg)
            pools.append(pool)
            return pool

        provider = PreviewProvider(configs, factory, clock=lambda: now[0], ttl_seconds=60)
        settings = {'selection_filters': {
            'length': {'min': 60, 'max': 120, 'mode': 'hard'},
        }}
        first = provider('standard-fixed', settings)
        before = list(pools[0].maps)
        second = provider('standard-fixed', settings)
        self.assertEqual(first, {'count': 2, 'samples': [
            {'beatmap_id': 1, 'sr': 4.0, 'hit_length': 90},
            {'beatmap_id': 3, 'sr': 4.0, 'hit_length': 120},
        ]})
        self.assertEqual(second, first)
        self.assertEqual(len(factory_calls), 1)
        self.assertEqual(pools[0].maps, before)
        self.assertEqual(pools[0]._recent, [])

        now[0] = 61
        provider('standard-fixed', settings)
        self.assertEqual(len(factory_calls), 2)

    def test_configured_pool_cache_evicts_before_build_and_reuses_hot_entry(self):
        now = [0.0]
        provider = None
        factory_cache_sizes = []
        pools = []

        def factory(cfg):
            factory_cache_sizes.append(len(provider._cache))
            pool = PreviewPool(cfg)
            pools.append(pool)
            return pool

        provider = PreviewProvider(
            lambda preset: {'ruleset_id': 0, 'sr_min': 3.5, 'sr_max': 4.5,
                            'auto_difficulty': False, '_preset': preset},
            factory, clock=lambda: now[0])
        self.assertEqual(provider.max_entries, 2)
        first = {'sr_min': 3.2}
        second = {'sr_min': 3.3}
        third = {'sr_min': 3.4}
        provider('standard-fixed', first)
        now[0] = 1
        provider('standard-fixed', second)
        now[0] = 2
        provider('standard-fixed', first)  # promote first; second becomes LRU
        now[0] = 3
        provider('standard-fixed', third)

        self.assertEqual(factory_cache_sizes, [0, 1, 1])
        self.assertEqual(len(provider._cache), 2)
        self.assertNotIn(pools[1], [entry['pool'] for entry in provider._cache.values()])
        now[0] = 4
        provider('standard-fixed', first)
        self.assertEqual(len(pools), 3, 'a hot cached pool must avoid another native factory query')

    def test_export_base_pool_cache_evicts_before_build_and_reuses_hot_entry(self):
        now = [0.0]
        provider = None
        factory_cache_sizes = []
        pools = []

        def factory(cfg):
            factory_cache_sizes.append(len(provider._base_pool_cache))
            pool = PreviewPool(cfg)
            pools.append(pool)
            return pool

        with tempfile.TemporaryDirectory() as temporary:
            provider = ExportPreviewProvider(
                temporary, pool_factory=factory, clock=lambda: now[0])
            self.assertEqual(provider.max_entries, 2)

            def prepare(preset, _settings):
                return ({'ruleset_id': 0, 'sr_min': 3.5, 'sr_max': 4.5,
                         '_preset': preset},
                        {'selection_filters': {}, 'required_mods': []},
                        f'base-{preset}')

            provider._prepare = prepare
            provider._configure_pool = lambda pool, _staged, _effective, _preset: pool
            provider('standard-fixed', {})
            now[0] = 1
            provider('standard-auto', {})
            now[0] = 2
            provider('standard-fixed', {})  # promote fixed; auto becomes LRU
            now[0] = 3
            provider('standard-loved', {})

            self.assertEqual(factory_cache_sizes, [0, 1, 1])
            self.assertEqual(len(provider._base_pool_cache), 2)
            self.assertNotIn(pools[1], [entry['pool'] for entry in provider._base_pool_cache.values()])
            now[0] = 4
            provider('standard-fixed', {})
            self.assertEqual(len(pools), 3, 'a hot base pool must avoid another native factory query')

            # Expiration still removes stale large pools even when the next
            # request uses a different preset key.
            now[0] = 64
            provider('mania-4k', {})
            self.assertEqual(factory_cache_sizes[-1], 0)

    def test_provider_validates_player_patch_and_does_not_accept_untrusted_paths(self):
        provider = PreviewProvider(
            lambda _preset: {'ruleset_id': 0, 'sr_min': 3.5, 'sr_max': 4.5,
                             'auto_difficulty': False},
            PreviewPool,
        )
        with self.assertRaises(SelectionError):
            provider('standard-fixed', {'unknown_config_path': '/tmp/catalog.db'})
        with self.assertRaises(SelectionError):
            provider('mania-4k', {'selection_filters': {
                'ar': {'min': 5, 'max': 7, 'mode': 'hard'},
            }})

    def test_full_catalog_exports_build_bounded_native_previews(self):
        validation_root = next((path for path in Path(__file__).resolve().parents
                                if path.name == 'validation'), None)
        source_root = validation_root / 'loved726' if validation_root else None
        if not source_root or not source_root.exists():
            self.skipTest('offline catalog fixtures are unavailable')
        bundles = {
            'standard': source_root / 'inputs' / 'standard',
            'mania4': source_root / 'inputs' / 'mania4',
            'mania7': source_root / 'inputs' / 'mania7',
            'catch': source_root / 'inputs' / 'catch',
            'loved': source_root / 'prepared' / 'loved',
        }
        if not all(path.is_dir() for path in bundles.values()):
            self.skipTest('complete native catalog fixture set is unavailable')

        # The checked-in validation inputs carry a harmless publication marker
        # alongside their bundle. Hard links make clean inspect_bundle roots
        # without copying the sizeable database files.
        temporary_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix='booking-preview-', dir=temporary_root) as temporary:
            export_root = Path(temporary)
            for mode, source in bundles.items():
                target = export_root / mode
                target.mkdir()
                manifest = json.loads((source / 'manifest.json').read_text())
                names = {'manifest.json'}
                names.update(entry['name'] for entry in manifest['files'])
                for name in names:
                    os.link(source / name, target / name)
                archive_path = source / manifest['archive']['name']
                if archive_path.is_file():
                    os.link(archive_path, target / archive_path.name)
                else:
                    from poolrotater_host.catalog_export import _pack
                    self.assertEqual(_pack(target, manifest['files']), manifest['archive'])

            created = []

            def factory(cfg):
                pool = _native_pool_factory(cfg)
                created.append(pool)
                return pool

            provider = ExportPreviewProvider(export_root, pool_factory=factory)
            presets = ('standard-auto', 'standard-fixed', 'standard-loved', 'mania-4k',
                       'mania-7k', 'catch-fixed')
            for preset in presets:
                with self.subTest(preset=preset):
                    settings = {'selection_filters': {
                        'length': {'min': 0, 'max': 180, 'mode': 'hard'},
                    }}
                    if preset in ('standard-auto', 'standard-fixed'):
                        settings['dt_mode'] = 'off'
                    result = provider(preset, settings)
                    self.assertGreaterEqual(result['count'], 10)
                    self.assertLessEqual(len(result['samples']), 5)
                    self.assertTrue(all('title' not in sample for sample in result['samples']))
                    pool = created[-1]
                    # Exercise at least ten real native picks through the same
                    # adapter that the worker uses. The preview itself never
                    # calls next_map or advances cooldown state.
                    staged, effective, _ = provider._prepare(preset, settings)
                    pool = provider._configure_pool(
                        copy.deepcopy(pool), staged, effective, preset)
                    picked = [pool.next_map() for _ in range(10)]
                    self.assertTrue(all(value is not None for value in picked))


if __name__ == '__main__':
    unittest.main()
