"""Installed opt-in policy and bounded managed-bookings command tests."""
import hashlib
import io
import json
import os
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from poolrotater_host.bookings import policy
from poolrotater_host.layout import atomic_json, read_json
from poolrotater_host.network.contracts import NetworkError


INSTALLATION = str(uuid.UUID(int=101))
GENERATION = str(uuid.UUID(int=202))
HOST = str(uuid.UUID(int=303))


class RoomState:
    def __init__(self, *, desired='stopped', phase='unobserved', visibility='private',
                 room_id=None, room_generation=None, pending=None, failure=None):
        self.desired = desired
        self.phase = phase
        self.visibility = visibility
        self.room_id = room_id
        self.room_generation = room_generation
        self.pending = pending
        self.failure = failure

    def values(self):
        return {'alias': 'room1', 'desired': self.desired, 'phase': self.phase,
                'config': {'preset': 'standard-fixed', 'visibility': self.visibility},
                'room_id': self.room_id, 'room_generation': self.room_generation,
                'pending': self.pending, 'failure': self.failure}


def prepare_root(root, monkeypatch, rooms=None):
    (root / 'config').mkdir(mode=0o700, exist_ok=True)
    (root / 'state').mkdir(mode=0o700, exist_ok=True)
    (root / 'config').chmod(0o700)
    (root / 'state').chmod(0o700)
    marker = {'installation_id': INSTALLATION, 'generation': GENERATION}
    host = {'rooms': [{'alias': alias} for alias in ('room1', 'room2', 'room3', 'room4')]}
    monkeypatch.setattr(policy, 'load_host', lambda _root: (marker, host))
    room_states = rooms or {'room1': RoomState()}

    managed_path = root / policy.MANAGED_DB
    managed_path.unlink(missing_ok=True)
    db = sqlite3.connect(managed_path)
    db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('CREATE TABLE rooms(alias TEXT PRIMARY KEY,desired TEXT,config TEXT,phase TEXT,room_id INTEGER,room_generation TEXT,pending TEXT,failure TEXT)')
    db.execute('INSERT INTO meta VALUES(?,?)', ('binding', json.dumps({
        'host_id': HOST, 'installation_id': INSTALLATION, 'generation': GENERATION})) )
    for alias, room in room_states.items():
        value = room.values()
        db.execute('INSERT INTO rooms VALUES(?,?,?,?,?,?,?,?)',
                   (alias, value['desired'], json.dumps(value['config']), value['phase'],
                    value['room_id'], value['room_generation'], value['pending'], value['failure']))
    db.commit(); db.close(); managed_path.chmod(0o600)
    return marker, host


def add_bookings_db(root, *, alias='room1', cleanup_complete=0, corrupt=False):
    path = root / policy.BOOKINGS_DB
    if corrupt:
        path.write_bytes(b'not a sqlite database')
        path.chmod(0o600)
        return path
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE host_bookings(alias TEXT,cleanup_complete INTEGER)')
    db.execute('INSERT INTO host_bookings VALUES(?,?)', (alias, cleanup_complete))
    db.commit(); db.close(); path.chmod(0o600)
    return path


def enable_value(*aliases):
    return {'format': 1, 'enabled': True, 'aliases': list(aliases)}


def test_policy_is_disabled_by_default_and_accepts_only_exact_alias_schema(tmp_path, monkeypatch):
    prepare_root(tmp_path, monkeypatch)
    assert policy.load(tmp_path) == {'format': 1, 'enabled': False, 'aliases': []}
    assert policy.enabled_aliases(tmp_path) == frozenset()

    for bad in (
        {'format': 1, 'enabled': True, 'aliases': ['room1', 'room1']},
        {'format': 1, 'enabled': True, 'aliases': ['room9']},
        {'format': 1, 'enabled': True, 'aliases': [{'x': 1}]},
        {'format': 1, 'enabled': False, 'aliases': ['room1']},
        {'format': 2, 'enabled': True, 'aliases': ['room1']},
    ):
        with pytest.raises(NetworkError):
            policy.configure(tmp_path, bad)
    assert not (tmp_path / policy.NAME).exists()


