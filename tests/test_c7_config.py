"""Catch is explicit, fixed native NM and cannot inherit Standard tuning."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot.mode import merge_room_config, ruleset_id, validate_catch_config
from poolrotater_host import config, gameplay_defaults, runtime_defaults
from poolrotater_host.catalog_store import mode_for
from poolrotater_host.layout import setup, load, atomic_json
from poolrotater_host.owner_settings import validate_settings, project_legacy
from poolrotater_host.runtime_config import render, validate_room_data


class CatchConfigurationTests(unittest.TestCase):
    def test_new_preset_is_explicit_and_default_four_are_unchanged(self):
        original = ['standard-auto', 'standard-fixed', 'mania-4k', 'mania-7k']
        self.assertEqual([r['preset'] for r in config.defaults()['rooms']], original)
        catch = config.defaults(['catch-fixed'])['rooms'][0]
        self.assertEqual(catch, {'alias': 'room1', 'preset': 'catch-fixed', 'sr_min': 3.0,
                                'sr_max': 4.0, 'language': 'en', 'catalog': 'catalogs/catch.db'})
        self.assertEqual(config.PRESETS['catch-fixed']['ruleset'], 2)
        self.assertFalse(config.PRESETS['catch-fixed']['auto_difficulty'])

    def test_catch_configuration_can_request_managed_production(self):
        for execution in ('pilot-live', 'pilot-candidate', 'pilot-qualification'):
            host = config.defaults(['catch-fixed']); host['execution'] = execution
            with self.subTest(execution=execution):config.validate(host)

    def test_extreme_loved_difficulty_is_explicit_without_other_mode_changes(self):
        host = config.defaults(['catch-fixed'])
        host['rooms'][0].update(sr_min=90, sr_max=99)
        config.validate(host)
        for preset in config.DEFAULT_PRESETS:
            host = config.defaults([preset]); host['rooms'][0].update(sr_min=90, sr_max=99)
            with self.subTest(preset=preset), self.assertRaises(config.ConfigError): config.validate(host)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'host'; setup(root, ['catch-fixed'])
            base = render(root)['rooms'][0]['settings']
            validate_room_data({**base, 'sr_min': 90, 'sr_max': 99}, base)

    def test_mode_identity_never_accepts_boolean_or_unknown_rulesets(self):
        for value in (0, 1, 2, 3): self.assertEqual(ruleset_id({'ruleset_id': value}), value)
        for value in (True, False, '2', 4, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ruleset_id({'ruleset_id': value})

    def test_standard_global_tuning_cannot_enter_catch_room(self):
        defaults = {'ruleset_id': 0, 'auto_difficulty': True, 'stream_enabled': True,
                    'freestyle': True, 'allowed_mods': ['HD'], 'min_passcount': 10000,
                    'max_map_age_years': 8, 'mania_farm_enabled': True,
                    'high_star_relax_enabled': True}
        room = {'ruleset_id': 2, 'catch_catalog': '/research/catch.db', 'sr_min': 2, 'sr_max': 4,
                'catch_settings': {'catch_include_loved': True, 'max_length': 300}}
        result = merge_room_config(defaults, room, {'ruleset_id': 3, 'freestyle': True,
                                                   'sr_min': 7, 'countdown_seconds': 20})
        self.assertEqual((result['ruleset_id'], result['sr_min'], result['sr_max']), (2, 2, 4))
        self.assertEqual(result['countdown_seconds'], 20)
        self.assertEqual(result['max_length'], 300)
        self.assertTrue(result['catch_include_loved'])
        for key in ('auto_difficulty','freestyle','stream_enabled','mania_farm_enabled','high_star_relax_enabled'):
            self.assertIs(result[key], False, key)
        self.assertEqual((result['allowed_mods'], result['required_mods']), ([], []))
        self.assertEqual((result['min_passcount'], result['max_map_age_years']), (0, 0))
        self.assertTrue(defaults['freestyle'])

    def test_explicit_unsupported_catch_preferences_refused(self):
        mutations = [{'allowed_mods': ['HD']}, {'required_mods': ['DT']}, {'freestyle': True},
                     {'auto_difficulty': True}, {'loved_mode': True}, {'stream_enabled': True},
                     {'catch_include_loved': 1}, {'allowed_mods': ()}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                merge_room_config({}, {'ruleset_id': 2, 'catch_settings': mutation}, {})
        for value in ([], {'token_file': '/outside'}, {'mania_db': '/outside'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                merge_room_config({}, {'ruleset_id': 2, 'catch_settings': value}, {})
        for rate in (1.5, True, '1'):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                validate_catch_config({'clock_rate': rate})

    def test_signed_managed_catalogs_identify_catch_explicitly(self):
        self.assertEqual(mode_for({'preset': 'catch-fixed'}),'catch')
        self.assertEqual([mode_for(r) for r in config.defaults()['rooms']],
                         ['standard', 'standard', 'mania4', 'mania7'])

    def test_catch_gameplay_defaults_have_no_standard_calibration(self):
        defaults = gameplay_defaults.settings_for('catch-fixed')
        self.assertNotIn('auto_sr_bias', defaults)
        self.assertNotIn('auto_sr_half_width', defaults)
        self.assertNotIn('catch-fixed', gameplay_defaults.PROFILES)
        self.assertEqual(defaults['pool_limit'], 200000)
        for update in ({'freestyle': True}, {'allowed_mods': ['HD']}, {'stream_enabled': True},
                       {'high_star_relax_enabled': True}, {'mania_farm_enabled': True}):
            with self.subTest(update=update), self.assertRaises(config.ConfigError):
                validate_settings({**defaults, **update}, preset='catch-fixed')
        self.assertTrue(validate_settings({'catch_include_loved': True}, preset='catch-fixed')['catch_include_loved'])

    def test_legacy_frozen_profiles_remain_valid_and_catch_does_not_mutate_them(self):
        marker = {'installation_id': 'invented', 'generation': 'invented-generation'}
        record = {'format': 1, **marker, 'profiles': {
            name: gameplay_defaults.settings_for(name) for name in config.DEFAULT_PRESETS}}
        original = copy.deepcopy(record)
        self.assertEqual(runtime_defaults._validated(record, marker), record)
        with patch.object(runtime_defaults, '_control', return_value=record):
            self.assertEqual(runtime_defaults.settings_for(Path('/unused'), marker, 'catch-fixed'), {})
            self.assertEqual(runtime_defaults.settings_for(Path('/unused'), marker, 'standard-auto'),
                             record['profiles']['standard-auto'])
        self.assertEqual(record, original)

    def test_catch_runtime_render_pins_local_catalog_without_other_ruleset_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'host'; setup(root, ['catch-fixed'])
            base = render(root)['rooms'][0]['settings']
            self.assertEqual(base['catch_catalog'], str(root / 'catalogs/catch.db'))
            self.assertEqual((base['ruleset_id'], base['auto_difficulty']), (2, False))
            self.assertEqual((base['farm_db'], base['mania_db']), ('', ''))
            self.assertFalse(base['freestyle'])
            validate_room_data(base, base)
            for mutation in ({'auto_difficulty': True}, {'allowed_mods': ['HD']},
                             {'freestyle': True}, {'catch_catalog': '/outside.db'},
                             {'catch_settings': {}}, {'stream_enabled': True}):
                with self.subTest(mutation=mutation), self.assertRaises(config.ConfigError):
                    validate_room_data({**base, **mutation}, base)

    def test_legacy_owner_adoption_does_not_turn_catch_into_standard(self):
        with self.assertRaisesRegex(config.ConfigError, 'explicit offline setup'):
            project_legacy({}, {'room1': {'ruleset_id': 2}}, {})


if __name__ == '__main__': unittest.main()
