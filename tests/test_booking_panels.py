"""Owner booking panel states derived from local, qualified evidence only."""
import copy
import uuid
from pathlib import Path
from types import SimpleNamespace

from poolrotater_host.bookings.panels import Publisher
from poolrotater_host.layout import atomic_json


HOST = str(uuid.UUID(int=11))
INSTALLATION = str(uuid.UUID(int=22))
GENERATION = str(uuid.UUID(int=33))
OWNER = 444


class Clock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value


class NativeState:
    def __init__(self, rooms, *, effective_limit=4, capacity_used=0):
        self.binding = {'host_id': HOST, 'installation_id': INSTALLATION,
                        'generation': GENERATION}
        self.rooms = copy.deepcopy(rooms)
        self.limits_value = {'effective_limit': effective_limit,
                             'capacity_used': capacity_used}

    def room(self, alias, *, private=False):
        return copy.deepcopy(self.rooms[alias])

    def limits(self):
        return copy.deepcopy(self.limits_value)


class FakeSessions:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = 0

    def leases(self, *, include_ended=False):
        self.calls += 1
        assert include_ended is True
        return copy.deepcopy(self.rows)


class View:
    def __init__(self, alias):
        self.alias = alias
        self.calls = []

    def update_public_status(self, **values):
        self.calls.append(copy.deepcopy(values))
        return True


class Discord:
    def __init__(self, aliases):
        self.views = {alias: View(alias) for alias in aliases}
        self.selected = []

    def view_for(self, alias):
        self.selected.append(alias)
        return self.views[alias]


def room(*, desired='stopped', phase='unobserved', visibility='private',
         room_id=None, room_generation=None, pending=None, failure=None):
    return {'alias': 'room1', 'desired': desired, 'phase': phase,
            'config': {'visibility': visibility}, 'room_id': room_id,
            'room_generation': room_generation, 'pending': pending, 'failure': failure}


def lease_row(phase='active', *, cleanup_complete=False, alias='room1'):
    return {'lease': {
        'id': str(uuid.uuid4()), 'alias': alias, 'host_id': HOST,
        'installation_id': INSTALLATION, 'generation': GENERATION,
        'session_generation': uuid.uuid4().hex, 'password': 'hidden-password',
        'user_id': OWNER,
    }, 'state': {'phase': phase}, 'cleanup_complete': cleanup_complete}


def setup(root, *, aliases=('room1',), rooms=None, leases=(), journal=None,
          capacity=0, effective_limit=4, channel_ids=None, clock=None):
    (root / 'config').mkdir(mode=0o700, exist_ok=True)
    (root / 'state').mkdir(mode=0o700, exist_ok=True)
    atomic_json(root / 'config/private-bookings.json',
                {'format': 1, 'enabled': True, 'aliases': list(aliases)})
    if leases:
        (root / 'state/bookings.sqlite3').write_bytes(b'local-ledger-present')
    native = NativeState(rooms or {'room1': room()}, effective_limit=effective_limit,
                         capacity_used=capacity)
    cfgs = {alias: {'_instance_name': alias,
                    'discord_public_status_channel_id': (channel_ids or {}).get(alias, 1000 + int(alias[-1]))}
            for alias in ('room1', 'room2', 'room3', 'room4')}
    discord = Discord(cfgs)
    owner = SimpleNamespace(
        runtime=root, configs=cfgs, discord=discord,
        managed=SimpleNamespace(state=native),
        registry=SimpleNamespace(snapshot=lambda: {'rooms': copy.deepcopy(journal or {})}),
        bookings=SimpleNamespace(sessions=FakeSessions(leases)),
    )
    publisher = Publisher(owner, clock=clock or Clock())
    return owner, publisher, native


