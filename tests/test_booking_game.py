"""Booking-specific chat and worker guards, independent of native services."""
import copy
import uuid
from types import SimpleNamespace

import pytest

from poolrotater_host.bookings import game
from poolrotater_host.bookings.local import LocalSessions
from poolrotater_host.network.contracts import NetworkError


HOST = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
INSTALLATION = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
GENERATION = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
OWNER = 101
CONTROLLER = 202
PLAYER = 303
BASE = {'sr_min': 3.5, 'sr_max': 4.5, 'countdown_seconds': 30}


class Clock:
    def __init__(self, value=1000):
        self.value = value

    def __call__(self):
        return self.value


class FakeBot:
    def __init__(self, root, binding, lease):
        self.cfg = {
            '_host_root': str(root),
            '_booking_session': {key: lease[key] for key in
                                 ('id', 'alias', 'session_generation', 'preset')},
            'user_commands_enabled': True,
        }
        self.binding = copy.deepcopy(binding)
        self.pool = SimpleNamespace(sr_min=3.5, sr_max=4.5)
        self.players = {OWNER: {'name': 'owner'}, PLAYER: {'name': 'player'}}
        self.lines = []
        self.started = []
        self.skipped = []
        self.aborted = []
        self._explicit_abort_pending = False
        self.room_id = 777
        self.hub = SimpleNamespace(abort_match=lambda room_id: self.aborted.append(room_id))

    def say(self, message):
        self.lines.append(message)

    def _start_from_command(self, user_id, *, privileged=False):
        self.started.append((user_id, privileged))

    def force_skip(self, user_id):
        self.skipped.append(user_id)


class CandidatePool:
    def __init__(self):
        self.maps = [SimpleNamespace(beatmap_id=901, sr=4.1, hit_length=90,
                                     approach_rate=7.0, is_dt=False, clock_rate=1.0)]

    def next_map(self):
        return self.maps[0] if self.maps else None


def configure_worker(bot):
    bot.cfg.update(ruleset_id=0, sr_min=3.5, sr_max=4.5,
                   auto_difficulty=False, _supervisor_overrides={})


def binding(generation=GENERATION):
    return {'host_id': HOST, 'installation_id': INSTALLATION, 'generation': generation}


def lease(*, booking_id=None, generation=GENERATION):
    return {
        'id': booking_id or str(uuid.uuid4()), 'user_id': OWNER,
        'designated_friend_id': None, 'host_id': HOST,
        'installation_id': INSTALLATION, 'generation': generation,
        'alias': 'room1', 'session_generation': uuid.uuid4().hex,
        'preset': 'standard-fixed', 'settings': copy.deepcopy(BASE),
        'password': 'booking-game-test-password',
    }


def setup(tmp_path, monkeypatch):
    clock = Clock()
    saved = lease()
    native = SimpleNamespace(binding=binding())
    import poolrotater_host.bookings.policy as policy
    monkeypatch.setattr(policy, 'native_binding', lambda root: native.binding)
    store = LocalSessions(tmp_path, native.binding, clock=clock)
    row = store.install(saved, BASE)
    bot = FakeBot(tmp_path, native.binding, row['lease'])
    bot._store = store
    return bot, store, row, clock, native


def add_controller(store, row, clock):
    store.advance(row['lease']['id'], {
        'ready': True, 'room_id': 777, 'incarnation': 'inc-1',
        'roster_known': True,
        'players': [{'user_id': OWNER, 'username': 'owner'},
                    {'user_id': CONTROLLER, 'username': 'controller'}],
        'match': None, 'closed': False, 'ownership_known': True,
        'updated_at': clock.value,
    })
    command = {
        'protocol_version': 1, 'operation_id': str(uuid.uuid4()),
        'booking_id': row['lease']['id'], 'host_id': HOST,
        'installation_id': INSTALLATION, 'generation': GENERATION,
        'alias': 'room1', 'session_generation': row['lease']['session_generation'],
        'sequence': 0, 'user_id': OWNER, 'action': 'grant',
        'payload': {'user_id': CONTROLLER},
    }
    result = store.receive(command)
    store.acknowledge(result['operation_id'], 'applied', application={'role': 'controller'})


