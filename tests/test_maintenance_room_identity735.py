"""Room identity and readiness behavior for the public WorkerDrain API."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import queue
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from rolling.operator_maintenance import WorkerDrain
from bot.bot import LobbyBot
from bot.api import ApiChatDeliveryUncertain


def job(mode='empty', **changes):
    value = {'id': 'maintenance-735', 'phase': 'requested', 'mode': mode,
             'expires_at': time.time() + 3600}
    value.update(changes)
    return value


def bot(room_id=1):
    hub = Mock()
    hub.is_connected.return_value = True
    return SimpleNamespace(running=True, _room_ready=True,
        _hub_rejoin_pending=False, hub=hub, events=queue.Queue(),
        room_id=room_id, channel_id=10, state='IDLE', players={},
        _deliver_room_chat=Mock(), handle_event=Mock(),
        hard_restart_room=Mock(return_value=True),
        _automatic_hard_restart_due_at=time.time()-1,
        _pending_hard_restart_maps=None,
        _hard_restart_close_delay_seconds=Mock(return_value=0))


def scheduled_bot(**changes):
    value = SimpleNamespace(hard_restart_room=Mock(return_value=True),
        _automatic_hard_restart_due_at=time.time() - 1,
        _pending_hard_restart_maps=None,
        _hard_restart_close_delay_seconds=Mock(return_value=0),
        players={}, room_id=1, _automatic_hard_restart_waiting_for_empty=False)
    for name, setting in changes.items():
        setattr(value, name, setting)
    return value


class RoomIdentityTests(unittest.TestCase):
    def empty_ready(self, worker=None, game=None):
        worker = worker or WorkerDrain()
        game = game or bot()
        worker.command(job())
        self.assertFalse(worker.tick(game))
        self.assertEqual(worker.status()['phase'], 'ready')
        return worker, game

    def test_empty_drain_fences_scheduled_refresh_while_occupied_then_empty(self):
        worker = WorkerDrain()
        game = bot()
        game.players = {7: True}
        worker.command(job())
        self.assertTrue(worker.tick(game))
        self.assertTrue(game._operator_maintenance_pending)
        game.players.clear()
        due_at = game._automatic_hard_restart_due_at
        self.assertFalse(LobbyBot._maybe_queue_automatic_hard_restart(game))
        game.hard_restart_room.assert_not_called()
        self.assertEqual(game._automatic_hard_restart_due_at, due_at)
        self.assertFalse(worker.tick(game))
        self.assertEqual(worker.status()['phase'], 'ready')
        self.assertTrue(game._operator_maintenance_pending)

    def test_room_change_stays_terminal_and_keeps_maintenance_fence_until_cancel(self):
        worker, game = self.empty_ready()
        game.room_id = 2
        self.assertTrue(worker.tick(game))
        self.assertEqual(worker.room_id, 1)
        self.assertEqual(worker.status()['phase'], 'room_changed')
        self.assertTrue(game._operator_maintenance_pending)
        scheduled = scheduled_bot(_operator_maintenance_pending=True)
        self.assertFalse(LobbyBot._maybe_queue_automatic_hard_restart(scheduled))
        scheduled.hard_restart_room.assert_not_called()
        worker.command(job(phase='cancelled'))
        self.assertTrue(worker.tick(game))
        self.assertFalse(game._operator_maintenance_pending)

    def test_ready_event_room_change_is_rechecked_and_fenced(self):
        worker, game = self.empty_ready()
        game.events.put(('RoomChanged', {'room_id': 2}))
        def change_room(*_args): game.room_id = 2
        game.handle_event.side_effect = change_room
        self.assertTrue(worker.tick(game))
        self.assertEqual(worker.room_id, 1)
        self.assertEqual(worker.status()['phase'], 'room_changed')
        self.assertTrue(game._operator_maintenance_pending)

    def test_one_map_room_change_is_terminal_and_does_not_replay_notice(self):
        worker = WorkerDrain()
        game = bot()
        game.players = {7: True}
        worker.command(job('one-map'))
        self.assertTrue(worker.tick(game))
        self.assertTrue(worker.status()['notice_confirmed'])
        game.room_id = 2
        self.assertTrue(worker.tick(game))
        self.assertEqual(worker.status()['phase'], 'room_changed')
        self.assertEqual(game._deliver_room_chat.call_count, 1)
        self.assertTrue(worker.status()['notice_confirmed'])
        self.assertTrue(game._operator_maintenance_pending)

    def test_one_map_boundary_consent_does_not_migrate_to_new_room(self):
        worker = WorkerDrain()
        game = bot()
        game.players = {7: True}
        worker.command(job('one-map'))
        self.assertTrue(worker.tick(game))
        game.state = 'PLAYING'
        self.assertTrue(worker.tick(game))
        self.assertTrue(worker.status()['active_map_observed'])
        game.room_id = 2
        self.assertTrue(worker.tick(game))
        self.assertEqual(worker.status()['phase'], 'room_changed')
        self.assertTrue(worker.status()['active_map_observed'])

    def test_uncertain_notice_and_room_change_keep_refresh_fenced(self):
        worker = WorkerDrain()
        game = bot()
        game.players = {7: True}
        game._deliver_room_chat.side_effect = ApiChatDeliveryUncertain('unknown')
        worker.command(job('one-map'))
        self.assertTrue(worker.tick(game))
        self.assertEqual(worker.status()['phase'], 'notice_uncertain')
        self.assertTrue(game._operator_maintenance_pending)
        game.room_id = 2
        self.assertTrue(worker.tick(game))
        self.assertEqual(worker.room_id, 1)
        self.assertEqual(worker.status()['phase'], 'notice_uncertain')
        scheduled = scheduled_bot(_operator_maintenance_pending=True)
        self.assertFalse(LobbyBot._maybe_queue_automatic_hard_restart(scheduled))
        scheduled.hard_restart_room.assert_not_called()
        worker.command(job(phase='cancelled'))
        self.assertTrue(worker.tick(game))
        self.assertFalse(game._operator_maintenance_pending)

    def test_failed_notice_remains_scheduled_refresh_fenced_until_cancel(self):
        worker = WorkerDrain()
        game = bot()
        game.players = {7: True}
        game._deliver_room_chat.side_effect = RuntimeError('refused')
        worker.command(job('one-map'))
        self.assertTrue(worker.tick(game))
        self.assertEqual(worker.status()['phase'], 'notice_failed')
        self.assertTrue(game._operator_maintenance_pending)
        scheduled = scheduled_bot(_operator_maintenance_pending=True)
        self.assertFalse(LobbyBot._maybe_queue_automatic_hard_restart(scheduled))
        scheduled.hard_restart_room.assert_not_called()
        worker.command(job(phase='cancelled'))
        self.assertTrue(worker.tick(game))
        self.assertFalse(game._operator_maintenance_pending)

    def test_cancelled_drain_cannot_resume_after_another_tick_or_command(self):
        worker, game = self.empty_ready()
        worker.command(job(phase='cancelled'))
        self.assertEqual(worker.status()['phase'], 'cancelled')
        self.assertTrue(worker.tick(game))
        self.assertFalse(game._operator_maintenance_pending)
        worker.command(job())
        self.assertTrue(worker.tick(game))
        self.assertEqual(worker.status()['phase'], 'cancelled')

    def test_expiry_releases_refresh_fence_and_allows_scheduled_restart(self):
        worker = WorkerDrain()
        game = bot()
        worker.command(job(expires_at=0))
        self.assertTrue(worker.tick(game))
        self.assertEqual(worker.status()['phase'], 'cancelled')
        self.assertFalse(game._operator_maintenance_pending)
        scheduled = scheduled_bot(_operator_maintenance_pending=False)
        self.assertTrue(LobbyBot._maybe_queue_automatic_hard_restart(scheduled))
        scheduled.hard_restart_room.assert_called_once_with(
            announce=False, close_delay_seconds=0,
            close_reason='automatic_hard_restart')

    def test_expired_drain_cannot_resume(self):
        worker = WorkerDrain()
        game = bot()
        worker.command(job(expires_at=0))
        self.assertTrue(worker.tick(game))
        self.assertEqual(worker.status()['phase'], 'cancelled')
        worker.job['expires_at'] = time.time() + 3600
        self.assertTrue(worker.tick(game))
        self.assertEqual(worker.status()['phase'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
