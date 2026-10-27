"""Rejection evidence survives real IPC without granting absence/retry authority."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import json
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from poolrotater_host.referee_errors import RefereeCommandRejected, safe_completion
from poolrotater_host.service_diagnostics import controller_failure, failure_details
from rolling.protocol import Peer, RemoteError
import test_v7_provider_rooms as rooms


class RejectionEvidenceTests(unittest.TestCase):
    def test_completion_keeps_number_and_drops_raw_body_and_injected_fields(self):
        raw = {'type': 3, 'invocationId': '8', 'error': 'Error 123: bearer secret password private-name',
               'rejection_code': 456, 'headers': {'Authorization': 'sensitive'}, 'secret': 'sensitive'}
        before = json.dumps(raw, sort_keys=True)
        safe = safe_completion(raw)
        self.assertEqual(safe, {'type': 3, 'invocationId': '8', 'error': 'referee_command_rejected',
                                'rejection_code': 123})
        self.assertEqual(json.dumps(raw, sort_keys=True), before)
        self.assertNotIn('secret', json.dumps(safe))

    def test_noncanonical_or_unbounded_codes_are_not_forwarded(self):
        for text in ('secret', 'Error 0: secret', 'Error -1: secret', 'Error 0001: secret',
                     'Error 1234567: secret', 'prefix Error 12: secret', 'Error 12secret', 'Error 12:\nsecret'):
            with self.subTest(text=text):
                self.assertEqual(safe_completion({'type': 3, 'invocationId': '1', 'error': text,
                                                  'rejection_code': 789}),
                                 {'type': 3, 'invocationId': '1', 'error': 'referee_command_rejected'})

    def test_result_error_conflict_remains_refused_before_sanitization(self):
        from poolrotater_host.oauth import AuthError
        with self.assertRaises(AuthError):
            safe_completion({'type': 3, 'invocationId': '1', 'error': 'Error 1: secret', 'result': {}})

    def test_success_and_noncompletion_are_not_relabelled(self):
        for value in ({'type': 3, 'invocationId': '1', 'result': {'room_id': 101}}, {'type': 6}):
            self.assertIs(safe_completion(value), value)

    def test_diagnostic_classifies_reply_without_claiming_no_mutation(self):
        for code in (None, 123):
            result = failure_details(RefereeCommandRejected(code))
            self.assertEqual(result['failure_code'], 'referee_command_rejected')
            self.assertEqual(result['referee_completion'], 'rejected')
            self.assertEqual(result['mutation_outcome'], 'unknown')
            self.assertEqual(result.get('referee_rejection_code'), code)
        for code in (True, 0, -1, 1000000, '123'):
            self.assertEqual(str(RefereeCommandRejected(code)), 'referee_command_rejected')

    def test_untrusted_prose_and_decorated_diagnostic_text_do_not_leak(self):
        for text in ('referee_command_rejected [code 12] secret', 'Error 12: secret',
                     'referee_command_rejected [code 00012]'):
            self.assertEqual(failure_details(RuntimeError(text)), {'failure_code': 'service_failed'})

    def test_real_worker_rpc_preserves_safe_code_and_controller_frame_contract(self):
        left, right = socket.socketpair()
        def reject(*args, **kwargs):
            raise RefereeCommandRejected(123)
        server = Peer(left, handler=reject, name='rejection-test').start()
        client = Peer(right, name='rejection-client').start()
        try:
            with self.assertRaises(RemoteError) as caught:
                client.call('hub.make_room', timeout=2)
            detail = failure_details(caught.exception)
            self.assertEqual(detail['referee_rejection_code'], 123)
            self.assertEqual(detail['mutation_outcome'], 'unknown')
            result = controller_failure(caught.exception, Path(__file__).resolve().parents[1])
            self.assertIn('frames', result)
            self.assertIn('component', result)
            self.assertEqual(result['failure_code'], 'referee_command_rejected')
        finally:
            client.close(); server.close()


class RejectionWireTests(rooms.RoomCase):
    def test_actual_child_sanitizes_completion_and_does_not_reconnect_or_replay(self):
        auth = self.authorized()
        backend, api, hub = self.adapters(auth)
        with auth.verified_owner():
            hub.connect()
            generation = hub._connect_generation
            first = hub.client._next_id
            backend.wire.completion_error = 'Error 123: private upstream detail bearer-secret'
            try:
                with self.assertLogs('poolrotater_host.provider_referee', level='WARNING') as captured:
                    with self.assertRaises(RefereeCommandRejected) as caught:
                        hub.make_room('invented', 1)
                self.assertEqual(caught.exception.rejection_code, 123)
                self.assertEqual(str(caught.exception), 'referee_command_rejected [code 123]')
                self.assertNotIn('bearer-secret', '\n'.join(captured.output))
                self.assertIn('method=MakeRoom', '\n'.join(captured.output))
                self.assertIn('code=123', '\n'.join(captured.output))
                self.assertEqual(hub.client._next_id, first + 1)
                self.assertEqual(hub._connect_generation, generation)
                self.assertTrue(hub.is_connected())
                backend.wire.completion_error = None
                room = hub.make_room('invented-success', 1)
                self.assertEqual(hub._connect_generation, generation)
                hub.invoke('CloseRoom', [room['room_id']])
            finally:
                backend.wire.completion_error = None
                hub.stop()
