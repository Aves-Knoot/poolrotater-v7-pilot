from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import json
import unittest

from poolrotater_host.bookings.presentation import public_booking_status, purebooking_card
from poolrotater_host.discord_relay.bridge import card


class BookingPresentationTests(unittest.TestCase):
    def test_available_slot_gets_descriptive_https_booking_link(self):
        slot = {'slot_id': 'west', 'label': 'West Pool', 'state': 'available', 'fresh': True,
                'supported_presets': ['standard-auto']}
        status = public_booking_status(slot, 'https://poolrotater.com')
        self.assertEqual(status['state'], 'available')
        self.assertEqual(status['button_label'], 'Book a private lobby')
        self.assertEqual(status['website_url'], 'https://poolrotater.com/private-lobbies/')
        values = purebooking_card(slot)
        self.assertTrue(values['is_open'])
        self.assertEqual(values['room_id'], 0)
        self.assertIn('Book a private lobby', values['extra_fields'][0][1])

    def test_stale_reservation_keeps_state_and_marks_freshness_separately(self):
        slot = {'label': 'West Pool', 'state': 'active', 'fresh': False,
                'room_id': 987654, 'password': 'hidden', 'roster': [{'user_id': 55}]}
        status = public_booking_status(slot)
        self.assertEqual(status['state'], 'active')
        self.assertEqual(status['fields'][-1], ('Freshness', 'Status delayed'))
        values = purebooking_card(slot)
        self.assertEqual(values['lifecycle'], 'Active · Status delayed')
        self.assertEqual(values['room_id'], 0)

    def test_unknown_state_fails_closed_and_unfresh_available_is_not_offered(self):
        self.assertEqual(public_booking_status({'state': 'secret', 'fresh': True})['state'], 'unavailable')
        self.assertEqual(public_booking_status({'state': 'available', 'fresh': False})['state'], 'unavailable')
        self.assertFalse(purebooking_card({'state': 'unknown', 'fresh': True})['is_open'])

    def test_card_omits_room_and_session_secrets_and_sanitizes_label(self):
        slot = {'label': 'West *Pool* <script>', 'state': 'reserved', 'fresh': True,
                'room_id': 987654, 'password': 'secret', 'roster': [{'user_id': 55}],
                'current_map': {'beatmap_id': 99}, 'owner_id': 42, 'join_url': 'https://bad.example'}
        values = purebooking_card(slot)
        encoded = json.dumps(values)
        for forbidden in ('987654', 'secret', '55', '99', '42', 'bad.example', '<script>'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(values['room_name'].split(' · ')[0], 'West Pool script')
        self.assertEqual(values['map_label'], '')
        self.assertEqual(values['map_url'], '')

    def test_invalid_website_origin_never_creates_a_link(self):
        status = public_booking_status({'label': 'Pool', 'state': 'available', 'fresh': True},
                                       'http://poolrotater.com')
        self.assertEqual(status['website_url'], '')
        self.assertEqual(status['button_label'], '')
        values = purebooking_card({'label': 'Pool', 'state': 'available', 'fresh': True},
                                  'https://poolrotater.com/path')
        self.assertEqual(values['extra_fields'], [])

    def test_bridge_booking_branch_and_existing_room_branch(self):
        slot = {'label': 'West Pool', 'state': 'preparing', 'fresh': True}
        values = card({}, 5, booking=slot, website_origin='https://poolrotater.com')
        self.assertEqual(values, purebooking_card(slot))
        # The old room card path still uses the established room-specific fields.
        room = {'display': {}, 'freshness': 'fresh', 'preset': 'standard-fixed', 'operator': 'X',
                'osu_room_id': 123, 'title': 'Community room', 'occupants': 0,
                'min_stars': 1, 'max_stars': 4, 'state': 'idle'}
        old = card(room, 5, now=100)
        self.assertEqual(old['room_id'], 123)
        self.assertEqual(old['room_name'], 'Community room')


if __name__ == '__main__':
    unittest.main()
