"""Protected stopped private-test administration; no implicit room or OAuth start."""
from .config import ConfigError
from .layout import file_lease
from .pilot_installation import administrative_binding, _stopped, require_no_service_processes
from .ratings.operations import _identity
from . import pilot_operations as pilot
from .network import operations as network

MAX_INPUT = 1024


def _safe_result(value, action):
    # The secret-bearing core record never belongs in an administrative response.
    # Return only closed public flags, even if a future core adds private fields.
    if (type(value) is not dict or value.get('ok') is not True
            or type(value.get('private_test_configured')) is not bool
            or value.get('selected_alias') not in (None, 'room1')
            or type(value.get('access_record_present')) is not bool
            or value.get('public') is not False
            or value.get('service_started') is not False):
        raise ConfigError('private test result refused')
    visible = value.get('discord_status_visible',False)
    if type(visible)is not bool or visible and not value['private_test_configured']:
        raise ConfigError('private test result refused')
    configured = value['private_test_configured']
    if configured != (value['selected_alias'] == 'room1'):
        raise ConfigError('private test result refused')
    session = value.get('session_id')
    if configured:
        import uuid
        try:
            if type(session) is not str or str(uuid.UUID(session)) != session:
                raise ValueError
        except ValueError:
            raise ConfigError('private test result refused') from None
    elif session is not None or value['access_record_present']:
        raise ConfigError('private test result refused')
    return {'format': 1, 'ok': True, 'action': 'private-test-' + action,
            'private_test_configured': configured, 'selected_alias': value['selected_alias'],
            'session_id': session, 'access_record_present': value['access_record_present'],
            'public': False, 'discord_status_visible': visible, 'protected_runtime_checked': True,
            'services_started': False, 'service_started': False,
            'provider_dispatch': False, 'actual_room_testing': False}


def command(instance, action, *, stream=None):
    if action not in ('configure', 'clear', 'status'):
        raise ConfigError('invalid private test administration action')
    if (stream is not None) != (action == 'configure'):
        raise ConfigError('private test configure requires explicit bounded stdin')
    target, _, record = administrative_binding(instance)
    pilot._service_command(target, record, ['--pilot-check'])
    with file_lease(target['prefix'] / 'network-admin.lock'):
        pilot._unit(instance, optional=True)
        network._checked_unit(instance, optional=True)
        for state in (pilot._service_state(instance), network._state(instance)):
            if (state['MainPID'] or state['ActiveState'] not in ('inactive', 'failed')
                    or state['UnitFileState'] not in ('', 'disabled', 'not-found')):
                raise ConfigError('private test administration requires disabled/stopped bot and network services')
        require_no_service_processes(record['service_uid'])
        with _stopped(target['root'], record['service_uid']):
            require_no_service_processes(record['service_uid'])
            if administrative_binding(instance)[2] != record:
                raise ConfigError('registered private test runtime changed')
            with _identity(record['service_uid']):
                from . import private_pilot
                if action == 'configure':
                    from .network.contracts import decode, fields
                    value = decode(stream.read(MAX_INPUT + 1), maximum=MAX_INPUT)
                    if type(value)is not dict or 'alias'not in value or set(value)-{'alias','password','discord_status'}:
                        fields(value,('alias',))
                    if value['alias'] != 'room1':
                        raise ConfigError('private test input refused')
                    options = {'alias': value['alias'], 'owner_lease_held': True}
                    if 'password' in value:
                        import re
                        if type(value['password']) is not str or not re.fullmatch(
                                '[A-Za-z0-9_-]{8,128}', value['password']):
                            raise ConfigError('private test password input refused')
                        options['password'] = value['password']
                    if 'discord_status'in value:
                        if type(value['discord_status'])is not bool:
                            raise ConfigError('private test Discord visibility input refused')
                        options['discord_status']=value['discord_status']
                    result = private_pilot.configure(target['root'], **options)
                elif action == 'clear':
                    result = private_pilot.clear(target['root'], owner_lease_held=True)
                else:
                    result = private_pilot.status(target['root'])
    return _safe_result(result, action)
