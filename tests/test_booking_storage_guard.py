"""The rolling worker may use only its pre-created private booking ledger."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import copy
import json
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from poolrotater_host.bookings import game, policy
from poolrotater_host.bookings.local import LocalSessions
from rolling.storage import install_guard


HOST = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
INSTALLATION = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
GENERATION = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
OWNER = 101
BASE = {'sr_min': 3.5, 'sr_max': 4.5, 'countdown_seconds': 30}
NATIVE_CONNECT = sqlite3.connect


def booking_lease(alias='room1'):
    return {
        'id': str(uuid.uuid4()), 'user_id': OWNER, 'designated_friend_id': None,
        'host_id': HOST, 'installation_id': INSTALLATION, 'generation': GENERATION,
        'alias': alias, 'session_generation': uuid.uuid4().hex,
        'preset': 'standard-fixed', 'settings': copy.deepcopy(BASE),
        'password': 'invented-local-booking-password',
    }


def config(root, lease):
    return {
        '_host_root': str(root), '_instance_name': lease['alias'],
        '_booking_session': {key: lease[key] for key in
                             ('id', 'alias', 'session_generation', 'preset')},
    }


def prepare(root, alias='room1'):
    root.mkdir(mode=0o700)
    lease = booking_lease(alias)
    with patch.object(sqlite3, 'connect', NATIVE_CONNECT):
        store = LocalSessions(root, {'host_id': HOST, 'installation_id': INSTALLATION,
                                     'generation': GENERATION})
        store.install(lease, BASE)
    return lease, store


def schema(path):
    with NATIVE_CONNECT(path) as db:
        return tuple(db.execute("SELECT type,name,sql FROM sqlite_master "
                                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))


class BookingStorageGuardTests(unittest.TestCase):
    def test_bound_booking_worker_can_use_precreated_ledger_without_schema_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'host'
            lease, store = prepare(root)
            path = root / 'state' / 'bookings.sqlite3'
            managed_path = root / 'state' / 'managed-host.sqlite3'
            binding = {'host_id': HOST, 'installation_id': INSTALLATION,
                       'generation': GENERATION}
            marker = {'installation_id': INSTALLATION, 'generation': GENERATION}
            with NATIVE_CONNECT(managed_path) as db:
                db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                db.execute('INSERT INTO meta VALUES(?,?)', ('binding', json.dumps(binding)))
            managed_path.chmod(0o600)
            bot = SimpleNamespace(cfg=config(root, lease), pool=SimpleNamespace(sr_min=3.5, sr_max=4.5),
                                  say=lambda _message: None)
            before = schema(path)
            with patch.object(sqlite3, 'connect', NATIVE_CONNECT):
                install_guard(config(root, lease))
                with patch.object(policy, 'load_host', lambda _root: (marker, {})):
                    session_store, row = game.local(bot)
                    self.assertEqual(row['lease']['id'], lease['id'])
                    self.assertTrue(session_store.authorize(lease['id'], OWNER, 'configure'))
                    self.assertEqual(session_store.get(lease['id'])['settings'], BASE)

                    self.assertTrue(game.command(bot, OWNER, 'sr', ['.sr', '4.0', '4.8']))
                    command = session_store.pending(lease['id'])[0]
                    receipt = session_store.acknowledge(
                        command['operation_id'], 'applied',
                        application={'application': 'next_map'},
                        effective_settings={'sr_min': 4.0, 'sr_max': 4.8,
                                            'countdown_seconds': 30})
                    self.assertEqual(receipt['phase'], 'applied')
                    self.assertEqual(session_store.get(lease['id'])['settings']['sr_min'], 4.0)
                    self.assertEqual(session_store.get(lease['id'])['operation_receipts'][0]['phase'], 'applied')

                    with sqlite3.connect(path) as db:
                        with self.assertRaises(sqlite3.DatabaseError):
                            db.execute('CREATE TABLE unauthorized(value TEXT)')

                    with sqlite3.connect(managed_path) as db:
                        self.assertEqual(json.loads(db.execute(
                            "SELECT value FROM meta WHERE key='binding'").fetchone()[0]), binding)
                        with self.assertRaises(sqlite3.DatabaseError):
                            db.execute("UPDATE meta SET value='{}' WHERE key='binding'")
            self.assertEqual(schema(path), before)
            self.assertNotIn('unauthorized', {entry[1] for entry in schema(path)})

    def test_public_workers_and_invalid_exact_bindings_cannot_open_booking_database(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'host'
            lease, _store = prepare(root)
            path = root / 'state' / 'bookings.sqlite3'
            invalid = []
            invalid.append({'_host_root': str(root), '_instance_name': 'room1'})
            wrong_id = config(root, lease); wrong_id['_booking_session']['id'] = 'not-a-uuid'
            invalid.append(wrong_id)
            wrong_alias = config(root, lease); wrong_alias['_booking_session']['alias'] = 'room2'
            invalid.append(wrong_alias)
            wrong_worker = config(root, lease); wrong_worker['_instance_name'] = 'room2'
            invalid.append(wrong_worker)
            wrong_generation = config(root, lease); wrong_generation['_booking_session']['session_generation'] = 'G' * 32
            invalid.append(wrong_generation)
            wrong_preset = config(root, lease); wrong_preset['_booking_session']['preset'] = 'invented-preset'
            invalid.append(wrong_preset)
            extra_field = config(root, lease); extra_field['_booking_session']['catalog_path'] = '/tmp/other.db'
            invalid.append(extra_field)

            for cfg in invalid:
                with self.subTest(cfg=cfg), patch.object(sqlite3, 'connect', NATIVE_CONNECT):
                    install_guard(cfg)
                    with self.assertRaisesRegex(sqlite3.OperationalError, 'undeclared database'):
                        sqlite3.connect(path)

    def test_binding_opens_only_exact_existing_database_and_managed_database_stays_readonly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'host'
            lease, _store = prepare(root)
            state = root / 'state'
            sibling = state / 'bookings-copy.sqlite3'
            new_database = state / 'other.sqlite3'
            arbitrary = Path(temporary) / 'outside.sqlite3'
            for path in (sibling, arbitrary):
                with NATIVE_CONNECT(path) as db:
                    db.execute('CREATE TABLE value(item INTEGER)')

            managed = state / 'managed-host.sqlite3'
            with NATIVE_CONNECT(managed) as db:
                db.execute('CREATE TABLE protected(value INTEGER)')
                db.execute('INSERT INTO protected VALUES(1)')

            with patch.object(sqlite3, 'connect', NATIVE_CONNECT):
                install_guard(config(root, lease))
                with sqlite3.connect(state / 'bookings.sqlite3') as db:
                    self.assertIsNotNone(db.execute('SELECT booking_id FROM host_bookings').fetchone())
                for path in (sibling, new_database, arbitrary):
                    with self.subTest(path=path), self.assertRaisesRegex(
                            sqlite3.OperationalError, 'undeclared database'):
                        sqlite3.connect(path)
                self.assertFalse(new_database.exists())
                with sqlite3.connect(managed) as db:
                    self.assertEqual(db.execute('SELECT value FROM protected').fetchone(), (1,))
                    with self.assertRaises(sqlite3.DatabaseError):
                        db.execute('UPDATE protected SET value=2')


if __name__ == '__main__':
    unittest.main()
