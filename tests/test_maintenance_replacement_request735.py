"""Safe replacement sequencing for retained maintenance evidence."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from contextlib import nullcontext
from pathlib import Path
import os
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from poolrotater_host import operator_maintenance as operations
from poolrotater_host.config import ConfigError
from poolrotater_host.layout import atomic_json, load, read_json, setup


class ReplacementRequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        self.root = folder / 'host'
        setup(self.root, ['standard-auto', 'mania-4k', 'mania-7k'])
        marker, _ = load(self.root)
        self.prefix = folder / 'prefix'
        self.prefix.mkdir(mode=0o700)
        self.parent = {**marker, 'prefix': str(self.prefix),
            'host_root': str(self.root), 'service_uid': os.geteuid(),
            'bundle_sha256': 'a' * 64, 'release': str(self.prefix / 'release')}
        self.target = {'prefix': self.prefix, 'root': self.root}
        self.binding = self.enterContext(patch.object(operations,
            'administrative_binding', return_value=(self.target, marker, self.parent)))
        self.service_state = self.enterContext(patch(
            'poolrotater_host.pilot_operations._service_state',
            return_value={'ActiveState': 'active', 'MainPID': 123}))
        self.enterContext(patch('poolrotater_host.ratings.operations._identity',
            side_effect=lambda _uid: nullcontext()))
        control, write = operations._control, operations._write
        self.enterContext(patch.object(operations, '_control',
            side_effect=lambda path, _owner=0, **kw: control(path, os.geteuid(), **kw)))
        self.enterContext(patch.object(operations, '_write',
            side_effect=lambda path, value, **kw: write(path, value, owner=os.geteuid(), **kw)))
        self.old_id = str(uuid.uuid4())
        self.new_id = str(uuid.uuid4())
        self.aliases = ['room1', 'room2', 'room3']
        self.config_before = (self.root / 'config/host.json').read_bytes()

    def request(self, operation_id):
        return operations.command('test', 'request', operation_id=operation_id,
            mode='empty', rooms=self.aliases)

    def seed_old_review(self):
        old = self.request(self.old_id)['request']
        receipt_path = self.root / operations.RECEIPT
        receipt_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        receipt = {'format': 1, 'id': self.old_id, 'phase': 'requires_review',
            'mode': 'empty', 'controller_pid': 123, 'updated_at': time.time(),
            'rooms': {'room1': 'room_changed', 'room2': 'closed', 'room3': 'closed'},
            'failures': {'room1': {'failure_code': 'service_failed'}}}
        atomic_json(receipt_path, receipt)
        return old, receipt

    def seed_controller_update(self, phase='held'):
        atomic_json(self.prefix / 'controller-update.json',
                    {'id': 'controller-update', 'phase': phase})

    def test_fresh_request_is_refused_while_update_is_held(self):
        _old, receipt = self.seed_old_review()
        self.seed_controller_update()
        self.service_state.reset_mock()
        with self.assertRaisesRegex(ConfigError, 'deployment_pending'):
            self.request(self.new_id)
        self.assertEqual(operations._control(self.prefix / operations.NAME)['id'], self.old_id)
        self.assertEqual(read_json(self.root / operations.RECEIPT), receipt)
        self.assertEqual((self.root / 'config/host.json').read_bytes(), self.config_before)
        self.service_state.assert_called_once()

    def test_exact_old_cancel_is_accepted_without_service_or_room_actions(self):
        _old, receipt = self.seed_old_review()
        self.seed_controller_update()
        self.service_state.reset_mock()
        response = operations.command('test', 'cancel', operation_id=self.old_id)
        self.assertEqual(response['request']['id'], self.old_id)
        self.assertEqual(response['request']['phase'], 'cancelled')
        self.assertEqual(read_json(self.root / operations.RECEIPT), receipt)
        self.service_state.assert_not_called()
        self.assertEqual((self.root / 'config/host.json').read_bytes(), self.config_before)

    def test_new_uuid_is_accepted_only_after_cancel_ack_and_old_id_stays_retired(self):
        self.seed_old_review()
        self.seed_controller_update()
        operations.command('test', 'cancel', operation_id=self.old_id)
        with self.assertRaisesRegex(ConfigError, 'deployment_pending'):
            self.request(self.new_id)

        atomic_json(self.prefix / 'controller-update.json',
                    {'id': 'controller-update', 'phase': 'cancelled'})
        with self.assertRaisesRegex(ConfigError, 'maintenance_operation_pending'):
            self.request(self.new_id)
        retained = {'format': 1, 'id': self.old_id, 'phase': 'cancelled',
            'mode': 'empty', 'controller_pid': 123, 'updated_at': time.time(),
            'rooms': {'room1': 'cancelled', 'room2': 'closed', 'room3': 'closed'},
            'failures': {'room1': {'failure_code': 'service_failed'}}}
        atomic_json(self.root / operations.RECEIPT, retained)

        accepted = self.request(self.new_id)
        request = accepted['request']
        self.assertEqual(request['id'], self.new_id)
        self.assertNotEqual(request['id'], self.old_id)
        self.assertEqual(request['phase'], 'requested')
        self.assertEqual(request['mode'], 'empty')
        self.assertEqual(request['rooms'], self.aliases)
        self.assertEqual(accepted['progress'], retained)
        self.assertEqual(operations._control(self.prefix / operations.NAME), request)
        with self.assertRaisesRegex(ConfigError, 'maintenance_operation_pending'):
            self.request(self.old_id)
        with self.assertRaisesRegex(ConfigError, 'operation_changed'):
            operations.command('test', 'cancel', operation_id=self.old_id)
        self.assertEqual(operations._control(self.prefix / operations.NAME)['id'], self.new_id)
        self.assertEqual((self.root / 'config/host.json').read_bytes(), self.config_before)


if __name__ == '__main__':
    unittest.main()