def test_enablement_requires_private_stopped_unused_and_known_closed_room(tmp_path, monkeypatch):
    prepare_root(tmp_path, monkeypatch)
    managed_before = hashlib.sha256((tmp_path / policy.MANAGED_DB).read_bytes()).hexdigest()
    result = policy.configure(tmp_path, enable_value('room1'))
    assert result == {'enabled': True, 'aliases': ['room1'],
                      'network_agent_restart_required': True,
                      'restart_note': 'The already-running network agent must be restarted once to begin booking exchanges.'}
    assert policy.load(tmp_path) == enable_value('room1')
    assert hashlib.sha256((tmp_path / policy.MANAGED_DB).read_bytes()).hexdigest() == managed_before

    # A new alias must be explicitly private, stopped, and free of pending or
    # uncertain ownership. Public/running aliases never get claimed.
    for room in (
        RoomState(visibility='public'),
        RoomState(desired='running', phase='running', room_id=987),
        RoomState(phase='stopped', pending='pending-op'),
        RoomState(phase='stopped', failure='uncertain'),
    ):
        prepare_root(tmp_path, monkeypatch, {'room1': room})
        atomic_json(tmp_path / policy.NAME, {'format': 1, 'enabled': False, 'aliases': []})
        with pytest.raises(NetworkError) as refused:
            policy.configure(tmp_path, enable_value('room1'))
        assert refused.value.code in ('booking_slot_not_closed', 'booking_native_state_uncertain')

    prepare_root(tmp_path, monkeypatch, {'room1': RoomState(phase='stopped')})
    atomic_json(tmp_path / policy.NAME, {'format': 1, 'enabled': False, 'aliases': []})
    (tmp_path / 'state/rolling').mkdir(mode=0o700, exist_ok=True)
    ownership = {'format': 1, 'rooms': {'room1': {
        'operation': 'owned', 'ownership_unknown': True, 'room_id': 987,
    }}}
    atomic_json(tmp_path / 'state/rolling/ownership.json', ownership)
    with pytest.raises(NetworkError) as unknown:
        policy.configure(tmp_path, enable_value('room1'))
    assert unknown.value.code == 'booking_slot_not_closed'

    atomic_json(tmp_path / 'state/rolling/ownership.json', {'format': 1, 'rooms': {
        'room1': {'operation': 'closed', 'ownership_unknown': False, 'room_id': None},
    }})
    assert policy.configure(tmp_path, enable_value('room1'))['enabled'] is True

    prepare_root(tmp_path, monkeypatch, {'room1': RoomState(phase='unobserved'),
                                         'room2': RoomState(phase='unobserved')})
    atomic_json(tmp_path / 'state/rolling/ownership.json', {'format': 1, 'rooms': {
        'room1': {'operation': 'closed', 'ownership_unknown': False, 'room_id': None},
    }})
    assert policy.configure(tmp_path, enable_value('room2'))['aliases'] == ['room2']


@pytest.mark.parametrize('requested', [
    {'format': 1, 'enabled': False, 'aliases': []},
    {'format': 1, 'enabled': True, 'aliases': ['room2']},
])
def test_active_or_corrupt_booking_ledger_blocks_disabling_or_removing_alias(
        tmp_path, monkeypatch, requested):
    prepare_root(tmp_path, monkeypatch)
    atomic_json(tmp_path / policy.NAME, enable_value('room1'))
    add_bookings_db(tmp_path)
    with pytest.raises(NetworkError) as blocked:
        policy.configure(tmp_path, requested)
    assert blocked.value.code == 'booking_slot_in_use'
    assert policy.load(tmp_path) == enable_value('room1')


def test_unknown_booking_database_fails_closed_when_removing_designation(tmp_path, monkeypatch):
    prepare_root(tmp_path, monkeypatch)
    atomic_json(tmp_path / policy.NAME, enable_value('room1'))
    add_bookings_db(tmp_path, corrupt=True)
    with pytest.raises((NetworkError, ValueError)):
        policy.configure(tmp_path, {'format': 1, 'enabled': False, 'aliases': []})
    assert policy.load(tmp_path) == enable_value('room1')


def test_managed_bookings_action_is_stdin_only_and_reports_restart_requirement(tmp_path, monkeypatch):
    prepare_root(tmp_path, monkeypatch)
    import poolrotater_host.managed_host.policy as managed_policy
    import poolrotater_host.managed_host.operations as operations
    import poolrotater_host.network.installed as installed_network
    monkeypatch.setattr(installed_network, 'guard', lambda root, release: None)
    monkeypatch.setattr(managed_policy, 'enabled', lambda root: True)
    body = json.dumps(enable_value('room1')).encode()
    result = operations.installed(tmp_path, object(), 'bookings', io.BytesIO(body))
    assert result['bookings']['network_agent_restart_required'] is True
    assert result['provider_dispatch'] is False
    assert result['protected_runtime_checked'] is True
    assert policy.enabled_aliases(tmp_path) == {'room1'}
    with pytest.raises(NetworkError) as missing:
        operations.installed(tmp_path, object(), 'bookings')
    assert missing.value.code == 'managed_input_required'


