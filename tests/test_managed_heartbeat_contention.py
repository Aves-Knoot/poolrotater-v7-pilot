"""Local lease contention must not suppress independent heartbeats for minutes."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
from types import SimpleNamespace
import os,sys,tempfile,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from poolrotater_host.layout import setup,LeaseBusy,read_json
from poolrotater_host.config import ConfigError
from poolrotater_host.network.agent import Agent

class HeartbeatContention(unittest.TestCase):
 def setUp(self):
  mask=os.umask(0o077);self.addCleanup(os.umask,mask)
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)/'host';setup(self.root)
  self.error=None;self.status_error=None;self.observation_error=None;self.calls=0
  def status():
   if self.status_error:raise self.status_error
   return {'configured':True,'enrolled':True,'credential_expires_at':2000,'pending_operation':None}
  def publish(value):
   self.calls+=1
   if self.error:raise self.error
  def observe():
   if self.observation_error:raise self.observation_error
   return {'rooms':[]}
  self.client=SimpleNamespace(root=self.root,status=status,publish=publish)
  self.agent=Agent(self.client,observe,clock=lambda:1000,jitter=lambda a,b:0)
 def assert_short_retry(self):
  value=self.agent.step();self.assertEqual(value['phase'],'retrying');self.assertIsNone(value['error'])
  self.assertEqual(value['retry_seconds'],5);self.assertFalse(value['live_rooms_affected'])
  self.assertEqual(read_json(self.agent.path),value)
 def test_busy_publish_lease_is_short_retry_without_review_or_remote_failure(self):
  self.error=LeaseBusy('private lease detail');self.assert_short_retry()
  self.assertNotIn('private',str(read_json(self.agent.path)));self.assertEqual(self.agent.failures,0)
 def test_busy_status_lease_never_dispatches_a_heartbeat(self):
  self.status_error=LeaseBusy('busy');self.assert_short_retry();self.assertEqual(self.calls,0)
 def test_busy_observation_lease_never_dispatches_a_heartbeat(self):
  self.observation_error=LeaseBusy('busy');self.assert_short_retry();self.assertEqual(self.calls,0)
 def test_repeated_busy_lease_does_not_expand_to_five_minute_backoff(self):
  self.error=LeaseBusy('busy')
  for _ in range(10):self.assert_short_retry()
 def test_release_resumes_normal_heartbeat_without_resetting_any_state(self):
  self.error=LeaseBusy('busy');self.assert_short_retry();self.error=None
  value=self.agent.step();self.assertEqual(value['phase'],'connected');self.assertEqual(value['retry_seconds'],60)
 def test_invalid_owned_data_still_requires_review(self):
  self.error=ConfigError('invalid protected data');value=self.agent.step()
  self.assertEqual(value['phase'],'local-review-required');self.assertEqual(value['error'],'local-state-refused');self.assertEqual(value['retry_seconds'],300)

if __name__=='__main__':unittest.main()
