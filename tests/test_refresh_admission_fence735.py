"""Real file locks fence refresh against update identity admission."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import tempfile
import time
import unittest
import subprocess
import sys
import threading

from bot.bot import LobbyBot
from poolrotater_host.layout import LeaseBusy
from poolrotater_host.refresh_fence import GATE, refresh_fence, update_fence
from poolrotater_host import update_drain


class RefreshAdmissionFenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'state/rolling').mkdir(parents=True)
        self.bot = SimpleNamespace(cfg={'_host_root': str(self.root)}, players={}, room_id=42,
            _pending_hard_restart_maps=None, _automatic_hard_restart_due_at=time.time()-10,
            _automatic_hard_restart_waiting_for_empty=False,
            _hard_restart_close_delay_seconds=Mock(return_value=0),
            hard_restart_room=Mock(return_value=True))

    def test_published_gate_blocks_refresh_before_worker_receives_mailbox(self):
        (self.root / GATE).write_text('{}')
        self.assertFalse(LobbyBot._maybe_queue_automatic_hard_restart(self.bot))
        self.bot.hard_restart_room.assert_not_called()
        (self.root / GATE).unlink()
        self.assertTrue(LobbyBot._maybe_queue_automatic_hard_restart(self.bot))

    def test_update_admission_lease_blocks_worker_until_gate_exists(self):
        with patch('poolrotater_host.ratings.operations._identity', return_value=nullcontext()):
            with update_fence(self.root, 123):
                self.assertFalse(LobbyBot._maybe_queue_automatic_hard_restart(self.bot))
                self.bot.hard_restart_room.assert_not_called()
                (self.root / GATE).write_text('{}')
            self.assertFalse(LobbyBot._maybe_queue_automatic_hard_restart(self.bot))
            self.bot.hard_restart_room.assert_not_called()

    def test_refresh_lease_blocks_admission_while_recreation_is_in_flight(self):
        with patch('poolrotater_host.ratings.operations._identity', return_value=nullcontext()):
            with refresh_fence(self.root) as admitted:
                self.assertTrue(admitted)
                with self.assertRaises(LeaseBusy):
                    with update_fence(self.root, 123):
                        self.fail('admission crossed a running room refresh')
            with update_fence(self.root, 123):
                pass

    def test_unavailable_fence_does_not_crash_or_restart_room(self):
        (self.root / 'state/rolling').rename(self.root / 'moved')
        self.assertFalse(LobbyBot._maybe_queue_automatic_hard_restart(self.bot))
        self.bot.hard_restart_room.assert_not_called()

    def test_restart_exception_is_not_misclassified_as_fence_contention(self):
        from poolrotater_host.config import ConfigError
        for error in (ConfigError('restart failure'), OSError('restart failure')):
            with self.subTest(error=type(error).__name__):
                self.bot.hard_restart_room.side_effect = error
                with self.assertRaises(type(error)):
                    LobbyBot._maybe_queue_automatic_hard_restart(self.bot)
                # The lease must be released even for an uncertain restart.
                with refresh_fence(self.root) as admitted:
                    self.assertTrue(admitted)

    def test_real_request_holds_lease_until_mailbox_gate_is_published(self):
        def admission(*args, **kwargs):
            self.assertFalse(LobbyBot._maybe_queue_automatic_hard_restart(self.bot))
            (self.root / GATE).write_text('{}')
            return {'phase': 'requested'}
        with patch('poolrotater_host.ratings.operations._identity', return_value=nullcontext()), \
             patch.object(update_drain, '_request_fenced', side_effect=admission):
            result = update_drain.request({'root': self.root}, {'service_uid': 123}, {}, Mock())
        self.assertEqual(result['phase'], 'requested')
        self.assertFalse(LobbyBot._maybe_queue_automatic_hard_restart(self.bot))
        self.bot.hard_restart_room.assert_not_called()

    def test_real_child_refresh_lock_blocks_admission_and_is_released_on_crash(self):
        # A separate process, not just a mock or a second descriptor in this
        # process, must fence admission and release automatically on death.
        source = str(Path(__file__).resolve().parents[1])
        code = ('import sys,time; sys.path.insert(0,sys.argv[1]); '
                'from poolrotater_host.refresh_fence import refresh_fence; '
                'root=sys.argv[2]; '
                'f=refresh_fence(root); f.__enter__(); '
                'print("ready",flush=True); time.sleep(60)')
        child = subprocess.Popen([sys.executable, '-B', '-c', code, source, str(self.root)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        received = []
        reader = threading.Thread(target=lambda: received.append(child.stdout.readline()), daemon=True)
        reader.start(); reader.join(5)
        self.assertFalse(reader.is_alive(), 'child did not acquire the refresh lease')
        self.assertEqual(received, ['ready\n'])
        with patch('poolrotater_host.ratings.operations._identity', return_value=nullcontext()):
            with self.assertRaises(LeaseBusy):
                with update_fence(self.root, 123):
                    self.fail('update admitted during child refresh')
            child.kill(); child.wait(timeout=5)
            with update_fence(self.root, 123):
                (self.root / GATE).write_text('{}')
        self.assertFalse(LobbyBot._maybe_queue_automatic_hard_restart(self.bot))
        self.bot.hard_restart_room.assert_not_called()
