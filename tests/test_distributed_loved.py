from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
import copy,hashlib,json,shutil,sqlite3,tempfile,unittest
from unittest.mock import patch
from poolrotater_host.config import ConfigError,defaults
from poolrotater_host.layout import setup,load,atomic_json
from poolrotater_host.catalog_export import inspect_bundle,pool_from_bundle,read_manifest,canonical,create_table,sha256
from poolrotater_host.catalog_loved import export,COLUMNS,check
from poolrotater_host.catalog_publication import initialize,Publisher,_legacy_feed,_scoped_feed
from poolrotater_host.catalog_trust import BootstrapTrust,verified_local_catalog,verified_target
from poolrotater_host.catalog_store import CatalogStore,mode_for,settings_for
from poolrotater_host.catalogs import make_pool,inspect_catalog
from poolrotater_host.runtime_config import render,default_room_title,RuntimeConfigBridge
from poolrotater_host.owner_settings import RECORD,validate_settings
from poolrotater_host.network.projection import project
from poolrotater_host.network.display import room_v2
from rolling.worker import make_worker_pool

BASE=Path(__file__).resolve().parents[1]
FULL_SOURCE=BASE/'inputs/loved-source.db'
FULL_BUNDLE=BASE/'prepared/loved'

class LovedExportTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.bundle=inspect_bundle(FULL_BUNDLE)
 def test_full_native_catalog_reader_and_expected_coverage(self):
  m=self.bundle
  with sqlite3.connect(FULL_SOURCE.as_uri()+'?mode=ro',uri=True)as db:
   expected=db.execute("SELECT COUNT(*) FROM beatmaps WHERE status='loved' AND mode='osu' AND convert=0 AND sr>0 AND sr<=100 AND beatmap_id!=1549496").fetchone()[0]
  self.assertGreater(expected,1000);self.assertEqual(m['files'][0]['tables']['beatmaps'],expected)
  pool=pool_from_bundle(FULL_BUNDLE,sr_min=4,sr_max=6,limit=200000)
  self.assertTrue(pool.loved_mode);self.assertGreater(len(pool.maps),100)
  self.assertTrue(all(x.is_loved and not x.is_dt and not x.is_farm for x in pool.maps))
  self.assertTrue(pool.next_map().is_loved)
 def test_projection_is_deterministic_and_does_not_copy_private_tables(self):
  with tempfile.TemporaryDirectory()as d:
   p=Path(d);src=p/'source.db';shutil.copyfile(FULL_SOURCE,src)
   with sqlite3.connect(src)as db:
    db.execute('CREATE TABLE private_players(player_id INTEGER, note TEXT)')
    db.execute("INSERT INTO private_players VALUES(123,'invented-private-marker')")
   a=export(src,p/'a');b=export(src,p/'b')
   self.assertEqual(a,b);self.assertNotIn(b'invented-private-marker',(p/'a/catalog.db').read_bytes())
   self.assertEqual(a['files'][0]['sha256'],self.bundle['files'][0]['sha256'])
   self.assertEqual(inspect_catalog(p/'a/catalog.db','standard-loved')['tables'],['beatmaps'])
 def test_schema_status_ruleset_counts_and_unknown_capabilities_refuse(self):
  for field,value in [('status','ranked'),('mode','mania'),('convert',1),('download_disabled',2)]:
   row={n:('x' if k=='TEXT' else 1)for n,k in COLUMNS};row.update(status='loved',mode='osu',convert=0,download_disabled=0)
   row[field]=value
   with self.subTest(field=field),self.assertRaises(ConfigError):check(tuple(row[n]for n,_ in COLUMNS))
  for changed in ({'ruleset':3},{'reader':'unknown'},{'capabilities':{'styles':['farm']}}, {'content_id':'0'*64}):
   from poolrotater_host.catalog_loved import validate_manifest
   with self.subTest(changed=changed),self.assertRaises(ConfigError):validate_manifest(self.bundle|changed)
 def test_existing_source_and_destination_are_not_modified(self):
  before=sha256(FULL_SOURCE)
  with self.assertRaises(ConfigError):export(FULL_SOURCE,FULL_BUNDLE)
  self.assertEqual(before,sha256(FULL_SOURCE))

