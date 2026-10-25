"""Signed six-mode catalog delivery with the old five-mode reader intact."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from poolrotater_host.catalog_export import MODES, inspect_bundle
from poolrotater_host.catalog_publication import Publisher, initialize
from poolrotater_host.catalog_trust import BootstrapTrust, verified_local_catalog, verified_target
from poolrotater_host.catalog_store import CatalogStore
from poolrotater_host.catalog_server import CatalogServer, route, tls_context
from poolrotater_host.catalog_download import download_catalog
from poolrotater_host.catalog_delivery import HttpsOrigin
from poolrotater_host.catalog_service import configure_refresh
from poolrotater_host.catalog_exclusions import ExclusionPolicy, membership_event
from poolrotater_host.layout import setup, file_lease, read_json
from poolrotater_host.testing.provider_https import certificates
from full_catalog_fixtures import full_export


class SignedTaikoDelivery733Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.taiko_bundle = Path(__file__).resolve().parents[2] / 'catalog' / 'taiko-v1'
        cls.taiko_manifest = inspect_bundle(cls.taiko_bundle)
        cls.work = Path.cwd() / 'scratch' / 'catalog-delivery733'
        cls.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=cls.work)
        cls.base = Path(cls._temp.name)
        cls.custody = cls.base / 'signers'
        cls.served = cls.base / 'served'
        initialize(cls.custody, cls.served)
        cls.trust_root = cls.custody / 'bootstrap.json'
        cls.trust = BootstrapTrust(cls.trust_root.read_bytes(), fixture=True)
        cls.bundles = {mode: full_export(mode) for mode in
                       ('standard', 'mania4', 'mania7', 'catch', 'loved')}
        cls.bundles['taiko'] = cls.taiko_bundle
        with sqlite3.connect((cls.taiko_bundle / 'catalog.db').as_uri() + '?mode=ro', uri=True) as db:
            cls.blocked_map_id, cls.set_id = db.execute(
                'SELECT beatmap_id,beatmapset_id FROM taiko_maps ORDER BY beatmap_id LIMIT 1').fetchone()
        taiko_membership = membership_event(cls.taiko_bundle / 'catalog.db', cls.taiko_bundle, cls.set_id)
        cls.feed = {'format': 1, 'revision': 7, 'events': [
            {'kind': 'beatmap', 'id': 1, 'modes': ['standard'], 'action': 'exclude',
             'reason': 'moderation', 'memberships': {}},
            {'kind': 'beatmap', 'id': 2, 'modes': ['catch'], 'action': 'exclude',
             'reason': 'moderation', 'memberships': {}},
            {'kind': 'beatmap', 'id': 3, 'modes': ['loved'], 'action': 'exclude',
             'reason': 'moderation', 'memberships': {}},
            taiko_membership,
        ]}
        cls.published = Publisher(cls.custody).publish(cls.bundles, exclusions=cls.feed)
        cls.addClassCleanup(cls._temp.cleanup)

    def setUp(self):
        for name in ('managed-host', 'tls', 'https-cache', 'taiko-download', 'publisher-state'):
            path = self.base / name
            if path.exists():
                import shutil
                shutil.rmtree(path)

    def _old_reader_check(self):
        """Run the frozen five-mode source against additive TUF targets."""
        work = Path(__file__).resolve().parents[4]
        old_source = work / 'validation' / 'controller-resilience732' / 'source'
        old_cache = self.base / 'old-reader-cache'
        old_cache.mkdir(mode=0o700)
        code = r'''import json,sys
from pathlib import Path
source,repository,cache,trust_path=map(Path,sys.argv[1:])
sys.path.insert(0,str(source))
from poolrotater_host.catalog_trust import BootstrapTrust,verified_local_catalog,verified_target
trust=BootstrapTrust(trust_path.read_bytes(),fixture=True)
for mode in ('standard','mania4','mania7','catch','loved'):
 with verified_local_catalog(repository,cache/mode,trust,mode) as result:
  assert result.manifest['mode']==mode
for name in ('exclusions.json','exclusions-v2.json','exclusions-loved.json'):
 feed=json.loads(verified_target(repository,cache/name,trust,name))
 assert all('taiko' not in row['modes'] for row in feed['events'])
print('old-five-mode-reader-compatible')
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, str(old_source),
                                 str(self.served), str(old_cache), str(self.trust_root)],
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('old-five-mode-reader-compatible', result.stdout)

    def test_signed_targets_keep_legacy_feed_contract_and_add_taiko_scope(self):
        from poolrotater_host.catalog_exclusions import validate_feed
        from poolrotater_host.catalog_publication import _legacy_feed, _scoped_feed
        trust = self.trust
        legacy_raw = verified_target(self.served, self.base / 'cache-legacy', trust, 'exclusions.json')
        catch_raw = verified_target(self.served, self.base / 'cache-catch', trust, 'exclusions-v2.json')
        loved_raw = verified_target(self.served, self.base / 'cache-loved', trust, 'exclusions-loved.json')
        taiko_raw = verified_target(self.served, self.base / 'cache-taiko', trust, 'exclusions-taiko.json')
        legacy, catch, loved, taiko = [json.loads(raw) for raw in (legacy_raw, catch_raw, loved_raw, taiko_raw)]
        for feed in (legacy, catch, loved, taiko): validate_feed(feed)
        from poolrotater_host.catalog_publication import _cumulative
        canonical_feed = _cumulative(None, self.feed)
        self.assertEqual(legacy, _legacy_feed(canonical_feed))
        self.assertEqual(catch, _scoped_feed(canonical_feed, {'standard', 'mania4', 'mania7', 'catch'}))
        self.assertEqual(loved, _scoped_feed(canonical_feed, {'loved'}))
        self.assertEqual(taiko, _scoped_feed(canonical_feed, {'taiko'}))
        self.assertTrue(all(set(row['modes']) <= {'standard', 'mania4', 'mania7'} for row in legacy['events']))
        self.assertTrue(all('taiko' not in row['modes'] for row in catch['events'] + loved['events']))
        self.assertTrue(all(row['modes'] == ['taiko'] for row in taiko['events']))
        # Existing feed filenames and old mode projections remain unchanged.
        self.assertEqual(set(self.published['content_ids']), set(MODES))
        self._old_reader_check()

    def test_taiko_exclusions_are_applied_only_to_taiko_store_and_set_membership(self):
        root = self.base / 'managed-host'
        setup(root)
        store = CatalogStore.initialize_fixture(root, self.trust)
        reference = store.import_local(self.served, list(MODES))
        self.assertEqual(set(reference['modes']), set(MODES))
        legacy = store.exclusions()
        taiko = store.exclusions(mode='taiko')
        self.assertTrue(all('taiko' not in event['modes'] for event in legacy['events']))
        self.assertTrue(all(event['modes'] == ['taiko'] for event in taiko['events']))
        self.assertTrue(any(event['kind'] == 'beatmapset' and event['id'] == self.set_id
                            for event in taiko['events']))
        blocked = ExclusionPolicy(store, 'taiko', self.taiko_manifest['content_id'])
        standard = ExclusionPolicy(store, 'standard', self.bundles['standard'] and
                                   inspect_bundle(self.bundles['standard'])['content_id'])
        self.assertFalse(blocked.allowed(SimpleNamespace(beatmap_id=self.blocked_map_id)))
        self.assertTrue(blocked.allowed(SimpleNamespace(beatmap_id=1)))
        self.assertFalse(standard.allowed(SimpleNamespace(beatmap_id=1)))
        self.assertTrue(standard.allowed(SimpleNamespace(beatmap_id=self.blocked_map_id)))
        self.assertFalse((store.path / 'exclusions-taiko.json').is_symlink())
        self.assertTrue((store.path / 'exclusions-taiko.json').is_file())

    def test_catalog_server_route_and_https_taiko_download(self):
        # The production server's fixed route table must permit Taiko targets
        # and its Taiko feed while retaining the existing five-mode routes.
        for mode in ('standard', 'mania4', 'mania7', 'catch', 'loved', 'taiko'):
            path, limit = route('/targets/' + mode + '/' + 'a' * 64 + '.catalog.db')
            self.assertEqual(path, 'targets/' + mode + '/' + 'a' * 64 + '.catalog.db')
            self.assertGreater(limit, 0)
        feed_path, _ = route('/targets/' + 'a' * 64 + '.exclusions-taiko.json')
        self.assertTrue(feed_path.endswith('.exclusions-taiko.json'))

        tls = self.base / 'tls'; tls.mkdir(mode=0o700)
        certificates(tls)
        server = CatalogServer(self.served, ('127.0.0.1', 0),
                               context=tls_context(tls / 'server.crt', tls / 'server.key'),
                               protected_uid=os.geteuid())
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        cache = self.base / 'https-cache'
        destination = self.base / 'taiko-download'
        origin = f'https://127.0.0.1:{server.server_port}/'
        try:
            receipt = download_catalog(
                url=origin, cache=cache, trust_root=self.trust_root,
                trust_sha256=self.trust.identity, ca_file=tls / 'ca.crt',
                ca_sha256=hashlib.sha256((tls / 'ca.crt').read_bytes()).hexdigest(),
                destination=destination, mode='taiko')
            self.assertTrue(receipt['ok'])
            self.assertFalse(receipt['activated'])
            downloaded = inspect_bundle(destination / 'catalog')
            self.assertEqual(downloaded['content_id'], self.taiko_manifest['content_id'])
            feed_origin = HttpsOrigin(origin, cache / 'taiko-feed-transport', timeout=5., deadline=30.,
                                      ca_file=tls / 'ca.crt',
                                      ca_sha256=hashlib.sha256((tls / 'ca.crt').read_bytes()).hexdigest())
            raw = verified_target(feed_origin, cache / 'metadata-cache', self.trust,
                                  'exclusions-taiko.json')
            feed = json.loads(raw)
            self.assertTrue(all(event['modes'] == ['taiko'] for event in feed['events']))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_refresh_configuration_serializes_against_scheduler(self):
        state = self.base / 'publisher-state'; state.mkdir(mode=0o700)
        bundles = dict(self.bundles)
        entered = threading.Event(); finished = threading.Event(); errors = []
        def configure():
            entered.set()
            try:
                configure_refresh(state, custody=self.custody, bundles=bundles,
                                  operator_exclusions=None)
            except BaseException as exc: errors.append(exc)
            finally: finished.set()
        # Exercise only scheduler ordering in this ordinary-user fixture; the
        # administrator command's root-owned path checks are covered elsewhere.
        with patch('poolrotater_host.catalog_service.os', SimpleNamespace(geteuid=lambda: 0)), \
             patch('poolrotater_host.catalog_service.protected', side_effect=lambda path, **kwargs: Path(path)):
            with file_lease(self.custody / 'scheduler.lock'):
                worker = threading.Thread(target=configure); worker.start()
                self.assertTrue(entered.wait(5))
                self.assertFalse(finished.wait(.1))
            worker.join(timeout=30)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        config = read_json(state / 'refresh.json')
        self.assertEqual(set(config['inputs']), set(MODES))
        self.assertEqual(Publisher(self.custody).state['version'], 1)
        self.assertFalse(Publisher(self.custody).state['pending'])
        # Configuration prepares paths only; it never changes signed metadata.
        self.assertFalse((self.served / 'metadata' / '3.root.json').exists())


if __name__ == '__main__': unittest.main()