def test_role_scoped_controls_preserve_moderation_and_block_broad_commands(tmp_path, monkeypatch):
    bot, store, row, _, _ = setup(tmp_path, monkeypatch)

    assert game.command(bot, OWNER, 'start', ['start']) is True
    queued = store.pending(row['lease']['id'])
    assert [(item['action'], item['user_id'], item['origin']) for item in queued] == [
        ('start', OWNER, 'local')]

    assert game.command(bot, PLAYER, 'start', ['start']) is True
    assert game.command(bot, PLAYER, 'range', ['range', '1', '2']) is True
    assert game.command(bot, OWNER, 'title', ['title', 'custom']) is True
    assert game.command(bot, OWNER, 'admin', ['admin']) is True
    assert game.command(bot, OWNER, 'freedive', ['freedive']) is True
    assert game.command(bot, OWNER, 'map', ['map', '123']) is True
    # Native moderation commands and player-facing information remain native.
    assert game.command(bot, PLAYER, 'good', ['good']) is False
    assert game.command(bot, PLAYER, 'bad', ['bad']) is False
    assert game.command(bot, PLAYER, 'queue', ['queue']) is False
    assert len(store.pending(row['lease']['id'])) == 1


def test_local_settings_cooldown_prevents_request_flood_and_keeps_queue(tmp_path, monkeypatch):
    bot, store, row, _, _ = setup(tmp_path, monkeypatch)
    assert game.command(bot, OWNER, 'range', ['range', '4', '5']) is True
    assert game.command(bot, OWNER, 'range', ['range', '4.1', '5.1']) is True
    pending = store.pending(row['lease']['id'])
    assert len(pending) == 1
    assert pending[0]['action'] == 'configure'
    assert pending[0]['payload']['sr_min'] == 4
    assert all('4.1' not in message for message in bot.lines)


def test_stale_session_and_native_generation_bindings_refuse_controls(tmp_path, monkeypatch):
    bot, store, row, _, native = setup(tmp_path, monkeypatch)
    bot.cfg['_booking_session']['session_generation'] = '0' * 32
    assert game.command(bot, OWNER, 'start', ['start']) is True
    assert store.pending(row['lease']['id']) == []

    bot.cfg['_booking_session'] = {key: row['lease'][key] for key in
                                   ('id', 'alias', 'session_generation', 'preset')}
    native.binding = binding('dddddddd-dddd-4ddd-8ddd-dddddddddddd')
    assert game.command(bot, OWNER, 'start', ['start']) is True
    assert store.pending(row['lease']['id']) == []


@pytest.mark.parametrize('action,payload', [
    ('start', {}), ('abort', {}), ('skip', {}), ('configure', {'sr_min': 4.0}),
])
def test_worker_rechecks_role_after_controller_revocation_and_keeps_fixed_receipt(
        tmp_path, monkeypatch, action, payload):
    bot, store, row, clock, _ = setup(tmp_path, monkeypatch)
    add_controller(store, row, clock)
    bot.players[CONTROLLER] = {'name': 'controller'}
    local_command = store.request_local(row['lease']['id'], CONTROLLER,
                                        str(uuid.uuid4()), action, payload)

    revoke = {
        'protocol_version': 1, 'operation_id': str(uuid.uuid4()),
        'booking_id': row['lease']['id'], 'host_id': HOST,
        'installation_id': INSTALLATION, 'generation': GENERATION,
        'alias': 'room1', 'session_generation': row['lease']['session_generation'],
        'sequence': 1, 'user_id': OWNER, 'action': 'revoke',
        'payload': {'user_id': CONTROLLER},
    }
    store.receive(revoke)
    assert game.execute(bot, local_command['operation_id'], materialize=lambda *_: None) is False
    assert bot.skipped == []
    assert bot.started == []
    assert bot.aborted == []
    receipt = next(item for item in store.get(row['lease']['id'])['operation_receipts']
                   if item['operation_id'] == local_command['operation_id'])
    assert (receipt['phase'], receipt['error'], receipt['origin']) == (
        'cancelled', 'revoked', 'local')


def test_authorized_worker_start_acknowledges_fixed_application(tmp_path, monkeypatch):
    bot, store, row, _, _ = setup(tmp_path, monkeypatch)
    assert game.command(bot, OWNER, 'start', ['start']) is True
    pending, = store.pending(row['lease']['id'])
    assert game.execute(bot, pending['operation_id'], materialize=lambda *_: None) is True
    assert bot.started == [(OWNER, True)]
    receipt = store.get(row['lease']['id'])['operation_receipts'][0]
    assert (receipt['phase'], receipt['action'], receipt['application']) == (
        'applied', 'start', {'application': 'live'})


