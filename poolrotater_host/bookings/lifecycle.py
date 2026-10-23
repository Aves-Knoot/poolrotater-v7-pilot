"""Pure, durable lifecycle reducer for a private booking hosted room."""
from __future__ import annotations

import copy
import math
import re
import uuid

from ..network.contracts import NetworkError


OBSERVATION_FIELDS = {
    'ready', 'room_id', 'incarnation', 'roster_known', 'players', 'match',
    'closed', 'ownership_known', 'updated_at',
}
MATCH_FIELDS = {'id', 'started_at', 'expected_end', 'ended_at', 'completed', 'aborted'}
STATE_FIELDS = {
    'binding', 'owner_id', 'designated_friend_id', 'phase', 'ready_at',
    'claim_deadline', 'claimed_at', 'empty_since', 'last_gameplay_at',
    'match_identity', 'match_started_at', 'match_expected_end',
    'last_gameplay_event', 'match_participant_since',
    'clock_high_water', 'claim_warning_sent', 'idle_warning_300_sent',
    'idle_warning_60_sent', 'close_requested', 'terminal_reason',
}

CLAIM_SECONDS = 600
IDLE_SECONDS = 1800
WARNING_CLAIM_SECONDS = 60
WARNING_IDLE_300_SECONDS = 300
WARNING_IDLE_60_SECONDS = 60
MAX_MATCH_SECONDS = 86400
MATCH_GRACE_SECONDS = 120
FRESH_SECONDS = 30


def _require(condition: bool, code: str = 'invalid_booking_lifecycle') -> None:
    if not condition:
        raise NetworkError(code)


def _number(value, *, minimum=0.0, maximum=2**53 - 1) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def _binding(value):
    _require(type(value) is dict and set(value) == {
        'booking_id', 'session_generation', 'host_id', 'installation_id', 'generation', 'alias'
    })
    for key in ('booking_id', 'host_id', 'installation_id', 'generation'):
        _require(type(value[key]) is str)
        try:
            _require(str(uuid.UUID(value[key])) == value[key])
        except (ValueError, AttributeError):
            raise NetworkError('invalid_booking_binding') from None
    _require(type(value['session_generation']) is str and
             re.fullmatch(r'[0-9a-f]{32}', value['session_generation']) is not None,
             'invalid_booking_binding')
    _require(type(value['alias']) is str and
             re.fullmatch(r'[a-z][a-z0-9_-]{0,23}', value['alias']) is not None,
             'invalid_booking_binding')
    return copy.deepcopy(value)


def initial(binding, owner_id, designated_friend_id=None):
    """Create initial durable state for one exact booking/session binding."""
    copied_binding = _binding(binding)
    _require(type(owner_id) is int and owner_id > 0, 'invalid_booking_owner')
    _require(designated_friend_id is None or
             (type(designated_friend_id) is int and designated_friend_id > 0),
             'invalid_designated_friend')
    _require(designated_friend_id != owner_id, 'invalid_designated_friend')
    return {
        'binding': copied_binding,
        'owner_id': owner_id,
        'designated_friend_id': designated_friend_id,
        'phase': 'preparing',
        'ready_at': None,
        'claim_deadline': None,
        'claimed_at': None,
        'empty_since': None,
        'last_gameplay_at': None,
        'match_identity': None,
        'match_started_at': None,
        'match_expected_end': None,
        'last_gameplay_event': None,
        'match_participant_since': {},
        'clock_high_water': None,
        'claim_warning_sent': False,
        'idle_warning_300_sent': False,
        'idle_warning_60_sent': False,
        'close_requested': False,
        'terminal_reason': None,
    }


def _validate_state(state):
    _require(type(state) is dict and set(state) == STATE_FIELDS)
    _binding(state['binding'])
    _require(type(state['owner_id']) is int and state['owner_id'] > 0)
    friend = state['designated_friend_id']
    _require(friend is None or (type(friend) is int and friend > 0 and friend != state['owner_id']))
    _require(state['phase'] in ('preparing', 'awaiting_claim', 'active', 'closing', 'ended'))
    for key in ('ready_at', 'claim_deadline', 'claimed_at', 'empty_since',
                'last_gameplay_at', 'match_started_at', 'match_expected_end',
                'clock_high_water'):
        value = state[key]
        _require(value is None or _number(value))
    _require(state['match_identity'] is None or
             (type(state['match_identity']) is str and 0 < len(state['match_identity']) <= 128))
    _require(state['last_gameplay_event'] is None or
             (type(state['last_gameplay_event']) is str and 0 < len(state['last_gameplay_event']) <= 140))
    _require(type(state['match_participant_since']) is dict and
             all(type(key) is str and key.isdigit() and int(key) > 0 and _number(value)
                 for key, value in state['match_participant_since'].items()))
    for key in ('claim_warning_sent', 'idle_warning_300_sent', 'idle_warning_60_sent', 'close_requested'):
        _require(type(state[key]) is bool)
    _require(state['terminal_reason'] is None or state['terminal_reason'] in
             ('claim_timeout', 'inactive_timeout', 'host_closed'))
    _require(state['phase'] != 'closing' or state['close_requested'])
    _require((state['phase'] in ('closing', 'ended')) == (state['terminal_reason'] is not None))


