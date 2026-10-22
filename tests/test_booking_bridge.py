"""Offline bridge integration for private booking card overlays."""
import hashlib
import json
from pathlib import Path
import queue
import sqlite3
import threading
from types import SimpleNamespace
import uuid
from unittest.mock import patch

from poolrotater_host.bookings.repository import Repository as BookingLedger
from poolrotater_host.discord_relay.bridge import Bridge
from poolrotater_host.network.contracts import canonical


HOST = str(uuid.UUID(int=101))
OWNER = str(uuid.UUID(int=202))
INSTALLATION = str(uuid.UUID(int=303))
GENERATION = str(uuid.UUID(int=404))


class FakeStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'network.sqlite3'
        self.clock = lambda: 1000
        self.rooms = [self.room('room1', room_id=9001)]

    @staticmethod
    def room(alias, room_id):
        return {'host_id': HOST, 'alias': alias, 'freshness': 'fresh',
                'age_seconds': 2, 'state': 'idle', 'osu_room_id': room_id,
                'operator': 'Public Operator', 'title': 'Public Room Title',
                'preset': 'standard-fixed', 'occupants': 3,
                'min_stars': 1, 'max_stars': 5, 'current_map': None,
                'display': {'settings': {'countdown_seconds': 15}}}

    def directory(self, *, offset=0, limit=50, protocol_version=1):
        page = self.rooms[offset:offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(self.rooms) else None
        return {'rooms': page, 'next_offset': next_offset}


class FakeDiscord:
    def __init__(self):
        self.route_lock = threading.Lock()
        self.routes = {}
        self.fleet_channel = 0
        self.commands = queue.Queue()
        self.cards = {}
        self.messages = []

    def update_public_status(self, **values):
        self.cards[values['channel_id']] = values
        return True

    def get_command_nowait(self):
        return self.commands.get_nowait()

    def send_to_channel(self, content, channel):
        self.messages.append((content, channel))
        return True


def bridge_fixture(tmp_path):
    root = tmp_path / 'hub'
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    store = FakeStore(root)
    reports(store)
    discord = FakeDiscord()
    cfg = {'channels': {'5': 1005, '6': 1006},
           'owner_channels': [1001, 1002, 1003, 1004],
           'owner_host_id': OWNER, 'discord': {},
           'feed': str(root / 'missing-feed.json')}
    bridge = Bridge(store, cfg, discord=discord, clock=store.clock)
    return root, store, discord, bridge


def _database(path, schema, inserts):
    db = sqlite3.connect(path)
    db.executescript(schema)
    for sql, args in inserts:
        db.execute(sql, args)
    db.commit()
    db.close()
    path.chmod(0o600)


def reports(store):
    now = 1000
    _database(store.path,
              'CREATE TABLE hosts(id TEXT PRIMARY KEY,installation_id TEXT,generation TEXT,revoked INTEGER)',
              [('INSERT INTO hosts VALUES(?,?,?,0)', (HOST, INSTALLATION, GENERATION))])
    rooms = [{'alias': alias, 'desired': 'stopped', 'revision': 0, 'applied_revision': 0,
              'phase': 'stopped', 'room_id': None, 'room_generation': None,
              'preset': 'standard-auto', 'visibility': 'private', 'pending_operation': None,
              'failure_code': None} for alias in ('room1', 'room2')]
    native = {'protocol_version': 1, 'policy_version': 1, 'installation_id': INSTALLATION,
              'generation': GENERATION, 'host_id': HOST, 'max_concurrent_rooms': 4,
              'provider_room_limit': 4, 'capacity_used': 0, 'rooms': rooms,
              'controller_phase': 'running'}
    _database(store.root / 'managed-rooms.sqlite3',
              'CREATE TABLE reports(host TEXT PRIMARY KEY,sequence INTEGER,pin TEXT,capabilities TEXT,received REAL,response TEXT)',
              [('INSERT INTO reports VALUES(?,?,?,?,?,?)',
                (HOST, 1, 'native-pin', json.dumps(native), now, '{}'))])
    payload = {'protocol_version': 1, 'host_id': HOST, 'installation_id': INSTALLATION,
               'generation': GENERATION, 'sequence': 1, 'sent_at': now,
               'capabilities': {'version': 1, 'enabled_aliases': ['room1', 'room2'],
                                'supported_presets': ['standard-auto'], 'active_booking_ids': []},
               'observations': [], 'receipts': []}
    pin = hashlib.sha256(canonical(payload)).hexdigest()
    _database(store.root / 'bookings-hosts.sqlite3',
              'CREATE TABLE reports(host TEXT PRIMARY KEY,sequence INTEGER,pin TEXT,payload TEXT,received REAL,response TEXT)',
              [('INSERT INTO reports VALUES(?,?,?,?,?,?)',
                (HOST, 1, pin, canonical(payload).decode(), now, '{}'))])


def eligible(host_id, alias):
    return {'host_id': host_id, 'installation_id': INSTALLATION,
            'generation': GENERATION, 'fresh': True, 'supported': True,
            'effective_limit': 4, 'capacity_used': 0,
            'observed_booking_ids': [], 'supported_presets': ['standard-auto']}


def add_private_slot(root, alias='room1', slot_id='private-west'):
    ledger = BookingLedger(root, eligibility=eligible)
    ledger.configure_slot(slot_id, HOST, alias, 'West Private Pool',
                          ['standard-auto'], {'sr_min': 1, 'sr_max': 5}, enabled=True)
    return ledger


def run(bridge):
    with patch.object(bridge, 'discover'):
        return bridge.step()


def test_absent_booking_database_is_inert_and_not_created(tmp_path):
    root, store, discord, bridge = bridge_fixture(tmp_path)
    result = run(bridge)
    assert not (root / 'bookings.sqlite3').exists()
    assert result['assigned'] == 1
    assert discord.cards[1005]['room_id'] == 9001
    assert discord.cards[1005]['room_name'] == 'Public Room Title'
    assert discord.routes[1005] == (HOST, 'room1')


def test_private_slot_safely_overrides_public_card_and_disables_commands(tmp_path):
    root, store, discord, bridge = bridge_fixture(tmp_path)
    add_private_slot(root)
    discord.commands.put(SimpleNamespace(content='.settings', channel_id=1005,
                                         discord_user_id=777, username='Tester', role='ADMIN'))

    result = run(bridge)
    card = discord.cards[1005]
    serialized = json.dumps(card)
    assert card['room_id'] == 0 and card['is_open'] is True
    assert 'West Private Pool' in card['room_name']
    for secret in ('9001', 'Public Room Title', 'Public Operator', 'settings'):
        assert secret not in serialized
    assert 1005 not in discord.routes
    assert discord.messages[-1][1] == 1005
    assert 'unavailable' in discord.messages[-1][0].casefold()
    assert result['routable'] == 0


def test_private_alias_missing_from_directory_gets_synthetic_stable_slot(tmp_path):
    root, store, discord, bridge = bridge_fixture(tmp_path)
    add_private_slot(root, alias='room2', slot_id='private-east')
    run(bridge)

    assert discord.cards[1005]['room_name'] == 'Public Room Title'
    assert discord.cards[1006]['room_id'] == 0
    assert 'West Private Pool' in discord.cards[1006]['room_name']
    assert set(discord.routes) == {1005}
    # The private slot identity retains the same assigned panel on restart.
    second = Bridge(store, bridge.cfg, discord=FakeDiscord(), clock=store.clock)
    run(second)
    assert second.discord.cards[1006]['room_id'] == 0


def test_unreadable_existing_booking_database_fails_closed_for_public_rooms(tmp_path):
    root, store, discord, bridge = bridge_fixture(tmp_path)
    (root / 'bookings.sqlite3').write_text('not a sqlite ledger')
    (root / 'bookings.sqlite3').chmod(0o600)
    run(bridge)
    assert discord.cards[1005]['room_id'] == 0
    assert discord.cards[1005]['lifecycle'] == 'Unavailable · Status delayed'
    assert discord.routes == {}
