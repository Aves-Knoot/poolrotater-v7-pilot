"""Real Synchronizer -> Cache -> ruleset readers for mixed Taiko hosts."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from contextlib import contextmanager
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from bot.rating_outbox import CATCH_KEY_BASE, TAIKO_KEY_BASE
from poolrotater_host.layout import atomic_json, load, setup
from poolrotater_host.network.contracts import fingerprint
from poolrotater_host.ratings.reader import NetworkReader
from poolrotater_host.ratings.snapshots import Cache, Publisher
from poolrotater_host.ratings.sync import Synchronizer


class TaikoRatingSnapshot733Tests(unittest.TestCase):
    def test_mixed_host_sync_fetches_v3_and_all_readers_use_the_same_fresh_point(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'host'
            setup(root, presets=['taiko-fixed', 'standard-fixed', 'catch-fixed'])
            marker, _ = load(root)
            epoch = str(uuid.uuid4())
            host_id = str(uuid.uuid4())
            hub_profile = {'host_id': host_id, 'server_epoch': epoch, 'account_id': 123}

            hub_db = Path(td) / 'hub.db'
            with sqlite3.connect(hub_db) as db:
                db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.executemany('INSERT INTO meta VALUES(?,?)', [
                    ('ratings_watermark', '18'), ('epoch', epoch)])
                db.execute('CREATE TABLE ratings_current(beatmap_id INTEGER,user_id INTEGER,rating INTEGER,suppressed INTEGER)')
                db.executemany('INSERT INTO ratings_current VALUES(?,?,?,0)', [
                    (123, 7, 1), (CATCH_KEY_BASE + 123, 8, -1),
                    (TAIKO_KEY_BASE + 123, 9, 1)])

            publisher_root = Path(td) / 'publisher'
            publisher_root.mkdir(mode=0o700)

            class Store:
                root = publisher_root
                @staticmethod
                def clock(): return 42.0
                @contextmanager
                def transaction(self):
                    db = sqlite3.connect(hub_db)
                    db.row_factory = sqlite3.Row
                    try: yield db
                    finally: db.close()

            class Policy:
                @staticmethod
                def check(_db): return {'operations': []}, None

            class Repository:
                store = Store()
                policy = Policy()

            publisher = Publisher(Repository())
            seen_routes = []

            class Transport:
                def request(self, method, route, *, token, body=None):
                    seen_routes.append(route)
                    if route.endswith('/snapshots/current'):
                        return publisher.current(context=True, taiko=route.startswith('/v3/'))
                    if '/snapshot-page?' in route:
                        from urllib.parse import parse_qs, urlsplit
                        query = parse_qs(urlsplit(route).query)
                        return publisher.read_page(query['content_id'][0], int(query['index'][0]),
                                                   context=True, taiko=route.startswith('/v3/'))
                    raise AssertionError(f'unexpected request {method} {route}')

            class Network:
                lock_path = root / 'state/network.lock'
                def profile(self): return hub_profile
                def credential(self):
                    return {'host_id': host_id, 'server_epoch': epoch, 'credential': 'invented-token'}
                @staticmethod
                def pending(_profile): return None
                @staticmethod
                def check_account(_account_id): return None
                @staticmethod
                def transport(_profile): return Transport()

            atomic_json(root / 'config/ratings.json', {
                'format': 1, 'installation_id': marker['installation_id'],
                'generation': marker['generation'], 'share_ratings': True,
                'network_profile_sha256': fingerprint(hub_profile),
                'host_id': host_id, 'server_epoch': epoch})

            receipt = Synchronizer(root, network=Network()).fetch_snapshots()
            self.assertEqual(receipt['phase'], 'activated')
            self.assertIn('/v3/ratings/snapshots/current', seen_routes)
            self.assertTrue(all(route.startswith('/v3/ratings/') for route in seen_routes))

            readers = {mode: NetworkReader(root, server_epoch=epoch, ruleset_id=mode)
                       for mode in (0, 1, 2)}
            self.assertEqual(readers[0].tallies([123]), {123: (1, 0)})
            self.assertEqual(readers[1].tallies([123]), {123: (1, 0)})
            self.assertEqual(readers[2].tallies([123]), {123: (0, 1)})
            self.assertEqual({reader.status['watermark'] for reader in readers.values()}, {18})
            self.assertEqual({reader.status['phase'] for reader in readers.values()}, {'current'})

            # If an older client cache is newer than the last v3 refresh,
            # Standard/Catch may use it while Taiko stays bound to v3. v2
            # never contains or reinterprets the Taiko key range.
            with sqlite3.connect(hub_db) as db:
                db.execute("UPDATE meta SET value='19' WHERE key='ratings_watermark'")
                db.execute('INSERT INTO ratings_current VALUES(123,10,-1,0)')
            Cache(root).download(Transport(), 'invented-token', server_epoch=epoch,
                                 context=True)
            self.assertEqual(readers[0].tallies([123]), {123: (1, 1)})
            self.assertEqual(readers[2].tallies([123]), {123: (0, 1)})
            self.assertEqual(readers[0].status['watermark'], 19)
            self.assertEqual(readers[1].tallies([123]), {123: (1, 0)})
            self.assertEqual(readers[1].status['watermark'], 18)


if __name__ == '__main__':
    unittest.main()
