"""Pacing never borrows the wire budget; progress remains bounded and private."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import json,multiprocessing,time,unittest
from unittest.mock import patch,Mock
from poolrotater_host import provider_transport as transport
from poolrotater_host.oauth import AuthError
from poolrotater_host.provider_pacing import ProviderAdmissionDeferred
from tests import test_v7_provider_transport as fixtures


def never_ready(pipe,*args):
    time.sleep(30)


def duplicate_progress(pipe,*args):
    for _ in range(2):pipe.send_bytes(b'{"phase":"request","status":null}')
    time.sleep(30)


class SeparateBudgetTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.DeadlineTests.setUpClass.__func__)
    setUp=fixtures.DeadlineTests.setUp
    tearDown=fixtures.DeadlineTests.tearDown
    get=fixtures.DeadlineTests.get

    def test_admission_wait_and_guard_leave_full_budget_for_dispatch(self):
        self.client.timeout=.3
        self.client.gate._next=time.monotonic()+.16
        remaining=[]
        def exchange(port,trust,request,deadline,**kwargs):
            remaining.append(deadline-time.monotonic())
            return {'status':200,'data':{'id':99},'cooldown':0}
        with patch.object(transport,'_owned_exchange',side_effect=exchange):
            with self.client.operation('GET','/api/v2/me',bearer='invented-private') as permit:
                original=permit.deadline
                time.sleep(.04)  # mutable authority recheck after actual pacing
                self.assertEqual(permit.request('GET','/api/v2/me',bearer='invented-private')[0],200)
                self.assertEqual(permit.deadline,original)  # never extend authority
        self.assertGreater(remaining[0],.28)
        self.assertLessEqual(remaining[0],.3)

    def test_actual_paced_tls_response_succeeds_after_original_combined_budget(self):
        self.client.timeout=1.2
        self.client.gate._next=time.monotonic()+.85
        self.provider.wire('/api/v2/me',200,b'{"id":99}',delay=.55)
        start=time.monotonic()
        self.assertEqual(self.get(),(200,{'id':99}))
        elapsed=time.monotonic()-start
        self.assertGreater(elapsed,1.2)  # old shared deadline cannot pass this
        self.assertLess(elapsed,3.4)  # admission + network + bounded child reap

    def test_expired_authority_permit_still_defers_without_starting_exchange(self):
        self.client.timeout=.08
        with patch.object(transport,'_owned_exchange') as exchange:
            with self.client.operation('GET','/api/v2/me',bearer='invented-private') as permit:
                time.sleep(.1)
                with self.assertRaises(ProviderAdmissionDeferred):
                    permit.request('GET','/api/v2/me',bearer='invented-private')
                self.assertFalse(permit.used)
            exchange.assert_not_called()
        self.assertEqual(self.provider.calls,[])

    def test_fixed_live_adapters_also_keep_full_wire_budget_after_admission(self):
        from poolrotater_host.provider_preflight import (
            FixedProviderTransport,PilotProviderTransport,LivePilotTransport,ProviderConfiguration)
        for cls in (FixedProviderTransport,PilotProviderTransport,LivePilotTransport):
            client=cls.local_state(ProviderConfiguration(request_timeout_seconds=1))
            client.trust=self.trust;client.gate=self.client.gate
            client.gate.execution=client.execution
            for api in (False,True):
                with self.subTest(adapter=cls.__name__,api=api):
                    client.gate._next=time.monotonic()+.16
                    remaining=[]
                    def exchange(port,trust,request,deadline,**kwargs):
                        remaining.append(deadline-time.monotonic())
                        return {'status':200,'data':{'id':99},'cooldown':0}
                    # Entry wiring only. Independent disabled/fixed-origin fence
                    # tests cover admission policy; no upstream dispatch occurs.
                    with patch('poolrotater_host.provider_preflight.require_live_provider'), \
                         patch.object(client,'_qualified_gate'), \
                         patch.object(transport,'_boundary'), \
                         patch.object(transport,'_owned_exchange',side_effect=exchange):
                        operation=client.api_operation if api else client.operation
                        path='/api/v2/beatmaps/99' if api else '/api/v2/me'
                        with operation('GET',path,bearer='invented-private')as permit:
                            invoke=permit.api_request if api else permit.request
                            self.assertEqual(invoke('GET',path,bearer='invented-private')[0],200)
                    self.assertGreater(remaining[0],.98)
                    self.assertLessEqual(remaining[0],1)

    def test_successful_permit_remains_single_use(self):
        with patch.object(transport,'_owned_exchange',return_value={'status':200,'data':{},'cooldown':0})as exchange:
            with self.client.operation('GET','/api/v2/me',bearer='invented-private')as permit:
                permit.request('GET','/api/v2/me',bearer='invented-private')
                with self.assertRaisesRegex(AuthError,'provider_admission_refused'):
                    permit.request('GET','/api/v2/me',bearer='invented-private')
            exchange.assert_called_once()

    def test_startup_timeout_is_diagnosed_and_child_reaped(self):
        self.client.timeout=.5
        with self.assertLogs('poolrotater_host.provider_transport',level='WARNING')as logs:
            with patch.object(transport,'_worker',never_ready):
                with self.assertRaisesRegex(AuthError,'provider_deadline_exceeded'):self.get()
        self.assertIn('phase=startup',logs.output[0]);self.assertIn('outcome=deadline',logs.output[0])
        self.assertEqual(self.provider.calls,[])

    def test_body_timeout_has_status_and_never_logs_credentials_ids_or_content(self):
        self.client.timeout=.8
        self.provider.wire('/api/v2/me',200,b'{"private_body":"must-never-be-logged"}',delay=1.5)
        with self.assertLogs('poolrotater_host.provider_transport',level='WARNING')as logs:
            with self.assertRaisesRegex(AuthError,'provider_deadline_exceeded'):self.get()
        text=' '.join(logs.output)
        self.assertIn('phase=response_body',text);self.assertIn('http_status=200',text)
        for value in ('invented-secret','must-never-be-logged','/api/v2/me','private_body'):
            self.assertNotIn(value,text)

    def test_duplicate_progress_cannot_renew_deadline_or_hide_protocol_failure(self):
        self.client.timeout=.8
        start=time.monotonic()
        with self.assertLogs('poolrotater_host.provider_transport',level='WARNING')as logs:
            with patch.object(transport,'_worker',duplicate_progress):
                with self.assertRaisesRegex(AuthError,'provider_transport_uncertain'):self.get()
        self.assertIn('phase=request',' '.join(logs.output))
        self.assertLess(time.monotonic()-start,1.8)
        self.assertEqual(self.provider.calls,[])

    def test_logging_failure_is_observational_and_operation_labels_hide_room_ids(self):
        request=Mock(path='/api/v2/chat/channels/987654321/messages',method='POST')
        self.assertEqual(transport._request_kind(request),'chat_send')
        with patch.object(transport.logging,'getLogger',return_value=Mock(log=Mock(side_effect=RuntimeError('invented-logger-failure')))):
            transport._timing(request,phase='request',outcome='failed',elapsed=1.,budget=5.)