def _observation(value):
    _require(type(value) is dict and set(value) == OBSERVATION_FIELDS)
    for key in ('ready', 'roster_known', 'closed', 'ownership_known'):
        _require(type(value[key]) is bool)
    _require(value['room_id'] is None or (type(value['room_id']) is int and value['room_id'] > 0))
    _require(value['incarnation'] is None or
             (type(value['incarnation']) is str and 0 < len(value['incarnation']) <= 128))
    _require(_number(value['updated_at']))
    _require(type(value['players']) is list and len(value['players']) <= 256)
    seen = set()
    for player in value['players']:
        _require(type(player) is dict and set(player) == {'user_id', 'username'})
        _require(type(player['user_id']) is int and player['user_id'] > 0)
        _require(type(player['username']) is str and 0 < len(player['username']) <= 64 and
                 not any(ord(ch) < 32 or ord(ch) == 127 for ch in player['username']))
        _require(player['user_id'] not in seen)
        seen.add(player['user_id'])
    match = value['match']
    if match is not None:
        _require(type(match) is dict and set(match) == MATCH_FIELDS)
        _require(type(match['id']) is str and 0 < len(match['id']) <= 128)
        for key in ('started_at', 'expected_end'):
            _require(_number(match[key]))
        _require(match['ended_at'] is None or _number(match['ended_at']))
        _require(type(match['completed']) is bool and type(match['aborted']) is bool)
        _require(not (match['completed'] and match['aborted']))
        _require(not match['completed'] or match['ended_at'] is not None)
        _require(match['expected_end'] >= match['started_at'] and
                 match['expected_end'] - match['started_at'] <= MAX_MATCH_SECONDS)
        if match['ended_at'] is not None:
            _require(match['ended_at'] >= match['started_at'])
    return value


def _warning(effects, code):
    effects.append({'type': 'warning', 'code': code})


def _activity_window(state, observation, now):
    """Return the same effective inactivity condition/deadline used by advance."""
    player_ids = {p['user_id'] for p in observation['players']}
    idle_deadline = (state['last_gameplay_at'] if state['last_gameplay_at'] is not None
                     else state['claimed_at']) + IDLE_SECONDS
    match = observation['match']
    active_match = (match is not None and match['ended_at'] is None and not match['aborted'] and
                    match['started_at'] <= now and now <= match['expected_end'] + MATCH_GRACE_SECONDS)
    if (active_match and state['match_expected_end'] is not None and
            state['match_started_at'] is not None):
        idle_deadline = max(idle_deadline, state['match_expected_end'] + MATCH_GRACE_SECONDS)
    empty_deadline = state['empty_since'] + IDLE_SECONDS if state['empty_since'] is not None else None
    deadlines = [idle_deadline]
    if empty_deadline is not None:
        deadlines.append(empty_deadline)
    condition = 'empty' if not player_ids else 'map_in_progress' if active_match else 'no_gameplay'
    return condition, min(deadlines), empty_deadline, active_match


def activity_snapshot(state, observation, now):
    """Project a host-authored inactivity countdown only from fresh, known state."""
    _validate_state(state)
    observation = _observation(observation)
    _require(_number(now))
    fresh = 0 <= now - observation['updated_at'] <= FRESH_SECONDS
    high = state['clock_high_water']
    clock_rollback = high is not None and now < high
    confirmed_room = (observation['ready'] and observation['ownership_known'] and
                      observation['room_id'] is not None and observation['incarnation'] is not None)
    if (state['phase'] != 'active' or not fresh or clock_rollback or not confirmed_room or
            not observation['roster_known']):
        return {'condition': 'unknown', 'closes_at': None}
    condition, closes_at, _, _ = _activity_window(state, observation, now)
    return {'condition': condition, 'closes_at': closes_at}


def _close(state, effects, reason):
    state['phase'] = 'closing'
    state['close_requested'] = True
    state['terminal_reason'] = reason
    effects.append({'type': 'close', 'reason': reason})


