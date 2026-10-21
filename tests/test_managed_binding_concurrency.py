"""Real binding-lease contention must not randomly fail managed startup."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from concurrent.futures import ThreadPoolExecutor
import json,os,sqlite3,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from poolrotater_host.layout import setup,load,file_lease,LeaseBusy
from poolrotater_host.managed_host.policy import state

class BindingConcurrency(unittest.TestCase):
 def setUp(self):
  mask=os.umask(0o077);self.addCleanup(os.umask,mask)
  temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
  self.root=Path(temp.name)/'host';setup(self.root);self.marker,_=load(self.root)
  self.lock=self.root/'state/managed-binding.lock'
 def test_startup_waits_for_agent_binding_transaction_and_uses_same_store(self):
  attempted=threading.Event();actual=file_lease
  def observed(path,**kwargs):
   if path==self.lock:attempted.set()
   return actual(path,**kwargs)
  with ThreadPoolExecutor(max_workers=1) as pool:
   with file_lease(self.lock),patch('poolrotater_host.layout.file_lease',side_effect=observed):
    future=pool.submit(state,self.root);self.assertTrue(attempted.wait(2))
    time.sleep(.15);self.assertFalse(future.done(),'short contention must not reject startup')
   store=future.result(timeout=3)
  self.assertEqual(store.binding['installation_id'],self.marker['installation_id'])
  self.assertEqual(state(self.root).binding,store.binding)
 def test_concurrent_first_openers_do_not_duplicate_or_replace_binding(self):
  barrier=threading.Barrier(6)
  def open_store(_):barrier.wait(timeout=3);return state(self.root).binding
  with ThreadPoolExecutor(max_workers=6) as pool:values=list(pool.map(open_store,range(6)))
  self.assertTrue(all(value==values[0] for value in values))
  with sqlite3.connect(self.root/'state/managed-host.sqlite3') as db:
   self.assertEqual(db.execute("SELECT COUNT(*) FROM meta WHERE key='binding'").fetchone()[0],1)
 def test_wedged_holder_still_fails_closed_after_bounded_wait(self):
  began=time.monotonic()
  with file_lease(self.lock),self.assertRaises(LeaseBusy):state(self.root)
  self.assertGreaterEqual(time.monotonic()-began,9)
  self.assertLess(time.monotonic()-began,15)
  self.assertFalse((self.root/'state/managed-host.sqlite3').exists())
 def test_unsafe_lock_is_not_adopted_or_waited_on(self):
  other=self.root/'state/unrelated';other.write_text('private sentinel');other.chmod(0o600);self.lock.symlink_to(other)
  with self.assertRaises(OSError):state(self.root)
  self.assertEqual(other.read_text(),'private sentinel')
 def test_wait_does_not_allow_foreign_existing_binding(self):
  store=state(self.root)
  with sqlite3.connect(store.path) as db:
   value=dict(store.binding);value['generation']='00000000-0000-0000-0000-000000000001'
   db.execute("UPDATE meta SET value=? WHERE key='binding'",(json.dumps(value),))
  with self.assertRaisesRegex(Exception,'host_identity_mismatch'):state(self.root)

if __name__=='__main__':unittest.main()
