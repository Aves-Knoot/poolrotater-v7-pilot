import copy
import json
import unittest

from poolrotater_host.bookings.lifecycle import advance, initial
from poolrotater_host.network.contracts import NetworkError


OWNER = 101
FRIEND = 202
BINDING = {
    'booking_id': '11111111-1111-4111-8111-111111111111',
    'session_generation': 'a' * 32,
    'host_id': '22222222-2222-4222-8222-222222222222',
    'installation_id': '33333333-3333-4333-8333-333333333333',
    'generation': '44444444-4444-4444-8444-444444444444',
    'alias': 'room1',
}


def observation(now, *, ready=True, room_id=987, incarnation='inc-1',
                roster_known=True, players=(), match=None, closed=False,
                ownership_known=True, updated_at=None):
    return {
        'ready': ready,
        'room_id': room_id,
        'incarnation': incarnation,
        'roster_known': roster_known,
        'players': [{'user_id': uid, 'username': 'player'} for uid in players],
        'match': copy.deepcopy(match),
        'closed': closed,
        'ownership_known': ownership_known,
        'updated_at': now if updated_at is None else updated_at,
    }


def begin_claim(now=1000, designated_friend_id=None):
    state = initial(BINDING, OWNER, designated_friend_id)
    state, effects = advance(state, observation(now, players=[999]), now)
    assert state['phase'] == 'awaiting_claim' and effects == []
    return state


def claim(state, now=1001, user=OWNER):
    return advance(state, observation(now, players=[user]), now)


