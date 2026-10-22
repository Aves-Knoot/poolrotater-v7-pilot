"""Qualification for the installed stdlib booking portal closure."""

from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poolrotater_host.bookings import portal_entry
from poolrotater_host.bookings.preview import PreviewProvider
from poolrotater_host.bookings.server import PortalServer
from poolrotater_host.bookings.web import Portal
from poolrotater_host.network.hub_service import BOOKING_ASSETS, PREVIEW_MODULES, prepare
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.layout import atomic_json


class _PreviewPool:
    maps = []

    def next_map(self):
        return None


class BookingPackageTests(unittest.TestCase):
    def test_hubkit_contains_static_assets_and_imports_preview_closure_under_isolated_stdlib(self):
        temporary_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix='booking-hubkit-', dir=temporary_root) as temporary:
            kit = Path(temporary) / 'kit'
            receipt = prepare(kit)
            runtime = kit / 'runtime'
            self.assertFalse(receipt['service_installed'])
            self.assertFalse(receipt['hub_started'])
            for name in BOOKING_ASSETS:
                packaged = runtime / name
                self.assertEqual(packaged.read_bytes(), (ROOT / name).read_bytes())
            inventory = json.loads((runtime / 'inventory.json').read_text())['files']
            for name in ('poolrotater_host/bookings/preview.py',
                         'poolrotater_host/bookings/portal_entry.py',
                         'poolrotater_host/bookings/static/index.html',
                         'poolrotater_host/bookings/static/app.js',
                         'poolrotater_host/bookings/static/app.css',
                         *(f'bot/{name}' for name in PREVIEW_MODULES)):
                self.assertIn(name, inventory)
                self.assertEqual(inventory[name], hashlib.sha256((runtime / name).read_bytes()).hexdigest())

            code = r'''import sys
from pathlib import Path
runtime=Path(sys.argv[1]);sys.path.insert(0,str(runtime))
from poolrotater_host.bookings.preview import ExportPreviewProvider, PreviewProvider
from poolrotater_host.bookings.portal_entry import validate_config
from poolrotater_host.managed_host.native import apply_pool_styles
from bot.pool import MapPool
from bot.standard_pool import StandardPool
from bot.mania_pool import ManiaPool
from bot.catch_pool import ProductionCatchPool
for name,module in tuple(sys.modules.items()):
    if name.startswith(('poolrotater_host','bot')) and getattr(module,'__file__',None):
        assert Path(module.__file__).is_relative_to(runtime), (name,module.__file__)
assert 'rolling' not in sys.modules
assert 'signalrcore' not in sys.modules
assert callable(ExportPreviewProvider) and callable(PreviewProvider) and callable(apply_pool_styles)
assert all(x is not None for x in (MapPool,StandardPool,ManiaPool,ProductionCatchPool))
'''
            result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code, str(runtime)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

            hub_root = Path(temporary) / 'hub-state'
            hub_root.mkdir(mode=0o700)
            initialize(hub_root)
            atomic_json(hub_root / 'hub.json', {'format': 1, 'bind': '127.0.0.1', 'port': 9444})
            _, local_config = portal_entry._read_config(hub_root)
            self.assertFalse(local_config['enabled'])
            self.assertFalse((hub_root / 'booking-browser.sqlite3').exists())
            protected_entry = subprocess.run(
                [sys.executable, '-I', '-S', '-B', str(runtime / 'bootstrap.py'),
                 '--root', str(hub_root), 'booking-check'],
                capture_output=True, text=True, timeout=30)
            result = json.loads(protected_entry.stdout)
            if os.geteuid() == 0:
                self.assertEqual(protected_entry.returncode, 0, protected_entry.stderr)
                self.assertEqual(result['enabled'], False)
            else:
                # The ordinary offline namespace runs unprivileged. The
                # installed bootstrap must reject this user-owned staging
                # tree instead of treating it as protected installed code.
                self.assertEqual(protected_entry.returncode, 1)
                self.assertEqual(result, {'ok': False, 'error': 'protected_hub_runtime_refused'})
            self.assertFalse((hub_root / 'booking-browser.sqlite3').exists())

    def test_optional_preview_root_is_local_config_and_is_injected_into_portal(self):
        with tempfile.TemporaryDirectory(prefix='booking-config-') as temporary:
            root = Path(temporary) / 'hub'
            root.mkdir(mode=0o700)
            catalog_root = root / 'trusted-preview-exports'
            catalog_root.mkdir(mode=0o700)
            config = portal_entry.validate_config({
                **portal_entry.DEFAULT_CONFIG,
                'enabled': True,
                'client_id': '123456',
                'client_secret': 'private-preview-secret',
                'preview_catalog_root': str(catalog_root),
            })
            captured = {}

            class FakeStore:
                clock = staticmethod(lambda: 1000.0)

                def __init__(self, _root):
                    pass

            class FakeHubRepository:
                def __init__(self, _store):
                    self.ledger = object()

            class FakePreview:
                def __init__(self, path):
                    captured['preview_path'] = path

            class FakePortal:
                def __init__(self, _root, _repository, **kwargs):
                    captured['portal_preview'] = kwargs['preview']

            class FakeServer:
                def __init__(self, _address, _portal):
                    pass

                def serve_forever(self, **_kwargs):
                    pass

                def server_close(self):
                    pass

            with (patch('poolrotater_host.network.store.Store', FakeStore),
                  patch('poolrotater_host.bookings.hub.Repository', FakeHubRepository),
                  patch('poolrotater_host.bookings.preview.ExportPreviewProvider', FakePreview),
                  patch('poolrotater_host.bookings.portal_entry.Portal', FakePortal),
                  patch('poolrotater_host.bookings.portal_entry.PortalServer', FakeServer)):
                portal_entry._serve(root, config)
            self.assertEqual(captured['preview_path'], str(catalog_root))
            self.assertIsInstance(captured['portal_preview'], FakePreview)
            self.assertNotIn(config['client_secret'], json.dumps(portal_entry.public_config_summary(config)))

    def test_browser_preview_cannot_select_a_catalog_path(self):
        with tempfile.TemporaryDirectory(prefix='booking-no-path-') as temporary:
            calls = {'factory': 0}

            def configs(_preset):
                return {'ruleset_id': 0, 'sr_min': 3.5, 'sr_max': 4.5,
                        'auto_difficulty': False}

            def factory(_cfg):
                calls['factory'] += 1
                return _PreviewPool()

            provider = PreviewProvider(configs, factory)

            class Repository:
                pass

            portal = Portal(Path(temporary), Repository(), origin='https://poolrotater.com',
                            enabled=True, preview=provider, clock=lambda: 1000.0)
            token = portal.sessions.create({'id': 44, 'username': 'Owner'})
            session = portal.sessions.get(token)
            headers = {'Cookie': f'__Host-poolrotater-booking={token}',
                       'Content-Type': 'application/json',
                       'Origin': 'https://poolrotater.com',
                       'X-CSRF-Token': session['csrf_token']}

            top_level = portal.handle('POST', '/private-lobbies/api/preview', headers,
                json.dumps({'preset': 'standard-fixed', 'settings': {},
                            'catalog_path': str(Path(temporary) / 'evil.db')}).encode())
            self.assertEqual(top_level.status, 400)
            nested = portal.handle('POST', '/private-lobbies/api/preview', headers,
                json.dumps({'preset': 'standard-fixed', 'settings': {
                    'catalog_path': str(Path(temporary) / 'evil.db')}}).encode())
            self.assertEqual(nested.status, 400)
            self.assertEqual(calls['factory'], 0)


if __name__ == '__main__':
    unittest.main()