def test_only_enabled_owner_channel_gets_private_available_card_without_room_data(tmp_path):
    owner, publisher, _ = setup(tmp_path, aliases=('room1',))
    # The database is absent, so the publisher must not construct/open local sessions.
    owner.bookings.sessions = SimpleNamespace(leases=lambda **kwargs: (_ for _ in ()).throw(
        AssertionError('sessions opened without a bookings database')))
    publisher.tick()

    card, = owner.discord.views['room1'].calls
    assert owner.discord.selected == ['room1']
    assert card['is_open'] is True and card['lifecycle'] == 'Available'
    assert card['room_id'] == 0 and card['player_count'] == card['spectator_count'] == 0
    assert card['map_label'] == card['map_url'] == ''
    serialized = repr(card)
    for secret in ('hidden-password', HOST, INSTALLATION, GENERATION, '987'):
        assert secret not in serialized
    assert owner.discord.views['room2'].calls == []


def test_booked_lifecycle_states_and_unclean_ended_lease_are_not_available(tmp_path):
    for phase, expected in (('preparing', 'Preparing'), ('awaiting_claim', 'Reserved'),
                            ('active', 'Active'), ('closing', 'Closing'),
                            ('ended', 'Closing')):
        root = tmp_path / phase
        root.mkdir()
        owner, publisher, native = setup(root, leases=[lease_row(phase)])
        native.limits_value.update(effective_limit=1, capacity_used=1)
        publisher.tick()
        card, = owner.discord.views['room1'].calls
        assert card['lifecycle'] == expected
        assert card['is_open'] is False and card['room_id'] == 0


def test_availability_requires_private_known_closed_slot_and_free_native_capacity(tmp_path):
    for state, journal, capacity, expected in (
        (room(phase='running', desired='running', room_id=987), {}, 0, 'Unavailable'),
        (room(phase='unobserved', visibility='public'), {}, 0, 'Unavailable'),
        (room(phase='stopped'), {'room1': {'operation': 'owned', 'ownership_unknown': True,
                                           'room_id': 987}}, 0, 'Unavailable'),
        (room(phase='unobserved'), {}, 4, 'Unavailable'),
        (room(phase='stopped'), {'room1': {'operation': 'closed', 'ownership_unknown': False,
                                           'room_id': None}}, 0, 'Available'),
    ):
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        root.mkdir()
        owner, publisher, _ = setup(root, rooms={'room1': state}, journal=journal,
                                    capacity=capacity, effective_limit=4)
        publisher.tick()
        card, = owner.discord.views['room1'].calls
        assert card['lifecycle'] == expected
        assert card['is_open'] is (expected == 'Available')


def test_completed_cleanup_releases_panel_only_after_confirmed_native_close(tmp_path):
    root = tmp_path / 'complete'
    root.mkdir()
    owner, publisher, _ = setup(root, leases=[lease_row('ended', cleanup_complete=True)],
        rooms={'room1': room(phase='stopped')},
        journal={'room1': {'operation': 'closed', 'ownership_unknown': False, 'room_id': None}})
    publisher.tick()
    card, = owner.discord.views['room1'].calls
    assert card['lifecycle'] == 'Available'
    assert card['is_open'] is True and card['room_id'] == 0


def test_panel_changes_are_coalesced_for_twenty_seconds(tmp_path):
    clock = Clock(100)
    root = tmp_path / 'coalesce'
    root.mkdir()
    owner, publisher, _ = setup(root, clock=clock)
    publisher.tick()
    assert len(owner.discord.views['room1'].calls) == 1

    owner.bookings.sessions.rows = [lease_row('active')]
    (root / 'state/bookings.sqlite3').write_bytes(b'local-ledger-present')
    clock.value += 5
    publisher.tick()
    assert len(owner.discord.views['room1'].calls) == 1

    clock.value += 15
    publisher.tick()
    assert len(owner.discord.views['room1'].calls) == 2
    assert owner.discord.views['room1'].calls[-1]['lifecycle'] == 'Active'


def test_panel_does_not_publish_when_channel_is_not_configured(tmp_path):
    root = tmp_path / 'no-channel'
    root.mkdir()
    owner, publisher, _ = setup(root, channel_ids={'room1': 0})
    publisher.tick()
    assert owner.discord.selected == []
