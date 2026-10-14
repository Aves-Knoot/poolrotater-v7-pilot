"""Explicit stopped gameplay patches; no topology, credentials or room calls."""
import copy
import os

from .config import ConfigError
from .layout import atomic_json, contained, file_lease, load
from .owner_settings import RECORD, load_overrides, validate_record
from .pilot_installation import administrative_binding, _stopped, require_no_service_processes
from . import pilot_operations as pilot
from .network import operations as network
from .ratings.operations import _identity


def status(root):
    marker, host = load(root)
    overlay = load_overrides(root, marker, host)
    return {'ok': True, 'rooms': [
        {'alias': room['alias'], 'preset': room['preset'],
         'configured_settings': copy.deepcopy(overlay.get(room['alias'], {}).get('settings', {}))}
        for room in host['rooms']], 'scope': 'saved_gameplay_defaults',
        'provider_dispatch': False, 'services_started': False}


def configure(root, value):
    """Called as dedicated UID with stopped ownership/process guards held."""
    from .network.contracts import fields
    fields(value, ('rooms',))
    if not isinstance(value['rooms'], dict) or not value['rooms']:
        raise ConfigError('room settings requires a nonempty room patch')
    marker, host = load(root)
    expected = {room['alias']: room['preset'] for room in host['rooms']}
    if set(value['rooms']) - set(expected):
        raise ConfigError('room settings contains an unknown alias')
    old = load_overrides(root, marker, host)
    rooms = {alias: {'preset': preset, 'settings': copy.deepcopy(old.get(alias, {}).get('settings', {}))}
             for alias, preset in expected.items()}
    for alias, patch in value['rooms'].items():
        if not isinstance(patch, dict) or not patch:
            raise ConfigError('room settings requires nonempty settings objects')
        rooms[alias]['settings'].update(patch)
    record = validate_record({'format': 1,
        **{key: marker[key] for key in ('installation_id', 'generation')}, 'rooms': rooms}, marker, host)
    changed = {alias: sorted(key for key, item in patch.items()
               if old.get(alias, {}).get('settings', {}).get(key) != item)
               for alias, patch in value['rooms'].items()}
    path = contained(root, RECORD)
    if not os.path.lexists(path) or rooms != old:
        atomic_json(path, record)
    return {**status(root), 'changed_settings': changed,
            'takes_effect': 'next_start_preserving_unrelated_native_preferences',
            'credentials_catalogs_ownership_preserved': True}


def command(instance, action, *, stream=None):
    if action not in ('configure', 'status') or (stream is not None) != (action == 'configure'):
        raise ConfigError('invalid room settings action or input')
    target, _, record = administrative_binding(instance)
    pilot._service_command(target, record, ['--pilot-check'])
    if action == 'status':
        with _identity(record['service_uid']):
            result = status(target['root'])
    else:
        with file_lease(target['prefix'] / 'network-admin.lock'):
            pilot._unit(instance, optional=True)
            network._checked_unit(instance, optional=True)
            for state in (pilot._service_state(instance), network._state(instance)):
                if (state['MainPID'] or state['ActiveState'] not in ('inactive', 'failed')
                        or state['UnitFileState'] not in ('', 'disabled', 'not-found')):
                    raise ConfigError('room settings requires disabled/stopped bot and network services')
            require_no_service_processes(record['service_uid'])
            with _stopped(target['root'], record['service_uid']):
                require_no_service_processes(record['service_uid'])
                from .network.contracts import decode
                value = decode(stream.read(32769), maximum=32768)
                with _identity(record['service_uid']):
                    result = configure(target['root'], value)
    return {**result, 'protected_runtime_checked': True, 'actual_room_testing': False}
