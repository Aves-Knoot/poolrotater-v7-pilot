"""Use the actual fixed HTTP adapter shape for room admission regressions."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from poolrotater_host.provider_preflight import FixedProviderTransport,PilotProviderTransport,LivePilotTransport,ProviderConfiguration
from poolrotater_host.provider_rooms import ProviderRoomHub
from poolrotater_host import provider_rooms as rooms
from poolrotater_host.provider_pacing import ProviderAdmissionDeferred
from bot.hub import HubInvocationUncertain

class LiveTransportTimeoutTests(unittest.TestCase):
 def hub(self,timeout=5):
  transport=LivePilotTransport.local_state(ProviderConfiguration(request_timeout_seconds=timeout))
  transport.gate=SimpleNamespace(read_delay=Mock(return_value=0.))
  hub=object.__new__(ProviderRoomHub)
  hub.auth=SimpleNamespace(_active=7,transport=transport)
  hub.client=SimpleNamespace(_connect_generation=1,is_connected=lambda:True,_boundary=lambda:None)
  return hub
 def test_actual_fixed_adapters_expose_configured_readonly_timeout_without_dispatch(self):
  for cls in (FixedProviderTransport,PilotProviderTransport,LivePilotTransport):
   for seconds in (1.,3.,5.):
    with self.subTest(adapter=cls.__name__,seconds=seconds):
     transport=cls.local_state(ProviderConfiguration(request_timeout_seconds=seconds))
     self.assertEqual(transport.timeout,seconds)
     self.assertIsNone(transport.gate);self.assertIsNone(transport.trust)
     with self.assertRaises(AttributeError):transport.timeout=60
 def test_finish_make_uses_real_live_adapter_shape_and_verifies_owner_once(self):
  hub=self.hub()
  snapshot={'room_id':101,'chat_channel_id':201,'players':[],'referees':[{'user_id':7}]}
  evidence={'id':101,'user_id':7,'type':'head_to_head','active':True,'ends_at':None}
  with patch.object(rooms,'_fetch',return_value=(200,evidence)) as fetch:
   self.assertEqual(hub.finish_make_room(snapshot,1),snapshot)
  fetch.assert_called_once_with(hub.auth,'/api/v2/rooms/101')
 def test_refresh_respects_configured_threshold_and_local_deferral(self):
  hub=self.hub(timeout=1.)
  hub.auth.transport.gate.read_delay.return_value=.3
  self.assertFalse(hub.refresh_ready())
  hub.auth.transport.gate.read_delay.return_value=.2
  self.assertTrue(hub.refresh_ready())
  hub.auth.transport.gate.read_delay.side_effect=ProviderAdmissionDeferred('provider_pacing_deferred')
  self.assertFalse(hub.refresh_ready())
 def test_cooldown_waits_then_checks_owner_without_recreating(self):
  hub=self.hub();hub.auth.transport.gate.read_delay.side_effect=[1.,0.]
  snapshot={'room_id':101,'chat_channel_id':201,'players':[],'referees':[{'user_id':7}]}
  evidence={'id':101,'user_id':7,'type':'head_to_head','active':True,'ends_at':None}
  now=[100.];clock=SimpleNamespace(monotonic=lambda:now[0],sleep=lambda delay:now.__setitem__(0,now[0]+delay))
  with patch.object(rooms,'time',clock),patch.object(rooms,'_fetch',return_value=(200,evidence)) as fetch:
   self.assertEqual(hub.finish_make_room(snapshot,1),snapshot)
  self.assertEqual(fetch.call_count,1);self.assertGreater(now[0],100.)
 def test_owner_mismatch_still_refuses_without_adopting_snapshot(self):
  hub=self.hub();snapshot={'room_id':101,'chat_channel_id':201,'players':[],'referees':[{'user_id':7}]}
  evidence={'id':101,'user_id':8,'type':'head_to_head','active':True,'ends_at':None}
  with patch.object(rooms,'_fetch',return_value=(200,evidence)) as fetch:
   with self.assertRaisesRegex(HubInvocationUncertain,'referee_room_owner_requires_review'):
    hub.finish_make_room(snapshot,1)
  self.assertEqual(fetch.call_count,1)