class BookingLifecycleTests(unittest.TestCase):
    def test_initial_copies_binding_and_has_durable_complete_shape(self):
        binding = copy.deepcopy(BINDING)
        state = initial(binding, OWNER, FRIEND)
        binding['alias'] = 'changed'
        self.assertEqual(state['binding'], BINDING)
        self.assertEqual(state['phase'], 'preparing')
        self.assertIsNone(state['clock_high_water'])
        self.assertFalse(state['close_requested'])
        self.assertEqual(set(state), {
            'binding', 'owner_id', 'designated_friend_id', 'phase', 'ready_at',
            'claim_deadline', 'claimed_at', 'empty_since', 'last_gameplay_at',
            'match_identity', 'match_started_at', 'match_expected_end',
            'last_gameplay_event', 'match_participant_since', 'clock_high_water', 'claim_warning_sent',
            'idle_warning_300_sent', 'idle_warning_60_sent', 'close_requested',
            'terminal_reason',
        })

    def test_stale_or_unknown_ownership_never_starts_or_expires_claim(self):
        state = initial(BINDING, OWNER)
        state, effects = advance(state, observation(1000, updated_at=969), 1000)
        self.assertEqual((state['phase'], state['ready_at'], effects), ('preparing', None, []))
        state, effects = advance(state, observation(100000, updated_at=1), 100000)
        self.assertIsNone(state['clock_high_water'])
        self.assertEqual(effects, [])
        state, effects = advance(state, observation(1001, ownership_known=False), 1001)
        self.assertEqual((state['phase'], state['ready_at'], effects), ('preparing', None, []))

    def test_claim_warning_and_timeout_are_once_and_survive_restart(self):
        state = begin_claim(1000)
        state, effects = advance(state, observation(1540, players=[999]), 1540)
        self.assertEqual(effects, [{'type': 'warning', 'code': 'claim_required'}])
        state, effects = advance(json.loads(json.dumps(state)), observation(1541, players=[999]), 1541)
        self.assertEqual(effects, [])
        state, effects = advance(state, observation(1600, players=[999]), 1600)
        self.assertEqual(state['phase'], 'closing')
        self.assertEqual(state['terminal_reason'], 'claim_timeout')
        self.assertEqual(effects, [{'type': 'close', 'reason': 'claim_timeout'}])
        state, effects = advance(state, observation(1601, players=[999]), 1601)
        self.assertEqual(effects, [])

    def test_owner_or_designated_friend_can_claim_but_random_player_cannot(self):
        state = begin_claim(1000, FRIEND)
        state, effects = advance(state, observation(1010, players=[999]), 1010)
        self.assertEqual((state['phase'], effects), ('awaiting_claim', []))
        state, effects = claim(state, 1020, FRIEND)
        self.assertEqual((state['phase'], state['claimed_at'], effects), ('active', 1020, []))

    def test_unknown_roster_does_not_claim_or_start_empty_timer(self):
        state = begin_claim(1000)
        state, effects = advance(state, observation(1010, roster_known=False, players=[OWNER]), 1010)
        self.assertEqual((state['phase'], state['claimed_at'], effects), ('awaiting_claim', None, []))

    def test_unknown_roster_holds_claim_and_inactivity_expiry(self):
        state = begin_claim(1000)
        state, effects = advance(state, observation(1600, roster_known=False, players=[]), 1600)
        self.assertEqual((state['phase'], effects), ('awaiting_claim', []))
        state, effects = advance(state, observation(1601, players=[]), 1601)
        self.assertEqual(state['phase'], 'closing')
        self.assertEqual(effects[-1], {'type': 'close', 'reason': 'claim_timeout'})

        active, _ = claim(begin_claim(1000), 1001)
        active, _ = advance(active, observation(2801, roster_known=False, players=[]), 2801)
        self.assertEqual(active['phase'], 'active')
        active, effects = advance(active, observation(2802, players=[OWNER]), 2802)
        self.assertEqual(active['phase'], 'closing')
        self.assertEqual(effects[-1], {'type': 'close', 'reason': 'inactive_timeout'})

    def test_unknown_roster_breaks_continuous_empty_timer(self):
        state, _ = claim(begin_claim(1000), 1001)
        state, _ = advance(state, observation(1100, players=[]), 1100)
        completed = {
            'id': 'map-before-gap', 'started_at': 1200, 'expected_end': 1800,
            'ended_at': 2000, 'completed': True, 'aborted': False,
        }
        state, _ = advance(state, observation(2000, players=[], match=completed), 2000)
        self.assertEqual(state['empty_since'], 1100)
        state, _ = advance(state, observation(2400, roster_known=False, players=[]), 2400)
        self.assertIsNone(state['empty_since'])
        # Renew gameplay after the unknown gap, then begin a new known-empty interval.
        second_completed = {**completed, 'id': 'map-after-gap', 'ended_at': 3000}
        state, _ = advance(state, observation(3000, players=[OWNER], match=second_completed), 3000)
        state, _ = advance(state, observation(3100, players=[]), 3100)
        self.assertEqual(state['empty_since'], 3100)
        state, effects = advance(state, observation(4500, players=[]), 4500)
        self.assertEqual(state['phase'], 'active')
        self.assertNotIn({'type': 'close', 'reason': 'inactive_timeout'}, effects)

    def test_abort_clears_match_deadline_and_active_match_gets_only_bounded_grace(self):
        state, _ = claim(begin_claim(1000), 1001)
        aborted = {
            'id': 'aborted', 'started_at': 1050, 'expected_end': 2000,
            'ended_at': 1070, 'completed': False, 'aborted': True,
        }
        state, _ = advance(state, observation(1100, players=[OWNER], match=aborted), 1100)
        self.assertIsNone(state['match_identity'])
        self.assertIsNone(state['match_expected_end'])

        active_match = {
            'id': 'bounded', 'started_at': 1100, 'expected_end': 3000,
            'ended_at': None, 'completed': False, 'aborted': False,
        }
        state, _ = advance(state, observation(3100, players=[OWNER], match=active_match), 3100)
        state, effects = advance(state, observation(3119, players=[OWNER], match=active_match), 3119)
        self.assertEqual(state['phase'], 'active')
        self.assertNotIn({'type': 'close', 'reason': 'inactive_timeout'}, effects)
        state, effects = advance(state, observation(3121, players=[OWNER], match=active_match), 3121)
        self.assertEqual(state['phase'], 'closing')
        self.assertEqual(effects[-1], {'type': 'close', 'reason': 'inactive_timeout'})

    def test_empty_and_no_gameplay_timeout_with_deduplicated_warnings(self):
        state, _ = claim(begin_claim(1000), 1001)
        state, effects = advance(state, observation(2501, players=[]), 2501)
        self.assertEqual(effects, [{'type': 'warning', 'code': 'inactivity_warning'}])
        state, effects = advance(json.loads(json.dumps(state)), observation(2502, players=[]), 2502)
        self.assertEqual(effects, [])
        state, effects = advance(state, observation(2741, players=[]), 2741)
        self.assertEqual(effects, [{'type': 'warning', 'code': 'inactivity_warning'}])
        state, effects = advance(state, observation(2801, players=[]), 2801)
        self.assertEqual(state['phase'], 'closing')
        self.assertEqual(state['terminal_reason'], 'inactive_timeout')
        self.assertEqual(effects, [{'type': 'close', 'reason': 'inactive_timeout'}])

    def test_active_participation_and_completed_map_renew_without_rapid_abort(self):
        state, _ = claim(begin_claim(1000), 1001)
        quick_abort = {
            'id': 'abort-1', 'started_at': 1002, 'expected_end': 1602,
            'ended_at': 1012, 'completed': False, 'aborted': True,
        }
        state, _ = advance(state, observation(1050, players=[OWNER], match=quick_abort), 1050)
        self.assertEqual(state['last_gameplay_at'], 1001)

        long_match = {
            'id': 'map-1', 'started_at': 1100, 'expected_end': 86500,
            'ended_at': None, 'completed': False, 'aborted': False,
        }
        state, _ = advance(state, observation(1200, players=[OWNER], match=long_match), 1200)
        self.assertEqual(state['last_gameplay_at'], 1001)
        state, _ = advance(state, observation(1260, players=[OWNER], match=long_match), 1260)
        self.assertEqual(state['last_gameplay_at'], 1260)
        self.assertEqual(state['match_expected_end'], 86500)
        # A stuck PLAYING observation cannot refresh participation or extend the bounded end.
        state, effects = advance(state, observation(87000, players=[OWNER], match=long_match), 87000)
        self.assertEqual(state['last_gameplay_at'], 1260)
        self.assertEqual(state['phase'], 'closing')
        self.assertEqual(effects[-1], {'type': 'close', 'reason': 'inactive_timeout'})

    def test_completed_map_renews_once_and_empty_room_still_expires(self):
        state, _ = claim(begin_claim(1000), 1001)
        completed = {
            'id': 'map-done', 'started_at': 1100, 'expected_end': 1700,
            'ended_at': 1500, 'completed': True, 'aborted': False,
        }
        state, _ = advance(state, observation(1600, players=[], match=completed), 1600)
        self.assertEqual(state['last_gameplay_at'], 1500)
        state, _ = advance(state, observation(2000, players=[], match=completed), 2000)
        self.assertEqual(state['last_gameplay_at'], 1500)
        state, effects = advance(state, observation(3400, players=[], match=None), 3400)
        self.assertEqual(state['phase'], 'closing')
        self.assertEqual(effects[-1], {'type': 'close', 'reason': 'inactive_timeout'})

    def test_clock_rollback_suppresses_expiry_until_high_water_is_reached(self):
        state, _ = claim(begin_claim(1000), 1001)
        state, effects = advance(state, observation(2501, players=[]), 2501)
        self.assertEqual(effects, [{'type': 'warning', 'code': 'inactivity_warning'}])
        state, effects = advance(state, observation(1500, players=[]), 1500)
        self.assertEqual(state['clock_high_water'], 2501)
        self.assertNotEqual(state['phase'], 'closing')
        self.assertEqual(effects, [])
        state, effects = advance(state, observation(2501, players=[]), 2501)
        self.assertEqual(effects, [])
        state, effects = advance(state, observation(2801, players=[]), 2801)
        self.assertEqual(effects[-1], {'type': 'close', 'reason': 'inactive_timeout'})

    def test_closing_observation_unknown_holds_then_confirmed_close_ends(self):
        state = begin_claim(1000)
        state, effects = advance(state, observation(1600, players=[]), 1600)
        self.assertEqual(state['phase'], 'closing')
        state, effects = advance(state, observation(1601, room_id=None, incarnation=None,
                                                       closed=True, ownership_known=False), 1601)
        self.assertEqual((state['phase'], effects), ('closing', []))
        state, effects = advance(state, observation(1602, room_id=None, incarnation=None,
                                                       closed=True), 1602)
        self.assertEqual((state['phase'], effects), ('ended', []))
        state, effects = advance(state, observation(1603, room_id=None, incarnation=None,
                                                       closed=True), 1603)
        self.assertEqual((state['phase'], effects), ('ended', []))

    def test_invalid_observations_and_match_over_24_hours_fail_closed(self):
        state = initial(BINDING, OWNER)
        malformed = observation(1000)
        malformed['players'] = [{'user_id': True, 'username': 'bad'}]
        with self.assertRaises(NetworkError):
            advance(state, malformed, 1000)
        overlong = observation(1000, match={
            'id': 'too-long', 'started_at': 1000, 'expected_end': 1000 + 86401,
            'ended_at': None, 'completed': False, 'aborted': False,
        })
        with self.assertRaises(NetworkError):
            advance(state, overlong, 1000)


if __name__ == '__main__':
    unittest.main()
