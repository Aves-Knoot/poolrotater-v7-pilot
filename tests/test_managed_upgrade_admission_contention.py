"""Admission may wait for cleanup; the admitted transaction is never replayed."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import fcntl
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from poolrotater_host import pilot_upgrade as upgrade
from poolrotater_host.config import ConfigError
from poolrotater_host.pilot_installation import CleanupPending,_quiescent

class UpgradeAdmissionContention(unittest.TestCase):
 def setUp(self):
  temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.base=Path(temporary.name);self.root=self.base/'host';(self.root/'state').mkdir(parents=True)
  self.owner=self.root/'state/owner.lock';self.owner.touch(mode=0o600);self.uid=os.geteuid();self.clock=0.;self.delays=[]
  self.check=self.enterContext(patch.object(upgrade,'require_no_service_processes'))
 def sleep(self,delay):self.delays.append(delay);self.clock+=delay
 def guard(self,seconds=1):return upgrade._admission_guard(self.root,self.uid,seconds=seconds,monotonic=lambda:self.clock,sleep=self.sleep)
 def assert_unlocked(self):
  with _quiescent(self.root,self.uid):pass
 def test_transient_uid_reader_finishes_before_admission(self):
  self.check.side_effect=[CleanupPending('update_children_still_running','invented reader'),None]
  with self.guard():self.assertEqual(self.check.call_count,2)
  self.assertEqual(self.delays,[.5]);self.assert_unlocked()
 def test_owner_lease_can_clear_without_resetting_it(self):
  fd=os.open(self.owner,os.O_RDONLY);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  original=self.sleep
  def finish(delay):original(delay);os.close(fd)
  self.sleep=finish
  before=self.owner.stat()
  with self.guard():self.check.assert_called_once_with(self.uid)
  self.assertEqual(self.owner.stat().st_ino,before.st_ino);self.assert_unlocked()
 def test_persistent_uid_child_expires_before_body(self):
  self.check.side_effect=CleanupPending('update_children_still_running','invented persistent reader')
  with self.assertRaisesRegex(ConfigError,'update_cleanup_timeout'):
   with self.guard(seconds=0):self.fail('transaction admitted despite live child')
  self.assertEqual(self.delays,[]);self.assert_unlocked()
 def test_persistent_owner_lease_expires_before_body(self):
  fd=os.open(self.owner,os.O_RDONLY);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  try:
   with self.assertRaisesRegex(ConfigError,'update_cleanup_timeout'):
    with self.guard(seconds=0):self.fail('transaction admitted despite owner lease')
  finally:os.close(fd)
  self.check.assert_not_called();self.assert_unlocked()
 def test_unverifiable_uid_census_is_not_retried(self):
  self.check.side_effect=ConfigError('cannot verify stopped pilot processes')
  with self.assertRaisesRegex(ConfigError,'cannot verify'):
   with self.guard():self.fail('unverifiable process census admitted')
  self.check.assert_called_once();self.assertFalse(self.delays);self.assert_unlocked()
 def test_unknown_cleanup_reason_is_not_retried(self):
  self.check.side_effect=CleanupPending('invented_unknown_reason','invented unknown guard')
  with self.assertRaises(CleanupPending):
   with self.guard():self.fail('unknown guard admitted')
  self.check.assert_called_once();self.assertFalse(self.delays);self.assert_unlocked()
 def test_invalid_owner_permissions_are_not_retried(self):
  self.owner.chmod(0o644)
  with self.assertRaisesRegex(ConfigError,'invalid pilot owner lock'):
   with self.guard():self.fail('invalid owner lock admitted')
  self.check.assert_not_called();self.assertFalse(self.delays)
 def test_cleanup_exception_inside_transaction_is_never_replayed(self):
  entered=0
  with self.assertRaises(CleanupPending):
   with self.guard():
    entered+=1;raise CleanupPending('update_children_still_running','invented post-admission failure')
  self.assertEqual(entered,1);self.check.assert_called_once();self.assertFalse(self.delays);self.assert_unlocked()
 def test_io_exception_inside_transaction_is_never_replayed(self):
  with self.assertRaisesRegex(OSError,'invented write failure'):
   with self.guard():raise OSError('invented write failure')
  self.check.assert_called_once();self.assertFalse(self.delays);self.assert_unlocked()
 def test_stopped_upgrade_entry_rechecks_binding_after_wait(self):
  prefix=self.base/'prefix';prefix.mkdir();target={'prefix':prefix,'root':self.root};marker={'installation_id':'invented'};record={'service_uid':self.uid}
  self.enterContext(patch.object(upgrade,'_admin'));binding=self.enterContext(patch.object(upgrade,'_binding',return_value=(target,marker,prefix/'record.json',record)))
  state=self.enterContext(patch.object(upgrade,'_unit_state'));self.check.side_effect=[CleanupPending('update_children_still_running','invented late reader'),None]
  with upgrade._locked('invented')as observed:self.assertEqual(observed,(target,marker,prefix/'record.json',record))
  self.assertEqual(binding.call_count,2);self.assertEqual(state.call_count,2);self.assertEqual(self.check.call_count,2);self.assert_unlocked()
