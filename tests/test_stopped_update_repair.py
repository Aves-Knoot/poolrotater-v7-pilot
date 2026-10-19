"""Committed-failed-start replacement requires positive retained authority."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,json,os,tempfile,unittest
from pathlib import Path
from poolrotater_host.stopped_update_repair import qualify,PREDECESSOR
from poolrotater_host.updates.contracts import UpdateError
from poolrotater_host.updates.recovery import _configuration_matches
from poolrotater_host.controller_update import _config_hashes
from poolrotater_host.layout import atomic_json

class Admission(unittest.TestCase):
 def setUp(self):
  self.current=dict(bundle_sha256=PREDECESSOR,installation_id='same',generation='same',prefix='same',host_root='same',service_uid=1000,execution='pilot-live',environment='same',environment_id='same')
  self.candidate={**self.current,'bundle_sha256':'b'*64}
  self.config={'config/host.json':'c'*64}
  self.job=dict(after=self.current,before={'old':True},phase='held',failure_stage='controller_start',startup_attempted_at=1,release_approval_required=True,configuration=self.config,rooms=['room1','room2'])
  self.upgrade=dict(phase='committed',before=self.job['before'],after=self.current)
  self.journal={'rooms':{a:dict(phase='stopped',operation='closed',ownership_unknown=False,room_id=None,channel_id=None,pid=None)for a in self.job['rooms']}}
 def check(self):return qualify(self.current,self.job,self.upgrade,self.candidate,self.config,self.journal,self.job['rooms'])
 def test_exact_committed_clean_failed_start_admitted(self):self.check()
 def test_unknown_live_or_inflight_ownership_refused(self):
  for key,val in [('room_id',123),('channel_id',12),('pid',11),('ownership_unknown',True),('phase','running'),('operation','make_pending'),('target','foreign'),('creation_candidate',{'room_id':10}),('error','uncertain')]:
   with self.subTest(key=key):
    row=self.journal['rooms']['room1'];before=copy.deepcopy(row);row[key]=val
    with self.assertRaises(UpdateError):self.check()
    self.journal['rooms']['room1']=before
 def test_changed_configuration_not_adopted(self):
  self.config={'config/host.json':'d'*64}
  with self.assertRaises(UpdateError):self.check()
 def test_wrong_runtime_or_uncommitted_upgrade_refused(self):
  for phase in ['prepared','aborted']:
   self.upgrade['phase']=phase
   with self.assertRaises(UpdateError):self.check()
 def test_unrelated_failure_or_unapproved_intent_refused(self):
  for key,value in [('phase','complete'),('failure_stage','upgrade'),('startup_attempted_at',None),('release_approval_required',False)]:
   original=self.job[key];self.job[key]=value
   with self.assertRaises(UpdateError):self.check()
   self.job[key]=original
 def test_foreign_installation_or_environment_refused(self):
  for key in ['installation_id','generation','prefix','host_root','service_uid','environment','environment_id','execution']:
   old=self.candidate[key];self.candidate[key]='foreign'
   with self.assertRaises(UpdateError):self.check()
   self.candidate[key]=old

class ConfigurationMigration(unittest.TestCase):
 def setUp(self):
  temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
  (self.root/'config/rooms').mkdir(parents=True);self.path=self.root/'config/rooms/room1.json'
  self.original={'version':'7.1.12','countdown_seconds':23,'admins':[888]}
  atomic_json(self.path,self.original)
  self.job={'rooms':['room1'],'configuration':_config_hashes(self.root),'before':{'service_uid':os.geteuid()}}
 def matches(self):return _configuration_matches({'root':self.root},self.job,before_version='v7.1.12',after_version='v7.1.16')
 def test_exact_profile_addition_and_version_stamp_are_proved(self):
  atomic_json(self.path,{**self.original,'version':'7.1.16','background_player_profiles':True})
  self.assertTrue(self.matches())
 def test_conflicting_or_nonboolean_profile_not_adopted(self):
  for value in [False,1,'true',None]:
   atomic_json(self.path,{**self.original,'version':'7.1.16','background_player_profiles':value})
   self.assertFalse(self.matches())
 def test_operator_setting_or_version_changes_refused(self):
  for extra in [{'countdown_seconds':99},{'admins':[]},{'version':'7.1.99'},{'unrelated_field':True}]:
   atomic_json(self.path,{**self.original,'version':'7.1.16','background_player_profiles':True,**extra})
   self.assertFalse(self.matches())
 def test_preexisting_false_cannot_be_overwritten(self):
  atomic_json(self.path,{**self.original,'background_player_profiles':False});self.job['configuration']=_config_hashes(self.root)
  atomic_json(self.path,{**self.original,'version':'7.1.16','background_player_profiles':True})
  self.assertFalse(self.matches())

class WorkflowBoundaries(unittest.TestCase):
 def setUp(self):
  from unittest.mock import patch
  import contextlib
  from poolrotater_host import stopped_update_repair as repair
  self.repair=repair
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);folder=Path(t.name)
  self.target={'prefix':folder,'root':folder,'unit':'invented.service'}
  self.current={'bundle_sha256':'a'*64}
  self.candidate={'bundle_sha256':'b'*64}
  self.enterContext(patch('poolrotater_host.pilot_installation.administrative_binding',return_value=(self.target,{},self.current)))
  self.service=self.enterContext(patch('poolrotater_host.pilot_operations._service_state',return_value={'MainPID':0,'ActiveState':'failed'}))
  self.writer=self.enterContext(patch.object(repair,'_write',autospec=True))
  self.control=self.enterContext(patch.object(repair,'_control',return_value=None))
  self.enterContext(patch('poolrotater_host.network.operations._state',return_value={'MainPID':0}))
  from types import SimpleNamespace
  self.process=self.enterContext(patch('subprocess.run',return_value=SimpleNamespace(returncode=0,
    stdout='LoadState=not-found\nActiveState=inactive\nMainPID=0\nUnitFileState=\n')))
  self.dispatch=self.enterContext(patch('poolrotater_host.updates.executor.controller'))
  self.prepare=self.enterContext(patch.object(repair,'prepare',return_value={'id':'invented-job'}))
 def run_command(self):return self.repair.run('two',self.candidate,Path('/invented.zip'),start=True,resume_agents=True,report=self.target['root'])
 def test_native_failure_is_dispatched_once_and_private_report_writer_contract_is_real(self):
  self.dispatch.side_effect=UpdateError('invented_native_refusal')
  with self.assertRaises(UpdateError):self.run_command()
  self.dispatch.assert_called_once();self.prepare.assert_called_once()
  self.assertTrue(self.writer.call_args_list)
  self.assertTrue(all(call.kwargs.get('private')is True for call in self.writer.call_args_list))
 def test_running_foreign_controller_is_refused_without_mutation_or_dispatch(self):
  self.service.return_value={'MainPID':123,'ActiveState':'active'}
  with self.assertRaises(UpdateError):self.run_command()
  self.prepare.assert_not_called();self.dispatch.assert_not_called();self.process.assert_not_called()