def test_configure_acknowledges_before_swapping_and_commits_once(tmp_path, monkeypatch):
    bot, store, row, _, _ = setup(tmp_path, monkeypatch)
    configure_worker(bot)
    old_cfg = copy.deepcopy(bot.cfg)
    old_pool = bot.pool
    request = store.request_local(row['lease']['id'], OWNER, str(uuid.uuid4()),
                                  'configure', {'sr_min': 4.0})
    calls = []
    original_acknowledge = LocalSessions.acknowledge

    def observe_acknowledge(instance, operation_id, phase, *args, **kwargs):
        if operation_id == request['operation_id']:
            calls.append(phase)
            if phase == 'applied':
                assert bot.cfg == old_cfg
                assert bot.pool is old_pool
        return original_acknowledge(instance, operation_id, phase, *args, **kwargs)

    monkeypatch.setattr(LocalSessions, 'acknowledge', observe_acknowledge)
    assert game.execute(bot, request['operation_id'], materialize=lambda _cfg: CandidatePool()) is True
    assert calls == ['applied']
    assert bot.cfg['sr_min'] == 4.0
    assert bot.pool is not old_pool
    saved = store.get(row['lease']['id'])
    assert saved['settings']['sr_min'] == 4.0
    assert saved['settings_revision'] == 1
    receipt = next(item for item in saved['operation_receipts']
                   if item['operation_id'] == request['operation_id'])
    assert receipt['phase'] == 'applied'
    assert receipt['effective_settings']['sr_min'] == 4.0


def test_configure_build_cancellation_from_owner_revoke_keeps_old_worker_state(tmp_path, monkeypatch):
    bot, store, row, clock, _ = setup(tmp_path, monkeypatch)
    add_controller(store, row, clock)
    configure_worker(bot)
    bot.players[CONTROLLER] = {'name': 'controller'}
    old_cfg = copy.deepcopy(bot.cfg)
    old_pool = bot.pool
    request = store.request_local(row['lease']['id'], CONTROLLER, str(uuid.uuid4()),
                                  'configure', {'sr_min': 4.0})

    def revoke_during_build(_cfg):
        store.receive({
            'protocol_version': 1, 'operation_id': str(uuid.uuid4()),
            'booking_id': row['lease']['id'], 'host_id': HOST,
            'installation_id': INSTALLATION, 'generation': GENERATION,
            'alias': 'room1', 'session_generation': row['lease']['session_generation'],
            'sequence': 1, 'user_id': OWNER, 'action': 'revoke',
            'payload': {'user_id': CONTROLLER},
        })
        return CandidatePool()

    assert game.execute(bot, request['operation_id'], materialize=revoke_during_build) is False
    assert bot.cfg == old_cfg
    assert bot.pool is old_pool
    receipt = next(item for item in store.get(row['lease']['id'])['operation_receipts']
                   if item['operation_id'] == request['operation_id'])
    assert (receipt['phase'], receipt['error'], receipt['origin']) == (
        'cancelled', 'revoked', 'local')


def test_configure_build_parent_failure_is_not_overwritten(tmp_path, monkeypatch):
    bot, store, row, _, _ = setup(tmp_path, monkeypatch)
    configure_worker(bot)
    old_cfg = copy.deepcopy(bot.cfg)
    old_pool = bot.pool
    request = store.request_local(row['lease']['id'], OWNER, str(uuid.uuid4()),
                                  'configure', {'sr_min': 4.0})

    def fail_during_build(_cfg):
        store.acknowledge(request['operation_id'], 'failed', error='settings_revision_conflict')
        return CandidatePool()

    assert game.execute(bot, request['operation_id'], materialize=fail_during_build) is False
    assert bot.cfg == old_cfg
    assert bot.pool is old_pool
    receipt = next(item for item in store.get(row['lease']['id'])['operation_receipts']
                   if item['operation_id'] == request['operation_id'])
    assert (receipt['phase'], receipt['error']) == ('failed', 'settings_revision_conflict')


def test_roster_observation_requires_referee_barrier_and_carries_names(tmp_path, monkeypatch):
    bot, _, row, _, _ = setup(tmp_path, monkeypatch)
    bot._hub_rejoin_pending = False
    bot._last_referee_roster_barrier = 7
    bot._required_referee_roster_barrier = 7
    bot._booking_match = {'id': 'map:1000', 'started_at': 1000,
                          'expected_end': 1100, 'ended_at': None,
                          'completed': False, 'aborted': False}

    value = game.observation(bot)
    assert value == {
        'id': row['lease']['id'], 'session_generation': row['lease']['session_generation'],
        'roster_known': True,
        'players': [{'user_id': OWNER, 'username': 'owner'},
                    {'user_id': PLAYER, 'username': 'player'}],
        'match': bot._booking_match,
    }
    bot._last_referee_roster_barrier = 6
    assert game.observation(bot)['roster_known'] is False
    bot.players[PLAYER]['name'] = 'bad\nname'
    assert game.observation(bot) is None


class EventBase:
    def handle_event(self, name, payload):
        if name == 'MatchStarted':
            self._match_started_at = payload['started_at']
        return name


class EventBot(game.BookingEventsMixin, EventBase):
    def __init__(self, *, players=None):
        self.cfg = {'_booking_session': {'id': 'booking'}}
        self.room_id = 777
        self.playing_item_id = 123
        self.playing_map = SimpleNamespace(hit_length=90)
        self._match_started_at = 900
        self._booking_match = None
        self.players = {OWNER: {'name': 'owner'}} if players is None else players


