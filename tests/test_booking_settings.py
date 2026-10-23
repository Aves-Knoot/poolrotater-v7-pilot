"""Booking-specific preset inventories and merged settings validation."""
import pytest

from bot.mode import MANIA_FREE_MODS
from poolrotater_host.bookings.settings import inventory, validate_player_patch
from poolrotater_host.network.contracts import NetworkError


def refused(patch, *, current=None, preset='standard-fixed'):
    with pytest.raises(NetworkError):
        validate_player_patch(patch, current=current, preset=preset)


def test_inventories_only_offer_supported_preset_controls():
    standard = inventory('standard-fixed')
    assert not {'DT', 'NC', 'HT', 'DC'} & set(standard['options']['allowed_mods'])
    assert not {'FI', 'MR'} & set(standard['options']['allowed_mods'])
    assert standard['options']['dt_mode'] == ['auto', 'off', 'mixed', 'full']
    mania = inventory('mania-4k')
    assert set(mania['options']['allowed_mods']) == MANIA_FREE_MODS
    assert 'dt_mode' not in mania['fields']
    catch = inventory('catch-fixed')
    assert not {'allowed_mods', 'required_mods', 'freestyle', 'auto_difficulty'} & set(catch['fields'])
    assert not {'allowed_mods', 'required_mods'} & set(catch['options'])
    assert catch['defaults']['auto_difficulty'] is False


@pytest.mark.parametrize('mod', ['DT', 'NC', 'HT', 'DC'])
def test_standard_clock_mods_are_not_player_editable(mod):
    refused({'allowed_mods': [mod]})
    refused({'required_mods': [mod]})


@pytest.mark.parametrize('mods', [['NF', 'SD'], ['NF', 'PF'], ['SD', 'PF'], ['EZ', 'HR']])
def test_standard_conflicting_required_mods_are_refused(mods):
    refused({'required_mods': mods})


def test_dt_modes_reject_fixed_speed_mods_but_auto_keeps_valid_mods():
    refused({'dt_mode': 'mixed', 'required_mods': ['HD']})
    refused({'dt_mode': 'full', 'required_mods': ['EZ']})
    assert validate_player_patch({'dt_mode': 'auto', 'required_mods': ['HD']}, preset='standard-fixed') == {
        'dt_mode': 'auto', 'required_mods': ['HD']}


def test_mode_specific_mods_and_fixed_catch_settings():
    assert validate_player_patch({'allowed_mods': ['FI', 'MR']}, preset='mania-4k') == {
        'allowed_mods': ['FI', 'MR']}
    refused({'allowed_mods': ['HR']}, preset='mania-4k')
    refused({'allowed_mods': ['HD', 'FI'], 'required_mods': ['HD', 'FI']}, preset='mania-4k')
    refused({'allowed_mods': ['HD']}, preset='catch-fixed')
    refused({'freestyle': True}, preset='catch-fixed')
    refused({'auto_difficulty': True}, preset='catch-fixed')


def test_sr_bounds_are_preset_specific_and_integer_fields_reject_floats():
    refused({'sr_max': 20.1})
    assert validate_player_patch({'sr_max': 100}, preset='catch-fixed') == {'sr_max': 100}
    refused({'sr_max': 100}, preset='standard-fixed')
    refused({'countdown_seconds': 20.0})
    refused({'max_length': 14})
    assert validate_player_patch({'countdown_seconds': 20}) == {'countdown_seconds': 20}


def test_booking_only_patch_still_validates_merged_native_combinations():
    current = {'preset': 'standard-fixed', 'required_mods': ['HD'], 'dt_mode': 'mixed'}
    refused({'selection_filters': {'length': {'min': 0, 'max': 120, 'mode': 'prefer'}}},
            current=current)


@pytest.mark.parametrize('preset', [
    'standard-auto', 'standard-fixed', 'standard-loved',
    'mania-4k', 'mania-7k', 'catch-fixed'])
def test_booking_settings_do_not_recreate_room_or_change_ruleset_mid_session(preset):
    from poolrotater_host.managed_host.settings import RECREATE
    schema = inventory(preset)
    assert not set(schema['fields']) & RECREATE
    assert set(schema['application'].values()) <= {'live', 'next_map'}
    for forbidden in ('preset', 'ruleset_id', 'visibility', 'password', 'room_name'):
        refused({forbidden: 'invented replacement'}, preset=preset)
