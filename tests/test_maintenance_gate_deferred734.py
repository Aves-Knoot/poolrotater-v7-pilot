"""Typed local no-dispatch deferrals for the native maintenance gate."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import json
from pathlib import Path
import queue
import socket
import tempfile
from types import SimpleNamespace
import unittest

from bot.hub import HubInvocationUncertain
from bot.multiroom import SharedReferee
from rolling.adapters import Hub as WorkerHub
from rolling.broker import Broker
from rolling.maintenance import MaintenanceGate
from rolling.protocol import Peer


class _FixtureReferee:
    def __init__(self):
        self._connect_generation = 1
        self.connected = True
        self.calls = []
        self.after_dispatch_error = False

    def is_connected(self):
        return self.connected

    def invoke(self, method, args, **_kwargs):
        self.calls.append((method, int(args[0]), 'password' in args[1],
                           args[1].get('password') == ''))
        if self.after_dispatch_error:
            raise HubInvocationUncertain('fixture private marker must not be logged')
        return {'accepted': True}


class MaintenanceGateDeferredTests(unittest.TestCase):
    def _stack(self, root):
        fixture = _FixtureReferee()
        shared = SharedReferee(fixture, queue.Queue())
        owner = SimpleNamespace(shared=shared)
        broker = object.__new__(Broker)
        broker.owner, broker.alias, broker.generation, broker.cfg = owner, 'room2', 'fixture-generation', {}
        broker.peer = None
        broker.active, broker.fenced, broker.quiesced = True, False, False
        broker.private_pilot = None
        broker.private, broker.pending_public, broker.uncertain, broker.known = False, False, False, True
        broker.password = ''
        broker.players = set()
        broker.hub = shared.room_view('room2', SimpleNamespace(put=lambda _item: None))
        broker.hub.owned.add(102)
        broker.hub.joined[102] = (1, {'room_id': 102})

        parent_sock, worker_sock = socket.socketpair()
        parent = Peer(parent_sock, handler=broker.handle, name='maintenance-gate-parent')
        worker = Peer(worker_sock, name='maintenance-gate-worker')
        parent.start(); worker.start()
        self.addCleanup(parent.close)
        self.addCleanup(worker.close)

        adapter = WorkerHub(worker)
        bot = SimpleNamespace(hub=adapter, room_id=102, _room_ready=True, running=True,
            state='IDLE', players={}, _hub_rejoin_pending=False, events=queue.Queue(),
            _flush_all_lobby_time=lambda: None, handle_event=lambda *_args: None)
        gate_path = root / 'state' / 'supervisor-maintenance.json'
        release = root / 'candidate-release'
        release.mkdir()
        gate_path.parent.mkdir(parents=True)
        gate_path.write_text(json.dumps({'format': 1, 'phase': 'locking', 'nonce': 'a' * 32,
            'expires_at': 4_000_000_000, 'bridge_release': str(release.resolve())}))
        gate = MaintenanceGate(gate_path, release=release)
        return fixture, broker, bot, gate, gate_path

    def _tick_now(self, gate, bot):
        gate.checked_at = 0.0
        return gate.tick(bot)

    def test_quiesced_rpc_defers_then_retries_once_after_guard_clears(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture, broker, bot, gate, _path = self._stack(Path(temp))
            broker.quiesced = True
            self.assertTrue(self._tick_now(gate, bot))
            self.assertEqual(gate.status(), {'nonce': 'a' * 32, 'locked': False,
                'uncertain': False, 'error': 'maintenance lock waiting for referee operation'})
            self.assertEqual(fixture.calls, [])

            broker.quiesced = False
            self.assertTrue(self._tick_now(gate, bot))
            self.assertTrue(gate.status()['locked'])
            self.assertFalse(gate.status()['uncertain'])
            self.assertIsNone(gate.status()['error'])
            self.assertEqual(fixture.calls, [('ChangeRoomSettings', 102, True, False)])
            self.assertEqual(set(gate.status()), {'nonce', 'locked', 'uncertain', 'error'})

    def test_dispatched_unknown_outcome_stays_paused_and_never_replays(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture, _broker, bot, gate, _path = self._stack(Path(temp))
            fixture.after_dispatch_error = True
            with self.assertLogs('rolling.maintenance', level='WARNING') as captured:
                self.assertTrue(self._tick_now(gate, bot))
            self.assertTrue(gate.status()['uncertain'])
            self.assertFalse(gate.status()['locked'])
            self.assertEqual(gate.status()['error'], 'maintenance lock outcome requires reconciliation')
            self.assertEqual(fixture.calls, [('ChangeRoomSettings', 102, True, False)])
            self.assertNotIn('fixture private marker', '\n'.join(captured.output))
            self.assertIn('referee_invocation_uncertain', '\n'.join(captured.output))
            self.assertTrue(self._tick_now(gate, bot))
            self.assertEqual(len(fixture.calls), 1)

    def test_unlock_deferral_preserves_private_fence_until_confirmed_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture, broker, bot, gate, path = self._stack(Path(temp))
            self.assertTrue(self._tick_now(gate, bot))
            self.assertTrue(gate.status()['locked'])
            path.unlink()  # Cancellation asks the already-locked gate to release.
            broker.quiesced = True
            bot.players = {7: {'status': 'idle'}}  # Late join remains untouched.
            self.assertTrue(self._tick_now(gate, bot))
            self.assertTrue(gate.status()['locked'])
            self.assertFalse(gate.status()['uncertain'])
            self.assertEqual(gate.status()['error'], 'maintenance unlock waiting for referee operation')
            self.assertEqual(fixture.calls, [('ChangeRoomSettings', 102, True, False)])

            broker.quiesced = False
            self.assertFalse(self._tick_now(gate, bot))
            self.assertFalse(gate.status()['locked'])
            self.assertFalse(gate.status()['uncertain'])
            self.assertIsNone(gate.status()['error'])
            self.assertEqual(fixture.calls[-1], ('ChangeRoomSettings', 102, True, True))
            self.assertEqual(bot.players, {7: {'status': 'idle'}})

    def test_foreign_request_is_ignored_without_downstream_call(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture, _broker, bot, gate, path = self._stack(Path(temp))
            request = json.loads(path.read_text())
            request['bridge_release'] = str(Path(temp) / 'foreign-release')
            path.write_text(json.dumps(request))
            self.assertFalse(self._tick_now(gate, bot))
            self.assertEqual(gate.status(), {'nonce': None, 'locked': False,
                'uncertain': False, 'error': None})
            self.assertEqual(fixture.calls, [])


if __name__ == '__main__':
    unittest.main()
