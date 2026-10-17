"""Root recovery state-machine decisions; installed guards are donor-qualified.

Native registration/systemd/provider actions are replaced here, with real durable
transaction files and no account credentials. Live/native transport tests remain
separate and are required before release acceptance.
"""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from contextlib import nullcontext
import copy,hashlib,json,os,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import Mock,patch
from poolrotater_host.config import ConfigError
from poolrotater_host.layout import atomic_json,read_json
from poolrotater_host.updates import recovery

class ControllerRecoveryTests(unittest.TestCase):
 def setUp(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.folder=Path(tmp.name)
  prefix=self.folder/'code';prefix.mkdir();root=self.folder/'host';root.mkdir();(root/'config').mkdir()
  (root/'config/retained.json').write_text('{"title":"operator chosen","offset":0.7}\n')
  self.target={'prefix':prefix,'root':root,'unit':'invented.service'}
  common={'installation_id':str(uuid.uuid4()),'generation':str(uuid.uuid4()),'prefix':str(prefix),
   'host_root':str(root),'service_uid':os.geteuid(),'execution':'pilot-live'}
  archive=b'invented next runtime';sha=hashlib.sha256(archive).hexdigest()
  self.before={**common,'bundle_sha256':'a'*64,'release':str(prefix/'old')}
  self.after={**common,'bundle_sha256':sha,'release':str(prefix/'new')};self.current=self.before
  path=prefix/('controller-'+sha+'.zip');path.write_bytes(archive)
  from poolrotater_host import controller_update as native
  self.native=native;self.id=str(uuid.uuid4())
  self.job={'id':self.id,'phase':'held','before':self.before,'after':self.after,'archive':str(path),
   'configuration':native._config_hashes(root),'service_enabled':True,'network_enabled':False,'network_active':False,
   'nonce':'a'*32,'commit_started_at':time.time(),'empty_since':1,'room_ids':{'room1':77},'worker_identity':{'old':'identity'}}
  atomic_json(prefix/native.NAME,self.job)
  self.service={'MainPID':0,'ActiveState':'inactive','UnitFileState':'disabled'}
  self.network={'MainPID':0,'ActiveState':'inactive','UnitFileState':'disabled'}
  self.upgrade=None
  self.enterContext(patch('poolrotater_host.pilot_installation._admin'))
  self.enterContext(patch('poolrotater_host.pilot_upgrade._binding',side_effect=lambda instance:(self.target,{},None,self.current)))
  self.enterContext(patch.object(recovery,'validate_runtime',side_effect=lambda record,prefix:{'manifest':{},'release':Path(record['release'])}))
  self.enterContext(patch.object(recovery,'protected',side_effect=lambda p:p))
  from poolrotater_host.provisioning import _control
  self.enterContext(patch.object(recovery,'_control',side_effect=lambda path,*args,**kw:_control(path,os.geteuid(),**kw)))
  self.enterContext(patch.object(recovery,'_write',side_effect=atomic_json))
  self.enterContext(patch.object(native,'_write',side_effect=atomic_json))
  self.enterContext(patch.object(recovery,'_upgrade',side_effect=lambda *args:self.upgrade))
  self.approval=self.enterContext(patch.object(recovery,'_approval'))
  self.fresh=self.enterContext(patch.object(recovery,'_fresh_running',return_value={'room1':{'players':2}}))
  self.gate=self.enterContext(patch.object(native,'_gate'))
  self.enterContext(patch.object(native,'disarm'))
  self.arm=self.enterContext(patch.object(native,'_arm'))
  self.enterContext(patch.object(native,'_process',return_value={'pid':12,'boot':'new boot','start':'new start'}))
  self.enterContext(patch('poolrotater_host.pilot_operations._service_state',side_effect=lambda instance:dict(self.service)))
  self.enterContext(patch('poolrotater_host.network.operations._state',side_effect=lambda instance:dict(self.network)))
  self.network_action=self.enterContext(patch('poolrotater_host.network.operations.unit',return_value={'ok':True}))
  self.enterContext(patch('poolrotater_host.network.operations._checked_unit',return_value=None))
  self.systemctl=self.enterContext(patch('poolrotater_host.pilot_operations._systemctl'))
  self.ready=self.enterContext(patch('poolrotater_host.pilot_operations._wait_ready'))
  self.stopped=self.enterContext(patch('poolrotater_host.pilot_installation._stopped',side_effect=lambda *a:nullcontext()))
  self.enterContext(patch('poolrotater_host.pilot_installation.require_no_service_processes'))
  def start(instance,action):
   self.assertEqual(action,'start');self.service.update(MainPID=222,ActiveState='active');return {'ok':True}
  self.lifecycle=self.enterContext(patch('poolrotater_host.pilot_operations.lifecycle',side_effect=start))
  def finish(*a,**kw):
   self.current=self.before if kw.get('recover')=='abort'else self.after
   self.upgrade={'phase':'aborted'if kw.get('recover')=='abort'else'committed','transition_id':'retained'}
   return {'transition_id':'retained'}
  self.finish=self.enterContext(patch('poolrotater_host.pilot_upgrade.upgrade',side_effect=finish))
  self.settings=(root/'config/retained.json').read_bytes()
 def run_recovery(self,decision):return recovery.controller('fixture',self.id,decision)
 def assert_no_lifecycle(self):
  self.lifecycle.assert_not_called();self.network_action.assert_not_called();self.systemctl.assert_not_called()
 def test_readonly_review_has_no_file_or_service_mutation(self):
  before=(self.target['prefix']/self.native.NAME).read_bytes()
  result=self.run_recovery('review');self.assertEqual(result['upgrade_phase'],'absent')
  self.assertEqual((self.target['prefix']/self.native.NAME).read_bytes(),before)
  self.assertFalse((self.target['prefix']/'controller-recovery-history').exists());self.assert_no_lifecycle()
 def test_wrong_operation_refused_before_action(self):
  with self.assertRaises(ConfigError):recovery.controller('fixture',str(uuid.uuid4()),'complete')
  self.assert_no_lifecycle()
 def test_old_occupied_controller_is_not_stopped_and_requires_new_admission(self):
  self.service.update(MainPID=12,ActiveState='active')
  result=self.run_recovery('complete');self.assertTrue(result['fresh_empty_window_required'])
  job=read_json(self.target['prefix']/self.native.NAME)
  self.assertEqual(job['phase'],'waiting_empty');self.assertIsNone(job['empty_since'])
  self.assertNotIn('worker_identity',job);self.assertNotIn('commit_started_at',job)
  self.assertEqual(job['process']['boot'],'new boot');self.lifecycle.assert_not_called();self.network_action.assert_not_called();self.finish.assert_not_called()
 def test_running_predecessor_restores_reporting_and_owned_timer_without_bot_restart(self):
  self.service.update(MainPID=12,ActiveState='active')
  self.job.update(network_active=True,network_enabled=True,timer_sha256='b'*64)
  atomic_json(self.target['prefix']/self.native.NAME,self.job)
  self.enterContext(patch('poolrotater_host.network.operations._checked_unit',return_value=Path('invented.service')))
  result=self.run_recovery('complete')
  self.assertTrue(result['fresh_empty_window_required'])
  self.network_action.assert_called_once_with('fixture','start');self.arm.assert_called_once_with('fixture')
  self.lifecycle.assert_not_called();self.finish.assert_not_called()
  self.assertIn(('enable',self.target['unit']),[call.args for call in self.systemctl.call_args_list])
 def test_running_predecessor_keeps_opted_out_network_stopped_and_no_unowned_timer(self):
  self.service.update(MainPID=12,ActiveState='active')
  self.assertEqual(self.run_recovery('complete')['phase'],'waiting_empty')
  self.network_action.assert_not_called();self.arm.assert_not_called();self.lifecycle.assert_not_called()
 def test_prepared_stopped_upgrade_finishes_once_and_repeated_completion_does_not_start_again(self):
  self.upgrade={'phase':'prepared','transition_id':'retained'}
  self.assertEqual(self.run_recovery('complete')['phase'],'complete')
  self.assertEqual(self.finish.call_args.kwargs['recover'],'complete')
  self.assertEqual(self.finish.call_args.kwargs['transaction_id'],'retained')
  self.assertEqual(self.lifecycle.call_count,1)
  self.assertTrue(self.run_recovery('complete')['replayed']);self.assertEqual(self.lifecycle.call_count,1)
  self.assertEqual((self.target['root']/'config/retained.json').read_bytes(),self.settings)
 def test_already_started_successor_is_observed_without_duplicate_room_start(self):
  self.current=self.after;self.upgrade={'phase':'committed','transition_id':'retained'}
  self.service.update(MainPID=222,ActiveState='active')
  self.assertEqual(self.run_recovery('complete')['phase'],'complete')
  self.lifecycle.assert_not_called();self.ready.assert_called_once();self.finish.assert_not_called()
 def test_unknown_stopped_ownership_refuses_start_and_keeps_recovery(self):
  self.current=self.after;self.upgrade={'phase':'committed','transition_id':'retained'}
  self.stopped.side_effect=ConfigError('unknown ownership')
  with self.assertRaisesRegex(ConfigError,'unknown ownership'):self.run_recovery('complete')
  self.lifecycle.assert_not_called();self.assertEqual(read_json(self.target['prefix']/self.native.NAME)['phase'],'held')
 def test_committed_rollback_and_active_prepared_abort_are_refused(self):
  for phase in('prepared','committed'):
   self.current=self.after;self.upgrade={'phase':phase,'transition_id':'retained'}
   self.service.update(MainPID=222,ActiveState='active')
   with self.subTest(phase=phase),self.assertRaises(ConfigError):self.run_recovery('rollback')
  self.assert_no_lifecycle();self.finish.assert_not_called()
 def test_only_prepared_stopped_registration_can_abort_without_starting_rooms(self):
  self.upgrade={'phase':'prepared','transition_id':'retained'}
  self.assertEqual(self.run_recovery('rollback')['phase'],'cancelled')
  self.assertEqual(self.finish.call_args.kwargs['recover'],'abort');self.assert_no_lifecycle()
  self.assertTrue(self.run_recovery('rollback')['replayed'])
 def test_config_change_and_archive_change_are_retained_before_services_change(self):
  (self.target['root']/'config/retained.json').write_text('operator changed this')
  with self.assertRaisesRegex(ConfigError,'configuration_changed'):self.run_recovery('complete')
  Path(self.job['archive']).write_bytes(b'changed')
  with self.assertRaisesRegex(ConfigError,'archive_changed'):self.run_recovery('complete')
  self.assert_no_lifecycle()
 def test_cancel_before_registration_change_never_starts_or_stops_services(self):
  self.service.update(MainPID=12,ActiveState='active')
  self.assertEqual(self.run_recovery('cancel')['phase'],'cancelled');self.assert_no_lifecycle()
 def test_failed_fresh_successor_observation_does_not_label_update_complete(self):
  self.current=self.after;self.upgrade={'phase':'committed','transition_id':'retained'}
  self.service.update(MainPID=222,ActiveState='active');self.fresh.side_effect=ConfigError('unhealthy sibling')
  with self.assertRaises(ConfigError):self.run_recovery('complete')
  self.lifecycle.assert_not_called();self.assertEqual(read_json(self.target['prefix']/self.native.NAME)['phase'],'held')

 def versioned_room(self,version='7.0.8'):
  folder=self.target['root']/'config/rooms';folder.mkdir()
  path=folder/'room1.json';atomic_json(path,{'version':'7.0.6','countdown_seconds':23,'admins':[888]})
  self.job.update(rooms=['room1'],configuration=self.native._config_hashes(self.target['root']))
  atomic_json(self.target['prefix']/self.native.NAME,self.job)
  self.enterContext(patch.object(recovery,'validate_runtime',side_effect=lambda record,prefix:{
   'manifest':{'version':'v7.0.6'if record==self.before else'v7.0.8'},'release':Path(record['release'])}))
  self.current=self.after;self.upgrade={'phase':'committed','transition_id':'retained'}
  self.service.update(MainPID=222,ActiveState='active')
  atomic_json(path,{'version':version,'countdown_seconds':23,'admins':[888]})
  return path
 def test_started_successor_native_version_stamp_does_not_prevent_completion(self):
  self.versioned_room()
  self.assertEqual(self.run_recovery('complete')['phase'],'complete')
  self.lifecycle.assert_not_called();self.ready.assert_called_once()
 def test_other_settings_or_wrong_successor_stamp_cannot_use_version_exception(self):
  path=self.versioned_room()
  for value in({'version':'7.0.8','countdown_seconds':99,'admins':[888]},
               {'version':'7.0.9','countdown_seconds':23,'admins':[888]}):
   atomic_json(path,value)
   with self.subTest(value=value),self.assertRaisesRegex(ConfigError,'configuration_changed'):
    self.run_recovery('complete')
  self.assert_no_lifecycle()
 def test_still_running_predecessor_cannot_adopt_successor_stamp(self):
  self.versioned_room();self.current=self.before;self.upgrade=None
  with self.assertRaisesRegex(ConfigError,'configuration_changed'):self.run_recovery('complete')
  self.assert_no_lifecycle()
 def test_recovered_start_may_materialize_only_expected_new_version(self):
  path=self.versioned_room('7.0.6');self.service.update(MainPID=0,ActiveState='inactive')
  def start(*args):
   atomic_json(path,{'version':'7.0.8','countdown_seconds':23,'admins':[888]})
   self.service.update(MainPID=222,ActiveState='active');return{'ok':True}
  self.lifecycle.side_effect=start
  self.assertEqual(self.run_recovery('complete')['phase'],'complete')
  self.lifecycle.assert_called_once();self.assertEqual(read_json(path)['countdown_seconds'],23)
