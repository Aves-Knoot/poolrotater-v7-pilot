from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from poolrotater_host.bookings import portal_entry
from poolrotater_host.network.contracts import NetworkError
from poolrotater_host.network.hub_layout import initialize


class PortalConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = __import__('tempfile').TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'hub'
        initialize(self.root)
        self.config_path = self.root / 'bookings-site.json'

    def write(self, value, mode=0o600):
        self.config_path.write_text(json.dumps(value), encoding='utf-8')
        self.config_path.chmod(mode)

    def default(self):
        return {'format': 1, 'enabled': False, 'origin': 'https://poolrotater.com',
                'bind': '127.0.0.1', 'port': 9446}

    def test_missing_config_uses_disabled_defaults_without_mutating_hub(self):
        before = set(self.root.iterdir())
        root, config = portal_entry._read_config(self.root)
        self.assertEqual(root, self.root)
        self.assertEqual(config, self.default())
        self.assertEqual(set(self.root.iterdir()), before)

    def test_check_reads_only_local_config_and_never_prints_oauth_secret(self):
        self.write(self.default())
        from io import BytesIO
        output = BytesIO()
        with patch('urllib.request.urlopen', side_effect=AssertionError('network use')):
            with patch('sys.stdout') as stdout:
                stdout.buffer = output
                self.assertEqual(portal_entry.main(['--root', str(self.root), '--check']), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result, {'ok': True, 'enabled': False, 'origin': 'https://poolrotater.com',
                                  'bind': '127.0.0.1', 'port': 9446, 'oauth_configured': False})
        self.assertFalse((self.root / 'network.sqlite3').exists())

    def test_enabled_requires_private_oauth_fields_and_fixed_redirect(self):
        value = {**self.default(), 'enabled': True}
        with self.assertRaises(NetworkError) as missing:
            portal_entry.validate_config(value)
        self.assertEqual(missing.exception.code, 'booking_oauth_configuration_required')
        test_secret = 'T' * 32
        value.update(client_id='123456', client_secret=test_secret)
        config = portal_entry.validate_config(value)
        self.assertEqual(config['origin'], 'https://poolrotater.com')
        callback = config['origin'] + '/private-lobbies/oauth/callback'
        self.assertEqual(callback, 'https://poolrotater.com/private-lobbies/oauth/callback')
        self.assertNotIn(test_secret, json.dumps(portal_entry.public_config_summary(config)))

    def test_config_file_must_be_owned_private_regular_nonsymlink(self):
        self.write(self.default(), 0o644)
        with self.assertRaises(NetworkError):
            portal_entry._read_config(self.root)
        self.config_path.unlink()
        target = self.root / 'elsewhere.json'
        target.write_text(json.dumps(self.default()), encoding='utf-8')
        target.chmod(0o600)
        self.config_path.symlink_to(target)
        with self.assertRaises(NetworkError):
            portal_entry._read_config(self.root)

    def test_invalid_origin_bind_port_and_partial_oauth_are_rejected(self):
        for change in ({'origin': 'http://poolrotater.com'}, {'origin': 'https://poolrotater.com/path'},
                       {'bind': '0.0.0.0'}, {'port': True}, {'port': 80},
                       {'client_id': '123'}):
            with self.subTest(change=change), self.assertRaises(NetworkError):
                portal_entry.validate_config({**self.default(), **change})

    def test_oauth_cookie_flags_and_wiki_cookie_are_separate(self):
        from poolrotater_host.bookings.web import Portal
        class Ledger:
            def public_slots(self): return []
        class OAuth:
            def authorization_url(self, state): return 'https://osu.ppy.sh/oauth/authorize?state=' + state
        portal = Portal(self.root, Ledger(), origin='https://poolrotater.com',
                        oauth=OAuth(), enabled=True, clock=lambda: 1000)
        response = portal.handle('GET', '/private-lobbies/oauth/start', {})
        cookie = dict(response.headers)['Set-Cookie']
        self.assertIn('__Host-poolrotater-booking-oauth=', cookie)
        self.assertIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        me = portal.handle('GET', '/private-lobbies/api/me',
                           {'Cookie': '__Host-poolrotater-wiki=unrelated'})
        self.assertFalse(json.loads(me.body)['authenticated'])

    def test_disabled_serve_refuses_before_store_or_listener_creation(self):
        self.write(self.default())
        with self.assertRaises(NetworkError) as error:
            portal_entry._serve(self.root, portal_entry.validate_config(self.default()))
        self.assertEqual(error.exception.code, 'booking_portal_disabled')
        self.assertFalse((self.root / 'network.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