class LovedConfigurationTests(unittest.TestCase):
 def host(self,presets):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
  root=Path(tmp.name)/'host';setup(root,presets);return root
 def test_explicit_preset_and_native_worker_settings(self):
  root=self.host(['standard-loved']);marker,host=load(root)
  self.assertEqual(mode_for(host['rooms'][0]),'loved');self.assertEqual(host['rooms'][0]['catalog'],'catalogs/loved.db')
  shutil.copyfile(FULL_BUNDLE/'catalog.db',root/'catalogs/loved.db')
  preview=render(root)['rooms'][0]['settings']
  self.assertTrue(preview['loved_mode']);self.assertEqual(preview['min_passcount'],0)
  self.assertFalse(preview['auto_difficulty']);self.assertEqual(preview['loved_db'],str(root/'catalogs/loved.db'))
  pool=make_worker_pool(preview);self.assertTrue(pool.maps);self.assertTrue(all(x.is_loved for x in pool.maps))
  pool2=make_pool(root,host['rooms'][0]);self.assertTrue(pool2.loved_mode)
 def test_default_titles_match_owner_and_explicit_custom_titles_win(self):
  expected={'standard-auto':'AutoHost|Type .help|','standard-fixed':'AutoHost|Type .help|',
   'mania-4k':'AutoHost 4K|Type .help|','mania-7k':'AutoHost 7K|Type .help|',
   'catch-fixed':'PoolRotater | Catch','standard-loved':'AutoHost Loved|Type .help|'}
  for preset,title in expected.items():
   with self.subTest(preset=preset):self.assertEqual(default_room_title(preset),title)
  root=self.host(['standard-loved']);marker,host=load(root);host['execution']='pilot-live';atomic_json(root/'config/host.json',host)
  self.assertEqual(render(root)['rooms'][0]['settings']['room_name'],expected['standard-loved'])
  atomic_json(root/RECORD,{'format':1,**marker,'rooms':{'room1':{'preset':'standard-loved','settings':{'room_name':'My chosen Loved room'}}}})
  self.assertEqual(render(root)['rooms'][0]['settings']['room_name'],'My chosen Loved room')
 def test_existing_generated_and_native_custom_titles_survive_new_defaults(self):
  for title in ('PoolRotater pilot | room1','A locally customized title'):
   with self.subTest(title=title):
    root=self.host(['standard-loved']);_,host=load(root)
    host['execution']='pilot-live';atomic_json(root/'config/host.json',host)
    shutil.copyfile(FULL_BUNDLE/'catalog.db',root/'catalogs/loved.db')
    with patch('poolrotater_host.runtime_config.default_room_title',return_value='PoolRotater pilot | room1'):
     RuntimeConfigBridge(root).materialize()
    path=root/'config/rooms/room1.json';data=json.loads(path.read_text());data['room_name']=title;atomic_json(path,data)
    RuntimeConfigBridge(root).materialize()
    self.assertEqual(json.loads(path.read_text())['room_name'],title)

 def test_loved_cannot_be_enabled_on_ranked_or_disabled_on_loved(self):
  for preset,value in [('standard-fixed',True),('standard-loved',False)]:
   with self.subTest(preset=preset),self.assertRaises(ConfigError):validate_settings({'loved_mode':value},preset=preset)
  self.assertTrue(validate_settings({'loved_mode':True},preset='standard-loved')['loved_mode'])
 def test_network_projection_keeps_legacy_standard_contract(self):
  root=self.host(['standard-loved']);_,host=load(root)
  status={'ready':True,'running':True,'room_id':99,'state':'IDLE','players':1,'version':'v7.2.6','public':{'current_map':None},
    'network':{'title':'AutoHost Loved','min_stars':4.,'max_stars':6.,'catalog_version':'0'*64,'display':None}}
  row={'room_id':99,'network_visibility':'public','heartbeat_age':1.,'status':status}
  projection=project({'generated_at':1000.,'rooms':{'room1':row}},host,now=1001.,code_version='v7.2.6')
  self.assertEqual(len(projection['rooms']),1);self.assertEqual(projection['rooms'][0]['preset'],'standard-fixed')
  room_v2(projection['rooms'][0])

class LovedSignedStoreTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  parent=Path.cwd()/'scratch/loved726';parent.mkdir(parents=True,exist_ok=True)
  cls.tmp=tempfile.TemporaryDirectory(dir=parent);cls.addClassCleanup(cls.tmp.cleanup);cls.base=Path(cls.tmp.name)
  cls.initial=initialize(cls.base/'signers',cls.base/'public')
  cls.bundles={m:Path.cwd()/'validation/owner-pilot/conversion-catalog-qualification/prepared'/m for m in ('standard','mania4','mania7')}
  # Existing prepared fixtures lack their archived manifests; use verified public
  # exports copied once into isolated inputs by the preparation step instead.
  cls.bundles={m:BASE/'inputs'/m for m in ('standard','mania4','mania7')}
  cls.bundles['catch']=BASE/'inputs/catch'
  cls.bundles['loved']=FULL_BUNDLE
  cls.feed={'format':1,'revision':2,'events':[{'kind':'beatmap','id':1549496,'modes':['standard','mania4','mania7','loved'],'action':'exclude','reason':'missing_audio','memberships':{}}]}
  cls.result=Publisher(cls.base/'signers').publish(cls.bundles,exclusions=cls.feed)
  cls.trust=BootstrapTrust((cls.base/'signers/bootstrap.json').read_bytes(),fixture=True)
 def test_signed_full_download_import_and_native_managed_pool(self):
  root=self.base/'host';setup(root,['standard-loved'])
  store=CatalogStore.initialize_fixture(root,self.trust)
  reference=store.import_local(self.base/'public',['loved'])
  self.assertEqual(set(reference['modes']),{'loved'})
  ref=store.inspect();self.assertEqual(ref['manifests']['loved']['mode'],'loved')
  pool=make_pool(root,load(root)[1]['rooms'][0]);self.assertTrue(pool.loved_mode);self.assertTrue(pool.maps)
  # Exercise the worker's signed-policy path, not only the native pool factory.
  # A Loved-only host has no Ranked Standard entry to accidentally fall back to.
  cfg=RuntimeConfigBridge(root).load_rooms()[0]
  worker_pool=make_worker_pool(cfg)
  self.assertTrue(worker_pool.loved_mode);self.assertTrue(worker_pool.maps)
  self.assertTrue(all(item.is_loved for item in worker_pool.maps))
  self.assertEqual(store.import_local(self.base/'public',['loved']),reference)
  self.assertTrue((store.path/'exclusions-loved.json').exists())
  self.assertEqual({(m,e['id'],e['action'])for e in store.exclusions()['events']for m in e['modes']},{(m,e['id'],e['action'])for e in _legacy_feed(self.feed)['events']for m in e['modes']})
 def test_legacy_signed_targets_keep_unknown_loved_out_of_exclusions(self):
  from poolrotater_host.layout import unique_fields
  for name,allowed in [('exclusions.json',{'standard','mania4','mania7'})]:
   raw=verified_target(self.base/'public',self.base/'legacy-cache',self.trust,name)
   feed=json.loads(raw)
   self.assertTrue(all(set(e['modes'])<=allowed for e in feed['events']))
  with verified_local_catalog(self.base/'public',self.base/'standard-cache',self.trust,'standard')as result:
   self.assertEqual(result.manifest['content_id'],inspect_bundle(self.bundles['standard'])['content_id'])
 def test_loved_exclusions_cannot_contaminate_legacy_or_catch_projection(self):
  self.assertTrue(all('loved' not in e['modes']for e in _legacy_feed(self.feed)['events']))
  self.assertTrue(all('loved' not in e['modes']for e in _scoped_feed(self.feed,{'standard','mania4','mania7','catch'})['events']))
  self.assertEqual(_scoped_feed(self.feed,{'loved'})['events'][0]['modes'],['loved'])

class LovedExclusionAndCliTests(LovedSignedStoreTests):
 def test_full_loved_reader_honors_signed_exclusion_and_source_set_binding(self):
  from poolrotater_host.catalog_exclusions import membership_event
  root=self.base/'excluded-host';setup(root,['standard-loved'])
  store=CatalogStore.initialize_fixture(root,self.trust);store.import_local(self.base/'public',['loved'])
  pool=make_pool(root,load(root)[1]['rooms'][0]);picked=pool.maps[0].beatmap_id
  with sqlite3.connect(FULL_SOURCE.as_uri()+'?mode=ro',uri=True)as db:
   set_id=db.execute('SELECT beatmapset_id FROM beatmaps WHERE beatmap_id=?',(picked,)).fetchone()[0]
  event=membership_event(FULL_SOURCE,FULL_BUNDLE,set_id,reason='missing_audio')
  self.assertIn(picked,event['memberships']['loved'][0]['beatmap_ids'])
  Publisher(self.base/'signers').publish(self.bundles,exclusions={'format':1,'revision':3,'events':[event]})
  store.accept_exclusions(self.base/'public')
  self.assertIsNone(pool.get_map(picked))
  self.assertTrue(all(pool.next_map().beatmap_id!=picked for _ in range(30)))
  self.assertTrue(all('loved'not in e['modes']for e in store.exclusions()['events']))
 def test_protected_cli_accepts_explicit_loved_import_without_dispatch(self):
  import io
  from contextlib import redirect_stdout
  from poolrotater_host.cli import main
  with patch('poolrotater_host.pilot_operations.https_repository',return_value='invented-repository'),patch('poolrotater_host.pilot_operations.import_catalogs',return_value={'ok':True})as call,redirect_stdout(io.StringIO()):
   result=main(['pilot-catalog-import','--instance','first','--origin','https://example.invalid/',
    '--ca-file','ca.crt','--ca-sha256','a'*64,'--trust-root','bootstrap.json','--trust-sha256','b'*64,'--mode','loved'])
  self.assertEqual(result,0);self.assertEqual(call.call_args.kwargs['modes'],['loved'])

