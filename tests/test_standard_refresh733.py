"""Stopped owner preparation refreshes only the latest signed Standard catalog."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import hashlib
import json
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from poolrotater_host.catalog_export import _identity, _pack, canonical, inspect_bundle, sha256
from poolrotater_host.catalog_store import MODES
from poolrotater_host.config import ConfigError
from poolrotater_host.layout import atomic_json, read_json
from poolrotater_host.managed_host.catalogs import (prepare_store,
    prepare_store_and_refresh_standard, refresh_standard_store)
from poolrotater_host.testing.catalog_repository import FixtureRepository
from tests.full_catalog_fixtures import full_export
from tests.test_catalog_store import StoreFixture


def modified_full_standard(source, destination, label):
    """Clone the full Standard export, change one display value, keep all IDs."""
    source=Path(source);destination=Path(destination)
    shutil.copytree(source,destination)
    (destination/'manifest.json').chmod(0o600)
    manifest=json.loads((destination/'manifest.json').read_text())
    for name in ('catalog.db','dt.db'):
        (destination/name).chmod(0o600)
    with sqlite3.connect(destination/'catalog.db') as db:
        before=db.execute('SELECT COUNT(*) FROM beatmaps').fetchone()[0]
        map_id=db.execute('SELECT MIN(beatmap_id) FROM beatmaps').fetchone()[0]
        db.execute('UPDATE beatmaps SET title=? WHERE beatmap_id=?',(label,map_id))
    catalog=next(item for item in manifest['files'] if item['role']=='catalog')
    catalog.update(sha256=sha256(destination/'catalog.db'),bytes=(destination/'catalog.db').stat().st_size)
    # The real Standard companion binds to the shipped catalog hash. Update
    # that binding while leaving every companion row and map key unchanged.
    with sqlite3.connect(destination/'dt.db') as db:
        db.execute("UPDATE metadata SET value=? WHERE key='source_sha256'",(catalog['sha256'],))
    dt=next(item for item in manifest['files'] if item['role']=='dt')
    dt.update(sha256=sha256(destination/'dt.db'),bytes=(destination/'dt.db').stat().st_size)
    assert db is not None and before>100_000
    (destination/'bundle.tar.gz').unlink()
    manifest['archive']=_pack(destination,manifest['files'])
    manifest.pop('content_id',None)
    manifest['content_id']=hashlib.sha256(canonical(manifest)).hexdigest()
    (destination/'manifest.json').write_bytes(canonical(manifest))
    assert inspect_bundle(destination)['content_id']==manifest['content_id']
    with sqlite3.connect((destination/'catalog.db').as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('SELECT title FROM beatmaps WHERE beatmap_id=?',(map_id,)).fetchone()==(label,)
        assert db.execute('SELECT COUNT(*) FROM beatmaps').fetchone()[0]==before
    return destination


class StandardRefresh733Tests(StoreFixture,unittest.TestCase):
    def _publish_full_set(self):
        for mode in ('standard','mania4','mania7','catch','loved'):
            self.repo.add_bundle(full_export(mode))
        taiko=Path(__file__).resolve().parents[2]/'catalog'/'taiko-v1'
        self.repo.add_bundle(taiko)
        empty={'format':1,'revision':1,'events':[]}
        for target in ('exclusions.json','exclusions-v2.json','exclusions-loved.json','exclusions-taiko.json'):
            self.repo.add_target(target,canonical(empty))
        self.repo.publish(bump=True)
        prepared=prepare_store(self.store,self.repo.folder,owner_lease_held=True)
        self.assertTrue(prepared['all_modes_prepared'])
        self.assertEqual(set(prepared['reference']['modes']),set(MODES))
        return prepared['reference']

    def test_full_standard_refresh_is_additive_resumable_and_idempotent(self):
        before=self._publish_full_set()
        config_before=(self.root/'config/host.json').read_bytes()
        room_files={path.name:path.read_bytes() for path in (self.root/'config/rooms').glob('*.json')}
        latest=modified_full_standard(full_export('standard'),self.base/'standard-new','Latest signed Standard metadata')
        self.repo.add_bundle(latest);self.repo.publish(bump=True)

        # First stop after CatalogStore has staged the signed bundle but before
        # the refresh receipt advances. Recovery must attribute this candidate
        # to the prewritten exact-before journal.
        original_write=self.store._write
        def fail_ready(path,value):
            if path==self.store.path/'.staging/managed-standard-refresh.json' and value.get('phase')=='ready':
                raise OSError('injected journal interruption after stage')
            return original_write(path,value)
        with patch.object(self.store,'_write',side_effect=fail_ready):
            with self.assertRaises(OSError):
                refresh_standard_store(self.store,self.repo.folder,owner_lease_held=True)
        record=read_json(self.store.path/'.staging/managed-standard-refresh.json')
        self.assertEqual(record['phase'],'staging')
        self.assertTrue((self.store.path/'candidate.json').exists())

        # Exercise both native CatalogStore commit crash boundaries using one
        # signed Standard candidate. First interrupt after pending.json is
        # durable but before current.json. Re-entry aborts that exact attempt,
        # retries the same candidate, then interrupts after current.json.
        faults={'pending_writes':0}
        def fail_commit_boundaries(path,value):
            result=original_write(path,value)
            if path==self.store.path/'pending.json':
                faults['pending_writes']+=1
                if faults['pending_writes']==1:
                    raise OSError('injected crash after pending write')
            if path==self.store.path/'current.json':
                raise OSError('injected crash after current reference publication')
            return result
        with patch.object(self.store,'_write',side_effect=fail_commit_boundaries):
            with self.assertRaises(OSError):
                refresh_standard_store(self.store,self.repo.folder,owner_lease_held=True)
            self.assertTrue((self.store.path/'pending.json').exists())
            self.assertEqual(self.store.current(allow_pending=True),before)
            with self.assertRaises(OSError):
                prepare_store_and_refresh_standard(self.store,self.repo.folder,
                                                    owner_lease_held=True)
        self.assertEqual(faults['pending_writes'],2)
        receipt=read_json(self.store.path/'.staging/managed-standard-refresh.json')
        self.assertEqual(receipt['phase'],'ready')
        self.assertEqual(self.store.current(allow_pending=True),receipt['after'])
        integrated=prepare_store_and_refresh_standard(self.store,self.repo.folder,
                                                     owner_lease_held=True)
        self.assertTrue(integrated['standard_refreshed'])
        self.assertTrue(integrated['existing_modes_replaced'])
        self.assertEqual(integrated['reference']['modes']['standard'],inspect_bundle(latest)['content_id'])
        self.assertEqual(integrated['reference']['serial'],before['serial']+1)
        folder,manifest=self.store.version('standard',integrated['reference']['modes']['standard'])
        self.assertEqual(manifest['files'][0]['tables']['beatmaps'],105502)
        with sqlite3.connect((folder/'catalog.db').as_uri()+'?mode=ro',uri=True) as db:
            self.assertEqual(db.execute('SELECT title FROM beatmaps ORDER BY beatmap_id LIMIT 1').fetchone(),
                             ('Latest signed Standard metadata',))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM beatmaps').fetchone()[0],105502)
        self.assertEqual((self.root/'config/host.json').read_bytes(),config_before)
        self.assertEqual({path.name:path.read_bytes() for path in (self.root/'config/rooms').glob('*.json')},room_files)
        self.assertFalse((self.store.path/'pending.json').exists())
        for mode in set(MODES)-{'standard'}:
            self.assertEqual(integrated['reference']['modes'][mode],before['modes'][mode])
        self.assertFalse((self.store.path/'candidate.json').exists())

    def test_foreign_uncommitted_candidate_and_pending_are_never_overwritten(self):
        before=self.store.current()
        proposal={'format':1,'before':before,
                  'after':self.store._reference(before['modes'],serial=before['serial']+1)}
        self.store._write(self.store.path/'candidate.json',proposal)
        with self.assertRaises(ConfigError):
            refresh_standard_store(self.store,self.repo.folder,owner_lease_held=True)
        self.assertEqual(self.store.current(),before)
        self.assertEqual(self.store._read('candidate.json'),proposal)
        self.assertFalse((self.store.path/'.staging/managed-standard-refresh.json').exists())

        (self.store.path/'candidate.json').unlink()
        foreign={'format':1,'before':before,
                 'after':self.store._reference(before['modes'],serial=before['serial']+1)}
        self.store._write(self.store.path/'pending.json',foreign)
        with self.assertRaises(ConfigError):
            prepare_store_and_refresh_standard(self.store,self.repo.folder,
                                               owner_lease_held=True)
        self.assertEqual(self.store._read('pending.json'),foreign)
        self.assertEqual(self.store.current(allow_pending=True),before)


if __name__=='__main__':unittest.main()