class StatusBase:
    def _update_discord_public_status(self, **kwargs):
        self.base_status_calls.append(kwargs)
        return 'base-status'

    def _notify_discord_lobby_event(self, *args, **kwargs):
        self.base_event_calls.append((args, kwargs))


class FakeDiscordStatus:
    def __init__(self):
        self.calls = []

    def update_public_status(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return True


class StatusBot(game.BookingEventsMixin, StatusBase):
    def __init__(self, root, saved, discord):
        self.cfg = {
            '_host_root': str(root), '_instance_name': 'room1',
            '_booking_session': {key: saved[key] for key in
                                 ('id', 'alias', 'session_generation', 'preset')},
            'discord_public_status_channel_name': 'room1-status',
            'discord_public_status_label': 'Original Room One',
        }
        self.discord_control = discord
        self.base_status_calls = []
        self.base_event_calls = []


def test_match_events_require_live_players_and_match_the_current_match_identity(monkeypatch):
    monkeypatch.setattr(game.time, 'time', lambda: 1100)
    bot = EventBot(players={})
    bot.handle_event('MatchStarted', {'room_id': 777, 'playlist_item_id': 123,
                                      'started_at': 1000})
    assert bot._booking_match is None

    bot.players[OWNER] = {'name': 'owner'}
    bot.handle_event('MatchStarted', {'room_id': 777, 'playlist_item_id': 123,
                                      'started_at': 1001})
    assert bot._booking_match['id'] == '123:1001'
    assert bot._booking_match['expected_end'] == 1091

    # Stale completion from a previous play of the same playlist item is ignored.
    bot.handle_event('MatchCompleted', {'room_id': 777, 'playlist_item_id': 123,
                                        'started_at': 900})
    assert bot._booking_match['ended_at'] is None
    bot.handle_event('MatchCompleted', {'room_id': 777, 'playlist_item_id': 123,
                                        'started_at': 1001})
    assert bot._booking_match['completed'] is True
    assert bot._booking_match['aborted'] is False
    assert bot._booking_match['ended_at'] == 1100


def test_aborted_match_is_recorded_without_completion_credit(monkeypatch):
    monkeypatch.setattr(game.time, 'time', lambda: 1200)
    bot = EventBot()
    bot.handle_event('MatchStarted', {'room_id': 777, 'playlist_item_id': 123,
                                      'started_at': 1100})
    bot.handle_event('MatchAborted', {'room_id': 777, 'playlist_item_id': 123,
                                      'started_at': 1100})
    assert bot._booking_match['aborted'] is True
    assert bot._booking_match['completed'] is False
    assert bot._booking_match['ended_at'] == 1200


def test_private_booking_discord_card_has_no_room_map_roster_or_join_data(tmp_path, monkeypatch):
    _, _, row, _, native = setup(tmp_path, monkeypatch)
    discord = FakeDiscordStatus()
    bot = StatusBot(tmp_path, row['lease'], discord)
    bot.room_id = 987654
    bot.current_map = SimpleNamespace(label='Secret Map', url='https://secret.invalid/map')
    bot.players = {OWNER: {'name': 'Owner Secret'}, PLAYER: {'name': 'Player Secret'}}

    assert bot._update_discord_public_status(force=True) is True
    card, = discord.calls
    assert card['instance'] == 'room1'
    assert card['channel_base_name'] == 'room1-status'
    assert card['display_name'] == 'Original Room One'
    assert card['room_id'] == 0
    assert (card['player_count'], card['spectator_count']) == (0, 0)
    assert card['map_label'] == card['map_url'] == ''
    public_text = repr(card)
    for private_value in ('987654', 'Secret Map', 'secret.invalid', 'Owner Secret', 'Player Secret'):
        assert private_value not in public_text

    bot._notify_discord_lobby_event('MATCH START', 'Secret Map room=987654', map_key='secret')
    assert bot.base_event_calls == []


def test_unbooked_discord_status_and_events_delegate_unchanged(tmp_path, monkeypatch):
    class PublicBot(game.BookingEventsMixin, StatusBase):
        def __init__(self):
            self.cfg = {}
            self.base_status_calls = []
            self.base_event_calls = []

    bot = PublicBot()
    values = {'force': True, 'is_open': True, 'lifecycle': 'Playing', 'rename_channel': False}
    assert bot._update_discord_public_status(**values) == 'base-status'
    assert bot.base_status_calls == [values]
    bot._notify_discord_lobby_event('MAP', 'Public Map', map_key='public')
    assert len(bot.base_event_calls) == 1
