"""Upgrade/prewarm seams for adding Taiko without changing existing room state."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from poolrotater_host.catalog_export import _pack, canonical, inspect_bundle
from poolrotater_host.catalog_store import MODES
from poolrotater_host.config import ConfigError
from poolrotater_host.layout import read_json
from poolrotater_host.managed_host import catalogs as managed_catalogs
from poolrotater_host.managed_host.native import NativeAdapter
from tests.test_catalog_store import StoreFixture
from tests.test_taiko_catalog733 import (ANALYSIS, CAPABILITIES, EXCLUDED,
    MAP_COLUMNS, READER, SHA, _canonical, map_tuple)


def taiko_bundle(folder):
    """Build one compact, invented, schema-valid Taiko target for TUF tests."""
    folder=Path(folder);folder.mkdir()
    db_path=folder/'catalog.db'
    with sqlite3.connect(db_path) as db:
        columns=','.join(f'{name} {kind}'+(' PRIMARY KEY' if name=='beatmap_id' else '')
                         for name,kind in MAP_COLUMNS)
        db.execute(f'CREATE TABLE taiko_maps ({columns})')
        db.execute('CREATE TABLE taiko_auto_calibration '
                   '(pp REAL PRIMARY KEY,sr REAL NOT NULL,band_floor INTEGER NOT NULL,'
                   'players INTEGER NOT NULL,scores INTEGER NOT NULL,charts INTEGER NOT NULL)')
        row=map_tuple(7654321,sr=4.0)
        db.execute('INSERT INTO taiko_maps VALUES ('+','.join('?' for _ in MAP_COLUMNS)+')',row)
        db.execute('CREATE INDEX taiko_selection_window ON taiko_maps(sr,hit_length,status)')
        db.execute('CREATE INDEX taiko_song_spacing ON taiko_maps(artist,title)')
        db.executemany('INSERT INTO taiko_auto_calibration VALUES (?,?,?,?,?,?)',
                       [(500.,3.0,0,6,24,4),(1500.,4.0,1000,6,24,5)])
    files=[{'name':'catalog.db','role':'catalog','sha256':hashlib.sha256(db_path.read_bytes()).hexdigest(),
            'bytes':db_path.stat().st_size,'tables':{'taiko_maps':1,'taiko_auto_calibration':2}}]
    row_map=dict(zip((name for name,_ in MAP_COLUMNS),row))
    qualification={'native_ranked_approved':1,'converted_ranked_approved':0,
                   'official_taiko_nm_only':True,'mods':[],'clock_rate':1.0}
    calibration={'format':1,'kind':'taiko_nm_topplay_exposure_heuristic',
        'qualification':'ruleset-1 passed no-mod top scores joined to exact-source official Taiko NM chart stars',
        'convergence':'fixture only; no convergence claim','not_a_claim':'fixture calibration only',
        'profiles_available':12,'profiles_with_qualified_nm_scores':12,'coverage_fraction':1.0,
        'minimum_band_profiles':5,'minimum_band_scores':20,'point_count':2}
    analysis={**ANALYSIS,'qualification':qualification,'auto_calibration':calibration,
              'exclusion_counts':{},
              'catalog_rows_sha256':hashlib.sha256(_canonical(row_map)).hexdigest()}
    sources={name:{'sha256':SHA,'bytes':1} for name in
             ('selection_map_research','analysis_maps','calculation_receipts','source_metadata',
              'profile_panel','native_coverage','conversion_inputs')}
    sources['source_metadata']['bytes']=7*1024**3
    archive=_pack(folder,files)
    manifest={'format':1,'export_schema':1,'reader':READER,'mode':'taiko','ruleset':1,'keys':None,
              'files':files,'sources':sources,'analysis':analysis,'capabilities':CAPABILITIES,
              'exclusions':{'revision':1,'beatmap_ids':list(EXCLUDED)},'archive':archive}
    manifest['content_id']=hashlib.sha256(canonical(manifest)).hexdigest()
    (folder/'manifest.json').write_bytes(canonical(manifest))
    inspect_bundle(folder)
    return folder


class TaikoCatalogPrewarmTests(StoreFixture,unittest.TestCase):
    def _old_store(self):
        from full_catalog_fixtures import full_export
        from poolrotater_host.catalog_export import canonical
        # Emulate the v7.3.2 complete set. Taiko is the only newly admitted
        # mode, so the automatic prewarm has one additive target to stage.
        for mode in ('catch','loved'):
            self.repo.add_bundle(full_export(mode))
        self.repo.add_target('exclusions.json',canonical({'format':1,'revision':1,'events':[]}))
        self.repo.add_target('exclusions-v2.json',canonical({'format':1,'revision':1,'events':[]}))
        self.repo.add_target('exclusions-loved.json',canonical({'format':1,'revision':1,'events':[]}))
        self.repo.publish(bump=True)
        old_modes=sorted(set(MODES)-{'taiko'})
        self.assertEqual(set(old_modes),{'standard','mania4','mania7','catch','loved'})
        return self.store.import_local(self.repo.folder,old_modes)

    def test_upgrade_prewarm_adds_taiko_keeps_old_modes_and_recovers_once(self):
        before=self._old_store()
        room_files={p.name:p.read_bytes() for p in (self.root/'config/rooms').glob('*.json')}
        host_bytes=(self.root/'config/host.json').read_bytes()
        with self.assertRaises(ConfigError):
            managed_catalogs.prepare_store(self.store,self.repo.folder,owner_lease_held=True)
        self.assertEqual(self.store.current(),before)
        taiko=taiko_bundle(self.base/'taiko-bundle')
        self.repo.add_bundle(taiko)
        self.repo.add_target('exclusions-taiko.json',canonical({'format':1,'revision':1,'events':[]}))
        self.repo.publish(bump=True)

        # Interrupt after a complete candidate is staged but before reference
        # commit, then replay the same prewarm. No partial set becomes current.
        with patch.object(self.store,'commit',side_effect=OSError('simulated interruption')):
            with self.assertRaises(OSError):
                managed_catalogs.prepare_store(self.store,self.repo.folder,owner_lease_held=True)
        self.assertEqual(self.store.current(),before)
        self.assertEqual(read_json(self.store.path/'.staging/managed-prewarm.json')['phase'],'ready')
        with patch.object(self.store,'stage',wraps=self.store.stage) as stage:
            result=managed_catalogs.prepare_store(self.store,self.repo.folder,owner_lease_held=True)
            self.assertEqual(stage.call_count,0)
        current=self.store.current()
        self.assertTrue(result['all_modes_prepared'])
        self.assertEqual(set(current['modes']),set(MODES))
        for mode,identity in before['modes'].items():self.assertEqual(current['modes'][mode],identity)
        self.assertNotEqual(current['modes']['taiko'],before['modes'].get('taiko'))
        self.assertEqual((self.root/'config/host.json').read_bytes(),host_bytes)
        self.assertEqual({p.name:p.read_bytes() for p in (self.root/'config/rooms').glob('*.json')},room_files)
        self.assertEqual(managed_catalogs.prepare_store(self.store,self.repo.folder,owner_lease_held=True)['reference'],current)
        self.assertEqual(self.store.current(),current)
        taiko_path,manifest=self.store.version('taiko',current['modes']['taiko'])
        self.assertEqual(manifest['mode'],'taiko')
        with sqlite3.connect((taiko_path/'catalog.db').as_uri()+'?mode=ro',uri=True) as db:
            self.assertEqual(db.execute('SELECT beatmap_id FROM taiko_maps').fetchone(),(7654321,))

        # Exact v7.3.2 reader code sees the additive TUF target set and still
        # verifies each catalog mode it knows; it never needs Taiko support.
        work=Path(__file__).resolve().parents[4]
        old_source=work/'validation/controller-resilience732/source'
        old_cache=self.base/'old-reader-cache';old_cache.mkdir(mode=0o700)
        code='''import sys\nfrom pathlib import Path\nsource,repo,cache=map(Path,sys.argv[1:])\nsys.path.insert(0,str(source))\nfrom poolrotater_host.catalog_trust import BootstrapTrust,verified_local_catalog\ntrust=BootstrapTrust((repo/"metadata/1.root.json").read_bytes(),fixture=True)\nfor mode in ("standard","mania4","mania7","catch","loved"):\n with verified_local_catalog(repo,cache/mode,trust,mode) as catalog: assert catalog.manifest["mode"]==mode\nprint("old-reader-compatible")\n'''
        check=subprocess.run([sys.executable,'-B','-c',code,str(old_source),str(self.repo.folder),
                              str(old_cache)],capture_output=True,text=True,timeout=60)
        self.assertEqual(check.returncode,0,check.stderr)
        self.assertIn('old-reader-compatible',check.stdout)


class TaikoSlotAndUpgradeBoundaryTests(unittest.TestCase):
    def test_deliberately_stopped_catalog_slot_does_not_create_a_worker(self):
        owner=SimpleNamespace(workers={},configs={'room1':{}},status_lock=threading.Lock())
        state=SimpleNamespace(room=lambda alias,private=False:{'revision':7,'desired':'stopped'})
        adapter=NativeAdapter(SimpleNamespace(owner=owner,state=state))
        value={'action':'catalog','alias':'room1','patch':{'mode':'taiko','content_id':'a'*64}}
        for _ in range(2):
            result=adapter.apply(value)
            self.assertEqual(result['room_id'],None)
            self.assertEqual(result['revision'],7)
        self.assertEqual(owner.workers,{})

    def test_booking_reservation_is_rechecked_under_catalog_admission(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'state/rolling').mkdir(parents=True,mode=0o700)
            owner=SimpleNamespace(runtime=folder,status_lock=threading.Lock(),rollout_alias=None,
                                  stopping=threading.Event(),targets={})
            adapter=NativeAdapter(SimpleNamespace(owner=owner,state=SimpleNamespace()))
            value={'action':'catalog','alias':'room1','patch':{'mode':'taiko','content_id':'a'*64}}
            with patch('poolrotater_host.bookings.admission.update_admission') as admission, \
                 patch('poolrotater_host.bookings.update_guard.holds_alias',return_value=True):
                from contextlib import contextmanager
                @contextmanager
                def guard(_root):yield
                admission.return_value=guard(folder)
                self.assertFalse(adapter.admit(value))
            self.assertIsNone(owner.rollout_alias)
            self.assertIsNone(adapter.admitted)

    def _frozen_manifest(self,archive,pin,scratch):
        from poolrotater_host.runtime_package import inspect
        from rolling.release import declared_contract
        spec=inspect(archive,pin,execution='pilot-live')
        expected={name.removeprefix('payload/'):row['sha256']
                  for name,row in spec['files'].items() if name.startswith('payload/')}
        root=Path(scratch);release_path=root/'rolling/release.py';release_path.parent.mkdir(parents=True)
        with zipfile.ZipFile(archive) as bundle:
            release_path.write_bytes(bundle.read('payload/rolling/release.py'))
        contract=declared_contract(root,expected)
        return {'version':spec['version'],'protocol':1,'storage_epoch':'poolrotater-6.7-v1',
                'requirements':expected['requirements.txt'],'python_hash':spec['interpreter_sha256'],
                'environment_hash':spec['environment_id'],'migrations':[],
                'shared':{name:expected[name] for name in contract['SHARED']},
                'storage':{name:expected[name] for name in contract['STORES']}}

    def test_exact_frozen_candidate_pairs_admit_and_unreviewed_pairs_refuse(self):
        from poolrotater_host.pilot_upgrade import require_repair_compatible
        freeze=json.loads((Path(__file__).resolve().parents[2]/'candidate/freeze.json').read_text())
        candidate_path=Path(freeze['build']['path'])
        candidate_pin=freeze['runtime_sha256']
        with tempfile.TemporaryDirectory() as td:
            candidate=self._frozen_manifest(candidate_path,candidate_pin,Path(td)/'candidate')
            for index,row in enumerate(freeze['baseline_versions']):
                with self.subTest(baseline=row['version']):
                    baseline=self._frozen_manifest(Path(row['archive']),row['sha256'],Path(td)/('baseline-'+str(index)))
                    self.assertEqual((baseline['version'],candidate['version']),
                                     (row['version'],'v7.3.3'))
                    require_repair_compatible(baseline,candidate)
                    for section in ('shared','storage'):
                        changed={**candidate,section:{**candidate[section]}}
                        name=next(iter(changed[section]))
                        changed[section][name]='0'*64
                        with self.subTest(baseline=row['version'],section=section), \
                             self.assertRaises(ConfigError):
                            require_repair_compatible(baseline,changed)
            baseline=self._frozen_manifest(Path(freeze['baseline_versions'][0]['archive']),
                freeze['baseline_versions'][0]['sha256'],Path(td)/'unreviewed-baseline')
            wrong_version={**candidate,'version':'v7.3.4'}
            with self.assertRaisesRegex(ConfigError,'reviewed runtime family'):
                require_repair_compatible(baseline,wrong_version)

    def test_release_classification_rejects_unreviewed_store_changes(self):
        from poolrotater_host.updates.publisher import classify
        from rolling.release import STORES
        before={'execution':'pilot-live','version':'v7.3.2','target':{'python':'3.12'},
                'interpreter_sha256':'a'*64,'wheels':[],'environment_id':'b'*64,
                'files':{'payload/'+name:'c'*64 for name in STORES}}
        after={**before,'version':'v7.3.3','files':{**before['files'],
                'payload/poolrotater_host/ratings/agent.py':'d'*64}}
        self.assertEqual(classify(before,after),'controller')
        unsafe={**after,'files':{**after['files'],'payload/'+STORES[0]:'e'*64}}
        with self.assertRaisesRegex(Exception,'update_storage_requires_manual_migration'):
            classify(before,unsafe)
