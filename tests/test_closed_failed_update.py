"""Only fresh, closed failed slots admit automatic repair of a running host."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from poolrotater_host import controller_update as update

class ClosedFailedAdmissionTests(unittest.TestCase):
 def setUp(self):
  self.now=100.;self.target={'root':Path('/invented'),'prefix':Path('/invented-prefix')};self.parent={'service_uid':1000}
  row={'phase':'parked','operation':'closed','pid':None,'room_id':None,'ownership_unknown':False,'error':None,
       'generation':'invented','release':'/invented-release','status':{'reopenable':False,'running':True,'paused':False,'maintenance':{'locked':False,'uncertain':False}}}
  self.rows={a:copy.deepcopy(row)for a in ('room1','room2')};self.journal={'format':1,'rooms':copy.deepcopy(self.rows)}
  self.snapshot={'rooms':self.rows,'generated_at':self.now,'configuration_error':None};self.job={'rooms':['room1','room2'],'phase':'waiting_empty','empty_since':None,'after':{},'id':'invented'}
 def read(self,path,*a,**kw):return copy.deepcopy(self.journal if str(path).endswith('ownership.json')else self.snapshot)
 def accepted(self):
  with patch.object(update,'_control',side_effect=self.read):return update._closed_failed_rows(self.target,self.parent,self.job,now=self.now)
 def test_all_closed_failed_slots_qualify_without_reading_logs_or_provider(self):self.assertIsNotNone(self.accepted())
 def test_unknown_ownership_pending_creation_room_or_child_keeps_gate_closed(self):
  for key,value in [('phase','recovery_required'),('ownership_unknown',True),('operation','make_pending'),('room_id',10),('pid',123),('target','unqualified'),('error','failure')]:
   for which in ('snapshot','journal'):
    with self.subTest(key=key,which=which):
     table=self.rows if which=='snapshot'else self.journal['rooms'];old=copy.deepcopy(table['room1']);table['room1'][key]=value
     self.assertIsNone(self.accepted());table['room1']=old
 def test_manual_close_paused_maintenance_and_mixed_running_hosts_refuse(self):
  for key,value in [('reopenable',True),('paused',True),('maintenance',{'locked':True}),('maintenance',{'uncertain':True})]:
   old=copy.deepcopy(self.rows['room1']['status']);self.rows['room1']['status'][key]=value
   self.assertIsNone(self.accepted());self.rows['room1']['status']=old
  self.rows['room1']['phase']='running';self.assertIsNone(self.accepted())
 def test_stale_changed_identity_missing_alias_and_configuration_error_refuse(self):
  self.snapshot['generated_at']=90;self.assertIsNone(self.accepted());self.snapshot['generated_at']=100
  self.rows['room1']['generation']='different';self.assertIsNone(self.accepted());self.rows['room1']['generation']='invented'
  self.snapshot['configuration_error']='invalid';self.assertIsNone(self.accepted());self.snapshot['configuration_error']=None
  self.rows.pop('room2');self.assertIsNone(self.accepted())
 def test_before_barrier_observation_never_qualifies(self):
  with patch.object(update,'_control',side_effect=self.read):
   self.assertIsNone(update._closed_failed_rows(self.target,self.parent,self.job,now=100,after=100))
 def test_initial_step_records_continuous_window_without_stopping(self):
  save=Mock()
  with patch.object(update,'_closed_failed_rows',return_value=self.rows),patch.object(update,'_commit')as commit:
   self.assertTrue(update._step_closed_failed('invented',self.target,self.parent,self.job,save,now=100))
  commit.assert_not_called();self.assertEqual(save.call_args.kwargs['reason'],'observing_confirmed_closed_failed_rooms')
 def test_revalidation_after_hashing_must_match_before_commit(self):
  self.job.update(empty_since=50,room_ids=dict.fromkeys(self.rows),worker_identity=update.identity(self.rows))
  save=Mock();events=[]
  def checked(*a,**kw):events.append('fresh'if kw.get('after')is not None else 'ordinary');return self.rows
  with patch.object(update,'_closed_failed_rows',side_effect=checked),patch.object(update,'validate_runtime'),patch.object(update,'_commit')as commit:
   self.assertTrue(update._step_closed_failed('invented',self.target,self.parent,self.job,save,now=100))
  commit.assert_called_once();self.assertIn('fresh',events);self.assertEqual(save.call_args.kwargs['admission_kind'],'confirmed_closed_failed_rooms')
