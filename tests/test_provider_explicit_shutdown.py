"""Only explicit coordinated stops bypass occupancy; ownership/roster fences stay."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import os,signal,unittest
from pathlib import Path
from unittest.mock import patch
from poolrotater_host import provider_service as service
from poolrotater_host.config import ConfigError
from poolrotater_host.layout import atomic_json,read_json
from poolrotater_host.testing.fixtures import create_catalogs
from test_v7_provider_rooms import RoomCase
from test_v7_runtime import fixture_release
from test_provider_parent_logging import preserve_logging

class ExplicitInstalledEntryStopTests(RoomCase):
 def setUp(self):
  super().setUp();preserve_logging(self)
  config=read_json(self.root/'config/host.json');config['rooms']=config['rooms'][:1];atomic_json(self.root/'config/host.json',config)
  create_catalogs(self.root);self.release=fixture_release(self.root,'v7.0.0')
  self.auth=self.authorized();self.backend,self.api,self.hub=self.adapters(self.auth)
  self.enterContext(patch('rolling.supervisor.require_immutable'))
 def fixed(self,observe):
  original=service.run_provider_service
  def run(*args,**kwargs):return original(*args,**kwargs,observer=observe)
  runtime={'registration':{'execution':'pilot-live'}}
  with patch('poolrotater_host.installed_authorization._runtime_guard',return_value=lambda:runtime),patch.object(service,'require_live_provider'),\
   patch('poolrotater_host.installed_authorization.installed_provider_authentication',return_value=self.auth),\
   patch('poolrotater_host.provider_transport.TrustSnapshot.system',return_value=object()),\
   patch.object(service,'ProviderRoomApi',return_value=self.api),patch.object(service,'ProviderRoomHub',return_value=self.hub),\
   patch.object(service,'run_provider_service',side_effect=run):
   return service.run_fixed_provider_service(self.root,self.release)
 def occupied(self,owner,worker):
  rid=worker.status['room_id'];self.backend.rooms[rid]['players']=[{'user_id':7}]
  self.backend.wire.route('UserJoined',{'room_id':rid,'user_id':7})
  owner.wait_for(lambda:worker.broker.known and worker.broker.players=={7} and not worker.broker.hub.roster_fenced,5,worker)
  self.assertTrue(worker.broker.known);self.assertEqual(worker.broker.players,{7});self.assertFalse(worker.broker.empty())
  return rid
 def test_fixed_entry_sigterm_explicitly_closes_occupied_owned_room_and_reaps(self):
  phases=[];saved=[]
  def observe(phase,owner,workers):
   phases.append(phase)
   if phase=='running':
    w,=workers;rid=self.occupied(owner,w);saved.append(w)
    self.assertFalse(owner._explicit_shutdown)
    os.kill(os.getpid(),signal.SIGTERM)
    self.assertTrue(owner._explicit_shutdown)
  self.fixed(observe)
  self.assertEqual(phases,['running','stopped']);self.assertFalse(self.backend.rooms)
  self.assertEqual(self.backend.authenticated_calls.count('CloseRoom'),1);self.assertIsNotNone(saved[0].process.poll())
  state=read_json(self.root/'state/rolling/ownership.json')['rooms']['room1'];self.assertEqual(state['phase'],'stopped');self.assertIsNone(state['room_id']);self.assertFalse(state['ownership_unknown'])
 def test_authorized_parent_shutdown_closes_occupied_without_worker_flag(self):
  saved=[]
  def observe(phase,owner,workers):
   if phase=='running':
    w,=workers;self.occupied(owner,w);saved.append(w);owner.request_shutdown()
    self.assertNotIn('_explicit_shutdown',w.broker.cfg)
  self.fixed(observe);self.assertFalse(self.backend.rooms);self.assertIsNotNone(saved[0].process.poll())
 def test_failure_cleanup_closes_known_occupied_room_before_transport_stop(self):
  saved=[]
  def observe(phase,owner,workers):
   if phase=='running':
    w,=workers;self.occupied(owner,w);saved.append(w)
    self.assertFalse(owner._explicit_shutdown);raise RuntimeError('invented startup failure')
  with self.assertRaisesRegex(RuntimeError,'invented startup failure'):self.fixed(observe)
  self.assertEqual(self.backend.authenticated_calls.count('CloseRoom'),1);self.assertFalse(self.backend.rooms)
  self.assertIsNotNone(saved[0].process.poll());state=read_json(self.root/'state/rolling/ownership.json')['rooms']['room1'];self.assertEqual(state['phase'],'stopped');self.assertIsNone(state['room_id'])
 def test_failed_standard_closes_occupied_4k_sibling_and_keeps_original_failure(self):
  from poolrotater_host.config import defaults
  config=read_json(self.root/'config/host.json');config['rooms']=defaults(['standard-auto','mania-4k'])['rooms'];atomic_json(self.root/'config/host.json',config)
  create_catalogs(self.root);saved=[]
  def observe(phase,owner,workers):
   if phase=='running':
    self.assertEqual([w.status['ruleset']for w in workers],[0,3])
    for w in workers:self.occupied(owner,w)
    saved.extend(workers);workers[0].failure='invented fatal Standard error'
  with self.assertRaisesRegex(ConfigError,'provider_worker_failed'):self.fixed(observe)
  self.assertEqual(self.backend.authenticated_calls.count('CloseRoom'),2);self.assertFalse(self.backend.rooms)
  self.assertTrue(all(w.process.poll()is not None for w in saved))
  self.assertTrue(all(s['phase']=='stopped'and s['room_id']is None for s in read_json(self.root/'state/rolling/ownership.json')['rooms'].values()))
 def test_explicit_stop_never_closes_uncertain_owned_room(self):
  saved=[]
  def observe(phase,owner,workers):
   if phase=='running':
    w,=workers;self.occupied(owner,w);saved.append(w);w.broker.uncertain=True;owner.request_shutdown()
  with self.assertRaises(ConfigError):self.fixed(observe)
  # An explicit stop cannot turn an unknown mutation outcome into permission
  # to send CloseRoom. Retain recovery state and reap the local process.
  self.assertEqual(self.backend.authenticated_calls.count('CloseRoom'),0)
  self.assertTrue(self.backend.rooms);self.assertIsNotNone(saved[0].process.poll())
  self.assertEqual(read_json(self.root/'state/rolling/ownership.json')['rooms']['room1']['phase'],'recovery_required')
 def test_explicit_stop_preserves_unresolved_roster_ordering_guard(self):
  saved=[]
  def observe(phase,owner,workers):
   if phase=='running':
    w,=workers;self.occupied(owner,w);saved.append(w)
    # Keep an actual ordering gap unresolved throughout cleanup. A bare flag
    # can be cleared by an already queued roster event and makes this test race.
    w.broker.hub.roster_fenced=True
    w.broker.hub.roster_progress=lambda:(1,2,True)
    owner.request_shutdown()
  with self.assertRaises(ConfigError):self.fixed(observe)
  # Explicit shutdown does not bypass the broker's ordered-event fence.
  self.assertEqual(self.backend.authenticated_calls.count('CloseRoom'),0)
  self.assertTrue(self.backend.rooms);self.assertIsNotNone(saved[0].process.poll())
  self.assertEqual(read_json(self.root/'state/rolling/ownership.json')['rooms']['room1']['phase'],'recovery_required')
if __name__=='__main__':unittest.main()