def test_managed_status_includes_only_policy_and_bounded_agent_diagnostics(tmp_path, monkeypatch):
    import poolrotater_host.managed_host.operations as operations
    from poolrotater_host.managed_host import policy as managed_policy
    import poolrotater_host.network.installed as installed_network
    monkeypatch.setattr(installed_network, 'guard', lambda root, release: None)
    monkeypatch.setattr(managed_policy, 'enabled', lambda root: True)
    store = SimpleNamespace(
        snapshot=lambda: {'rooms': [{'alias': 'room1'}]},
        limits=lambda: {'max_concurrent_rooms': 4},
        room=lambda alias: {'alias': alias, 'desired': 'stopped'},
    )
    monkeypatch.setattr(managed_policy, 'state', lambda root: store)
    monkeypatch.setattr(managed_policy, 'seed', lambda root, current: None)
    import poolrotater_host.managed_host.settings as settings
    monkeypatch.setattr(settings, 'inventory', lambda: {})
    monkeypatch.setattr(policy, 'load', lambda root: enable_value('room1'))
    import poolrotater_host.bookings.agent as agent
    monkeypatch.setattr(agent, 'diagnostics', lambda root: {
        'phase': 'connected', 'error': None, 'attempted_at': 1000, 'retry_seconds': 5,
    })

    result = operations.installed(tmp_path, object(), 'status')
    assert result['bookings'] == {
        'policy': enable_value('room1'),
        'agent': {'phase': 'connected', 'error': None, 'attempted_at': 1000,
                  'retry_seconds': 5},
    }
    assert 'password' not in json.dumps(result).casefold()
    assert 'booking_id' not in json.dumps(result)


def test_managed_operator_command_keeps_policy_body_on_stdin(tmp_path, monkeypatch):
    import poolrotater_host.managed_host.operations as operations
    import poolrotater_host.pilot_installation as installation
    import poolrotater_host.pilot_operations as pilot
    target={'root':tmp_path}
    record={'service_uid':1000}
    monkeypatch.setattr(installation,'administrative_binding',lambda instance:(target,None,record))
    calls=[]
    monkeypatch.setattr(pilot,'_service_command',lambda target,record,args,**kwargs:
                        calls.append((list(args),kwargs)) or {'ok':True})

    raw=b'{ "format": 1, "enabled": false, "aliases": [] }'
    assert operations.command('room1','bookings',io.BytesIO(raw))=={'ok':True}
    assert calls[0][0]==['--pilot-check']
    assert calls[1][0]==['--managed-action','bookings','--managed-input']
    assert calls[1][1]['input_data']==b'{"aliases":[],"enabled":false,"format":1}'
    assert all('enabled' not in ' '.join(args) for args,_ in calls)


def test_cli_and_bootstraps_forward_bounded_managed_booking_stdin(tmp_path, monkeypatch, capsys):
    import poolrotater_host.cli as cli
    import poolrotater_host.managed_host.operations as operations
    forwarded = {}
    monkeypatch.setattr(operations, 'command',
                        lambda instance, action, stream=None: forwarded.update(
                            instance=instance, action=action, body=stream.read()) or {'ok': True})
    monkeypatch.setattr(cli.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'{"format":1}')))
    assert cli.main(['pilot-managed-bookings', '--instance', 'room1', '--managed-stdin']) == 0
    assert forwarded == {'instance': 'room1', 'action': 'bookings', 'body': b'{"format":1}'}
    assert json.loads(capsys.readouterr().out) == {'ok': True}

    import poolrotater_host.pilot_admin_entry as admin_entry
    monkeypatch.setattr(admin_entry.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(admin_entry, 'verified_release', lambda instance: tmp_path)
    import poolrotater_host.pilot_installation as installation
    monkeypatch.setattr(installation, 'administrative_binding', lambda instance: (
        {}, None, {'environment': str(tmp_path)}))
    captured = {}
    def capture_exec(path, argv, env):
        captured.update(path=path, argv=argv, env=env)
        raise RuntimeError('exec captured')
    monkeypatch.setattr(admin_entry.os, 'execve', capture_exec)
    monkeypatch.setattr(admin_entry.sys, 'argv', [
        'pilot_admin_entry', '--instance', 'room1', 'managed-bookings', '--managed-stdin'])
    with pytest.raises(RuntimeError, match='exec captured'):
        admin_entry.main()
    assert captured['argv'][-1] == '--managed-stdin'
    assert 'format' not in ' '.join(captured['argv'])

    import poolrotater_host.service_bootstrap as bootstrap
    monkeypatch.setattr(bootstrap.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(bootstrap, '_protected', lambda path: Path(path))
    monkeypatch.setattr(bootstrap, '_json', lambda path, owner: {'format': 1, 'sha256': 'abc'})
    monkeypatch.setattr(bootstrap, '_hash', lambda path: 'abc')
    monkeypatch.setattr(bootstrap, 'preflight', lambda root, prefix, prepared=False: {
        'environment': Path('/registered/env'), 'release': Path('/registered/release')})
    monkeypatch.setattr(bootstrap.os, 'execve', capture_exec)
    monkeypatch.setattr(bootstrap.sys, 'argv', [
        'service_bootstrap', '--root', str(tmp_path), '--managed-action', 'bookings', '--managed-input'])
    with pytest.raises(RuntimeError, match='exec captured'):
        bootstrap.main()
    assert '--managed-action' in captured['argv']
    assert captured['argv'][captured['argv'].index('--managed-action') + 1] == 'bookings'
    assert '--managed-input' in captured['argv']