class LovedDeliveryAndCompatibilityTests(LovedSignedStoreTests):
 def test_https_full_loved_download_and_cached_rerun(self):
  from types import SimpleNamespace
  from poolrotater_host.testing.provider_https import certificates
  from poolrotater_host.testing.catalog_http import CatalogHTTPFixture
  from poolrotater_host.catalog_download import download_catalog
  folder=self.base/'https';folder.mkdir(mode=0o700);certificates(folder)
  server=CatalogHTTPFixture(SimpleNamespace(folder=self.base/'public'))
  with server.serve(tls=(folder/'server.crt',folder/'server.key'))as origin:
   args=dict(url=origin,cache=folder/'cache',trust_root=self.base/'signers/bootstrap.json',
     trust_sha256=sha256(self.base/'signers/bootstrap.json'),ca_file=folder/'ca.crt',ca_sha256=sha256(folder/'ca.crt'),mode='loved')
   first=download_catalog(**args,destination=folder/'first')
   before=server.bytes_sent
   second=download_catalog(**args,destination=folder/'second')
   self.assertEqual(sha256(folder/'first/catalog/catalog.db'),sha256(folder/'second/catalog/catalog.db'))
   self.assertEqual(inspect_bundle(folder/'first/catalog')['content_id'],inspect_bundle(FULL_BUNDLE)['content_id'])
   self.assertLess(server.bytes_sent-before,32768)
 def test_previous_release_reads_standard_and_catch_with_loved_published(self):
  import subprocess,sys
  old=BASE.parent/'roster-recovery725/source'
  code="""import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from poolrotater_host.catalog_trust import BootstrapTrust,verified_local_catalog,verified_target
repo,cache,trustroot=map(Path,sys.argv[2:])
cache.mkdir(mode=0o700)
trust=BootstrapTrust(trustroot.read_bytes(),fixture=True)
for mode in ('standard','catch'):
 with verified_local_catalog(repo,cache/mode,trust,mode)as result:assert result.manifest['mode']==mode
for name in ('exclusions.json','exclusions-v2.json'):
 value=json.loads(verified_target(repo,cache/name,trust,name))
 assert all('loved'not in row['modes']for row in value['events'])
print('old_reader_compatible')
"""
  cp=subprocess.run([sys.executable,'-B','-c',code,str(old),str(self.base/'public'),str(self.base/'old-cache'),str(self.base/'signers/bootstrap.json')],capture_output=True,text=True,timeout=30)
  self.assertEqual(cp.returncode,0,cp.stderr);self.assertIn('old_reader_compatible',cp.stdout)

class LovedPublicationPermissionsTests(unittest.TestCase):
 def test_private_administrator_umask_preserves_signers_and_public_traversal(self):
  import os,stat
  parent=Path.cwd()/'scratch/loved-public-permissions';parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=parent)as folder:
   root=Path(folder);previous=os.umask(0o077)
   try:
    initialize(root/'signers',root/'public')
    bundles={m:BASE/'inputs'/m for m in ('standard','mania4','mania7','catch')};bundles['loved']=FULL_BUNDLE
    result=Publisher(root/'signers').publish(bundles)
   finally:os.umask(previous)
   self.assertEqual(stat.S_IMODE((root/'signers').stat().st_mode),0o700)
   self.assertEqual(stat.S_IMODE((root/'signers/keys').stat().st_mode),0o700)
   for name in ('','metadata','targets','targets/loved','targets/standard','targets/catch'):
    self.assertEqual(stat.S_IMODE((root/'public'/name).stat().st_mode),0o755,name)
   self.assertEqual(result['content_ids']['loved'],inspect_bundle(FULL_BUNDLE)['content_id'])
