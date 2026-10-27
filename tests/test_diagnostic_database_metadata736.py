"""Unsafe metadata is explained without exposing paths or weakening custody."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from poolrotater_host.diagnostic_logs.common import check_database
from poolrotater_host.network.contracts import NetworkError


class MetadataDiagnosticsTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.main=Path(self.temp.name)/'sensitive-name.sqlite';self.main.touch(mode=0o600)

 def refused(self,member,flag):
  with self.assertLogs('poolrotater_host.diagnostic_logs.common',level='ERROR') as logs:
   with self.assertRaises(NetworkError) as error:check_database(self.main)
  self.assertEqual(error.exception.code,'unsafe_log_database')
  message=' '.join(logs.output)
  self.assertIn('member='+member,message);self.assertIn(flag+'=False',message)
  self.assertNotIn(str(self.main),message);self.assertNotIn(self.main.name,message)

 def test_insecure_wal_mode_is_rejected_with_redacted_diagnostics(self):
  wal=Path(str(self.main)+'-wal');wal.touch(mode=0o600);wal.chmod(0o644)
  self.refused('wal','private_mode')

 def test_symlink_sidecar_and_hardlinked_main_remain_rejected(self):
  shm=Path(str(self.main)+'-shm');shm.symlink_to(self.main)
  self.refused('shm','regular');shm.unlink()
  link=self.main.parent/'hardlink';os.link(self.main,link)
  self.refused('main','single_link')

 def test_wrong_owner_is_rejected_without_revealing_uid(self):
  actual=Path.lstat
  def fake(path):
   st=actual(path)
   if path==self.main:
    from types import SimpleNamespace
    return SimpleNamespace(st_mode=st.st_mode,st_uid=st.st_uid+1,st_nlink=st.st_nlink)
   return st
  with patch.object(Path,'lstat',fake):self.refused('main','owner_matches')

 def test_normal_database_is_accepted(self):
  check_database(self.main)

 def test_transient_private_sidecar_owner_mismatch_is_rechecked(self):
  wal=Path(str(self.main)+'-wal');wal.touch(mode=0o600)
  actual=Path.lstat;calls=0
  def fake(path):
   nonlocal calls
   st=actual(path)
   if path==wal:
    calls+=1
    if calls==1:
     from types import SimpleNamespace
     return SimpleNamespace(st_mode=st.st_mode,st_uid=st.st_uid+1,st_nlink=st.st_nlink)
   return st
  with patch.object(Path,'lstat',fake),patch('poolrotater_host.diagnostic_logs.common.time.sleep') as wait:
   check_database(self.main)
  self.assertEqual(calls,2);wait.assert_called_once_with(0.05)

 def test_persistent_private_sidecar_mismatch_still_fails_closed(self):
  wal=Path(str(self.main)+'-wal');wal.touch(mode=0o600);actual=Path.lstat
  def fake(path):
   st=actual(path)
   if path==wal:
    from types import SimpleNamespace
    return SimpleNamespace(st_mode=st.st_mode,st_uid=st.st_uid+1,st_nlink=st.st_nlink)
   return st
  with patch.object(Path,'lstat',fake),patch('poolrotater_host.diagnostic_logs.common.time.sleep') as wait:
   self.refused('wal','owner_matches')
  self.assertEqual(wait.call_count,3)

 def test_insecure_sidecar_never_waits_or_opens_database(self):
  wal=Path(str(self.main)+'-wal');wal.touch(mode=0o600);wal.chmod(0o644)
  with patch('poolrotater_host.diagnostic_logs.common.time.sleep') as wait:
   self.refused('wal','private_mode')
  wait.assert_not_called()

 def test_unlinked_journal_metadata_is_rechecked_not_accepted(self):
  journal=Path(str(self.main)+'-journal');journal.touch(mode=0o600);actual=Path.lstat
  def fake(path):
   st=actual(path)
   if path==journal:
    from types import SimpleNamespace
    journal.unlink()
    return SimpleNamespace(st_mode=st.st_mode,st_uid=st.st_uid,st_nlink=0)
   return st
  with patch.object(Path,'lstat',fake),patch('poolrotater_host.diagnostic_logs.common.time.sleep') as wait:
   check_database(self.main)
  wait.assert_called_once_with(0.05)

 def test_actual_hardlinked_sidecar_fails_without_retry(self):
  journal=Path(str(self.main)+'-journal');journal.touch(mode=0o600)
  os.link(journal,self.main.parent/'journal-hardlink')
  with patch('poolrotater_host.diagnostic_logs.common.time.sleep') as wait:
   self.refused('journal','single_link')
  wait.assert_not_called()

 def test_persistent_zero_link_metadata_fails_closed(self):
  journal=Path(str(self.main)+'-journal');journal.touch(mode=0o600);actual=Path.lstat
  def fake(path):
   st=actual(path)
   if path==journal:
    from types import SimpleNamespace
    return SimpleNamespace(st_mode=st.st_mode,st_uid=st.st_uid,st_nlink=0)
   return st
  with patch.object(Path,'lstat',fake),patch('poolrotater_host.diagnostic_logs.common.time.sleep') as wait:
   self.refused('journal','single_link')
  self.assertEqual(wait.call_count,3)

if __name__=='__main__':unittest.main()