def advance(state, observation, now):
    """Advance durable booking state using one native observation.

    All timer and roster decisions require a fresh observation whose owner and
    room are known. Invalid inputs raise NetworkError and fail closed.
    """
    _validate_state(state)
    observation = _observation(observation)
    _require(_number(now))
    new = copy.deepcopy(state)
    effects = []
    high = new['clock_high_water']
    fresh = 0 <= now - observation['updated_at'] <= FRESH_SECONDS
    if new['phase'] == 'ended':
        return new, effects
    if not fresh:
        return new, effects
    if high is None or now > high:
        new['clock_high_water'] = now
    clock_rollback = high is not None and now < high

    # Closure is deliberately independent of readiness and roster state.
    if observation['closed'] and observation['ownership_known'] and observation['room_id'] is None:
        if new['phase'] != 'ended':
            new['phase'] = 'ended'
            if not new['close_requested']:
                new['terminal_reason'] = 'host_closed'
        return new, effects

    # No timers can begin until the native controller confirms ownership of a ready room.
    confirmed_room = (observation['ready'] and observation['ownership_known'] and
                      observation['room_id'] is not None and observation['incarnation'] is not None)
    if not confirmed_room or clock_rollback or new['phase'] == 'closing':
        return new, effects

    if new['ready_at'] is None:
        new['ready_at'] = now
        new['claim_deadline'] = now + CLAIM_SECONDS
        new['phase'] = 'awaiting_claim'

    roster_verified = observation['roster_known']
    player_ids = {p['user_id'] for p in observation['players']} if roster_verified else set()
    claimant_present = (new['owner_id'] in player_ids or
                        (new['designated_friend_id'] is not None and
                         new['designated_friend_id'] in player_ids))

    if new['phase'] == 'awaiting_claim':
        if not roster_verified:
            return new, effects
        if claimant_present:
            new['phase'] = 'active'
            new['claimed_at'] = now
            new['last_gameplay_at'] = now
            new['empty_since'] = None if player_ids else now
        else:
            remaining = new['claim_deadline'] - now
            if remaining <= WARNING_CLAIM_SECONDS and not new['claim_warning_sent']:
                _warning(effects, 'claim_required')
                new['claim_warning_sent'] = True
            if remaining <= 0:
                _close(new, effects, 'claim_timeout')
                return new, effects
            return new, effects

    if new['phase'] != 'active':
        return new, effects

    if roster_verified:
        if player_ids:
            new['empty_since'] = None
        elif new['empty_since'] is None:
            new['empty_since'] = now
    else:
        # An unknown interval breaks proof of continuous emptiness.
        new['empty_since'] = None

    match = observation['match']
    if match is not None:
        # Keep only a verified, plausibly live match for its bounded grace.
        verified_match = (match['started_at'] <= now and not match['aborted'] and
                          match['ended_at'] is None and
                          now <= match['expected_end'] + MATCH_GRACE_SECONDS)
        if verified_match:
            if new['match_identity'] != match['id']:
                new['match_participant_since'] = {}
            new['match_identity'] = match['id']
            new['match_started_at'] = match['started_at']
            new['match_expected_end'] = match['expected_end']
            if roster_verified:
                old_participants = new['match_participant_since']
                new['match_participant_since'] = {
                    str(user_id): old_participants.get(str(user_id), now)
                    for user_id in player_ids
                }
                for user_id in player_ids:
                    participant_since = new['match_participant_since'][str(user_id)]
                    participation_event = 'participation:' + match['id'] + ':' + str(user_id)
                    if (now - participant_since >= 60 and
                            new['last_gameplay_event'] != participation_event):
                        new['last_gameplay_at'] = max(new['last_gameplay_at'] or 0, now)
                        new['last_gameplay_event'] = participation_event
                        new['idle_warning_300_sent'] = False
                        new['idle_warning_60_sent'] = False
        else:
            # Ended, aborted, expired, or absent matches cannot suspend inactivity.
            new['match_identity'] = None
            new['match_started_at'] = None
            new['match_expected_end'] = None
            new['match_participant_since'] = {}
        if (match['completed'] and not match['aborted'] and match['ended_at'] is not None and
                match['ended_at'] <= now):
            completion_event = 'completed:' + match['id']
            if new['last_gameplay_event'] != completion_event:
                new['last_gameplay_at'] = max(new['last_gameplay_at'] or 0, match['ended_at'])
                new['last_gameplay_event'] = completion_event
                new['idle_warning_300_sent'] = False
                new['idle_warning_60_sent'] = False
    else:
        new['match_identity'] = None
        new['match_started_at'] = None
        new['match_expected_end'] = None
        new['match_participant_since'] = {}

    _, expiry, empty_deadline, active_match = _activity_window(new, observation, now)
    remaining = expiry - now

    # Unknown roster state cannot prove either a no-show or an expired idle room.
    if not roster_verified:
        return new, effects

    if remaining <= WARNING_IDLE_300_SECONDS and not new['idle_warning_300_sent']:
        _warning(effects, 'inactivity_warning')
        new['idle_warning_300_sent'] = True
    if remaining <= WARNING_IDLE_60_SECONDS and not new['idle_warning_60_sent']:
        _warning(effects, 'inactivity_warning')
        new['idle_warning_60_sent'] = True
    empty_expired = empty_deadline is not None and empty_deadline <= now
    if remaining <= 0 and (empty_expired or not active_match):
        _close(new, effects, 'inactive_timeout')
    return new, effects
