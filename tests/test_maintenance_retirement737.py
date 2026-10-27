"""Crash boundaries for evidence-only retirement; all state is invented/offline."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from poolrotater_host import controller_history as history
from poolrotater_host import maintenance_retirement_io as io
from poolrotater_host import provisioning, update_drain as drain
from poolrotater_host import controller_update
from poolrotater_host.layout import file_lease
from poolrotater_host.config import ConfigError
from poolrotater_host.network.contracts import NetworkError, fingerprint

REAL_PROCESS_ABSENT = io._old_process_absent


class RetirementIOTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.target = {'prefix': base / 'prefix', 'root': base / 'host'}
        self.target['prefix'].mkdir(mode=0o700)
        (self.target['root'] / 'state/rolling').mkdir(mode=0o700, parents=True)
        fixture = json.loads((Path(__file__).parent / 'fixtures/synthetic-retired-maintenance737.json').read_text())
        assert fixture['synthetic_only'] is True
        raw = json.dumps(fixture).replace(fixture['parent']['prefix'], str(self.target['prefix']))
        raw = raw.replace(fixture['parent']['host_root'], str(self.target['root']))
        fixture = json.loads(raw)
        self.parent = fixture['parent']
        self.terminal = fixture['terminal_job']
        self.terminal['before']['service_uid'] = self.parent['service_uid'] = os.geteuid()
        self.terminal['after'] = copy.deepcopy(self.parent)
        self.terminal['precommit_controller_failure_proof_sha256'] = fingerprint(
            self.terminal['precommit_controller_failure_proof'])
        self.terminal[drain.RECORD]['projection_sha256'] = fingerprint(
            self.terminal[drain.RECORD]['projection'])
        self.mailbox = fixture['mailbox']
        self.receipt = fixture['receipt']
        self.mailbox_path, self.receipt_path = io._pair_paths(self.target)
        self.mailbox_path.write_text(json.dumps(self.mailbox, indent=3) + '\n')
        self.receipt_path.write_text(json.dumps(self.receipt, indent=1) + '\n')
        self.receipt_path.chmod(0o600)
        self.original = (self.mailbox_path.read_bytes(), self.receipt_path.read_bytes())
        self.original_times = (self.mailbox_path.stat().st_mtime_ns, self.receipt_path.stat().st_mtime_ns)
        folder = self.target['prefix'] / 'installations'
        folder.mkdir()
        self.registration = folder / (self.parent['installation_id'] + '.json')
        self.registration.write_text(json.dumps(self.parent))
        self.pending = {'id': str(uuid.uuid4()), 'phase': 'waiting_empty', 'before': self.parent,
                        'release_approval_required': True, 'rooms': self.terminal['rooms'][:],
                        'configuration': {}, 'queued_at': max(time.time(), self.terminal['completed_at'] + 1),
                        'process': {'pid': self.terminal['process']['pid'] + 10000000},
                        history.REFERENCE: history.terminal_reference(self.terminal)}
        read, write = provisioning._control, provisioning._write
        def control(path, owner=0, **kw):
            return read(path, os.geteuid(), **kw)
        def save_file(path, value, **kw):
            return write(path, value, owner=os.geteuid(), **kw)
        raw_original = io._original
        for module in (history, io):
            self.enterContext(patch.object(module, 'protected', side_effect=lambda p: p))
            self.enterContext(patch.object(module, '_control', side_effect=control))
            self.enterContext(patch.object(module, '_write', side_effect=save_file))
        self.enterContext(patch.object(io, '_original',
            side_effect=lambda p, owner, expected, **kw: raw_original(p, os.geteuid(), expected, **kw)))
        self.enterContext(patch.object(drain, '_control', side_effect=control))
        self.enterContext(patch.object(drain, '_write', side_effect=save_file))
        self.process = self.enterContext(patch.object(drain, '_process_matches', return_value=True))
        self.collect = self.enterContext(patch.object(drain, '_collect',
            return_value=(['room1', 'room2'], {}, 'a' * 64, 'b' * 64, {})))
        self.absent = self.enterContext(patch.object(io, '_old_process_absent', return_value=True))
        self.native = self.enterContext(patch.object(io, 'committed_upgrade_witness',
            return_value={'format': 1, 'transition_id': self.terminal['upgrade_transition'],
                          'before_sha256': self.terminal['before']['bundle_sha256'],
                          'after_sha256': self.parent['bundle_sha256'],
                          'upgrade_sha256': 'a' * 64, 'activation_sha256': 'b' * 64}))
        history.archive_terminal(self.target, self.parent, self.terminal)

    def save(self, **value):
        self.pending.update(value)

    def ensure(self, save=None):
        return io.ensure_retired(self.target, self.parent, self.pending, save or self.save)

    def assert_untouched(self):
        self.assertEqual((self.mailbox_path.read_bytes(), self.receipt_path.read_bytes()), self.original)
        self.assertEqual((self.mailbox_path.stat().st_mtime_ns,
                          self.receipt_path.stat().st_mtime_ns), self.original_times)

    def test_decision_preserves_original_bytes_and_never_manufactures_closure(self):
        self.assertTrue(self.ensure())
        self.assert_untouched()
        path = io._path(self.target, self.pending)
        record = json.loads(path.read_text())
        self.assertEqual(bytes.fromhex(record['mailbox_hex']), self.original[0])
        self.assertEqual(bytes.fromhex(record['receipt_hex']), self.original[1])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(record['witness']['room_operations_authorized'])
        self.assertFalse(record['witness']['ownership_changes_authorized'])
        self.assertTrue(io.pair_matches(self.target, self.parent, self.pending, self.mailbox, self.receipt))
        paths = drain._maintenance_paths(self.target, self.parent, self.pending)
        self.assertEqual(paths[2:], (self.mailbox, self.receipt))
        baseline = drain._pre_gate_baseline(self.target, self.parent, self.pending, 'invented-nonce')
        self.assertIsNotNone(baseline)
        self.assertEqual(baseline['receipt_sha256'], fingerprint(self.receipt))

    def test_crash_after_decision_before_reference_reuses_same_immutable_record(self):
        def crash(**kw):
            raise RuntimeError('invented crash before job reference save')
        with self.assertRaisesRegex(RuntimeError, 'invented crash'):
            self.ensure(crash)
        path = io._path(self.target, self.pending)
        data, modified = path.read_bytes(), path.stat().st_mtime_ns
        self.assertNotIn(io.RETIREMENT, self.pending)
        self.assert_untouched()
        self.assertTrue(self.ensure())
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assert_untouched()

    def test_retry_after_reference_save_never_rewrites_pair_or_decision(self):
        self.ensure()
        path = io._path(self.target, self.pending)
        modified = path.stat().st_mtime_ns
        self.assertTrue(self.ensure())
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assert_untouched()

    def test_current_request_can_await_old_receipt_only_under_exact_gate_pin(self):
        self.ensure()
        self.pending['nonce'] = 'invented-nonce'
        baseline = drain._pre_gate_baseline(self.target, self.parent, self.pending, self.pending['nonce'])
        self.pending[drain.GATE_BASELINE] = baseline
        record = copy.deepcopy(self.terminal[drain.RECORD])
        record.update(job_id=self.pending['id'], parent_sha256=self.parent['bundle_sha256'],
                      request_id=drain._request_id(self.pending['id'], 0), round=0)
        self.pending[drain.RECORD] = record
        request = {k: record[k] for k in ('format', 'installation_id', 'generation', 'parent_sha256',
                                         'mode', 'rooms', 'created_at', 'expires_at')}
        request.update(id=record['request_id'], phase='requested')
        self.mailbox_path.write_text(json.dumps(request))
        self.assertTrue(io.pair_matches(self.target, self.parent, self.pending, request, self.receipt))
        self.assertTrue(drain._pre_gate_terminal_matches(self.target, self.parent, self.pending,
                                                       request, self.receipt))
        self.assertEqual(drain._maintenance_paths(self.target, self.parent, self.pending)[2:], (request, None))
        baseline['nonce'] = 'foreign-nonce'
        self.assertFalse(drain._pre_gate_terminal_matches(self.target, self.parent, self.pending,
                                                        request, self.receipt))
        with self.assertRaises((ConfigError, NetworkError)):
            drain._maintenance_paths(self.target, self.parent, self.pending)

    def test_changed_original_after_decision_is_not_accepted(self):
        self.ensure()
        changed = {**self.receipt, 'updated_at': self.receipt['updated_at'] + 1}
        self.receipt_path.write_text(json.dumps(changed))
        self.assertFalse(io.pair_matches(self.target, self.parent, self.pending, self.mailbox, changed))
        with self.assertRaises((ConfigError, NetworkError)):
            self.ensure()
        self.assertEqual(json.loads(self.receipt_path.read_text()), changed)

    def test_changed_archive_is_refused_without_overwriting_it(self):
        self.ensure()
        path = io._path(self.target, self.pending)
        value = json.loads(path.read_text())
        value['decision'] = 'invented'
        path.write_text(json.dumps(value))
        with self.assertRaises(ConfigError):
            self.ensure()
        self.assertEqual(json.loads(path.read_text())['decision'], 'invented')
        self.assert_untouched()

    def test_live_retired_process_or_unknown_current_process_never_prepares(self):
        self.absent.return_value = False
        with self.assertRaises(ConfigError):
            self.ensure()
        self.absent.return_value = True
        self.process.return_value = False
        with self.assertRaises(ConfigError):
            self.ensure()
        self.assertFalse(io._path(self.target, self.pending).exists())
        self.assert_untouched()

    def test_changed_registration_or_unclean_current_ownership_never_prepares(self):
        self.registration.write_text(json.dumps({**self.parent, 'bundle_sha256': '0' * 64}))
        with self.assertRaises(ConfigError):
            self.ensure()
        self.registration.write_text(json.dumps(self.parent))
        self.collect.side_effect = ConfigError('invented uncertain current ownership')
        with self.assertRaises(ConfigError):
            self.ensure()
        self.assertFalse(io._path(self.target, self.pending).exists())
        self.assert_untouched()

    def test_manual_request_or_started_commit_never_prepares(self):
        changed = {**self.mailbox, 'id': str(uuid.uuid4())}
        self.mailbox_path.write_text(json.dumps(changed))
        self.receipt_path.write_text(json.dumps({**self.receipt, 'id': changed['id']}))
        with self.assertRaises((ConfigError, NetworkError)):
            self.ensure()
        self.mailbox_path.write_bytes(self.original[0])
        self.receipt_path.write_bytes(self.original[1])
        self.pending['commit_started_at'] = time.time()
        with self.assertRaises(ConfigError):
            self.ensure()
        self.assertFalse(io._path(self.target, self.pending).exists())

    def test_missing_old_evidence_and_private_booking_guard_do_not_mutate(self):
        reference = self.pending.pop(history.REFERENCE)
        self.assertFalse(self.ensure())
        self.pending[history.REFERENCE] = reference
        self.collect.return_value = ([], {}, 'a' * 64, 'b' * 64, {})
        self.assertFalse(self.ensure())
        self.assertFalse(io._path(self.target, self.pending).exists())
        self.assert_untouched()

    def test_archive_directory_cannot_be_redirected_through_symlink(self):
        other = self.target['prefix'].parent / 'elsewhere'
        other.mkdir()
        (self.target['prefix'] / io.FOLDER).symlink_to(other, target_is_directory=True)
        with self.assertRaises(ConfigError):
            self.ensure()
        self.assertEqual(list(other.iterdir()), [])
        self.assert_untouched()

    def request(self, save=None):
        original = self.terminal[drain.RECORD]
        self.collect.return_value = (original['rooms'], copy.deepcopy(original['initial']),
            fingerprint(original['projection']), original['eligibility_sha256'],
            copy.deepcopy(original['projection']))
        # Real local leases and request persistence, invented provider/service
        # boundary. There is no worker transport or account in this test.
        with file_lease(self.target['prefix'] / 'deployment-admission.lock'), \
                file_lease(self.target['prefix'] / 'controller-update-admin.lock'), \
                patch.object(controller_update, '_gate', return_value=True):
            return drain.request(self.target, self.parent, self.pending, save or self.save)

    def test_normal_request_supersedes_pair_then_replays_until_real_new_ack(self):
        result = self.request()
        self.assertEqual(result['phase'], 'requested')
        current = json.loads(self.mailbox_path.read_text())
        self.assertNotEqual(current['id'], self.mailbox['id'])
        self.assertEqual(current['parent_sha256'], self.parent['bundle_sha256'])
        self.assertEqual(self.receipt_path.read_bytes(), self.original[1])
        modified = self.mailbox_path.stat().st_mtime_ns
        self.assertEqual(self.request(), result)
        self.assertEqual(self.mailbox_path.stat().st_mtime_ns, modified)
        actual = {'format': 1, 'id': current['id'], 'phase': 'draining',
                  'rooms': {alias: 'waiting_empty' for alias in current['rooms']},
                  'mode': 'empty', 'controller_pid': self.pending['process']['pid'],
                  'updated_at': time.time()}
        self.receipt_path.write_text(json.dumps(actual))
        self.assertEqual(self.request()['phase'], 'draining')
        self.assertEqual(json.loads(self.receipt_path.read_text()), actual)

    def test_crash_before_gate_baseline_save_replays_same_retirement(self):
        def crash(**value):
            if drain.GATE_BASELINE in value:
                raise RuntimeError('invented crash before gate baseline persistence')
            self.save(**value)
        with self.assertRaisesRegex(RuntimeError, 'invented crash'):
            self.request(crash)
        self.assert_untouched()
        path = io._path(self.target, self.pending)
        original = path.read_bytes()
        self.assertEqual(self.request()['phase'], 'requested')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.receipt_path.read_bytes(), self.original[1])

    def test_crash_after_new_mailbox_write_never_repeats_the_dispatch(self):
        real_write = drain._write
        def write_then_crash(path, value, **kw):
            real_write(path, value, **kw)
            raise RuntimeError('invented crash after new mailbox persistence')
        with patch.object(drain, '_write', side_effect=write_then_crash):
            with self.assertRaisesRegex(RuntimeError, 'invented crash'):
                self.request()
        current = self.mailbox_path.read_bytes()
        modified = self.mailbox_path.stat().st_mtime_ns
        with patch.object(drain, '_write', side_effect=AssertionError('must not dispatch again')):
            self.assertEqual(self.request()['phase'], 'requested')
        self.assertEqual(self.mailbox_path.read_bytes(), current)
        self.assertEqual(self.mailbox_path.stat().st_mtime_ns, modified)
        self.assertEqual(self.receipt_path.read_bytes(), self.original[1])

    def test_process_observation_refuses_actual_live_pid_and_invalid_identity(self):
        self.assertFalse(REAL_PROCESS_ABSENT(os.getpid()))
        for pid in (None, True, 0, 1, '123'):
            with self.subTest(pid=pid), self.assertRaises(ConfigError):
                REAL_PROCESS_ABSENT(pid)

    def test_oversized_archive_refuses_before_publishing_an_unreadable_decision(self):
        self.pending['invented_large_metadata'] = 'x' * 40000
        with patch.object(io, 'MAXIMUM', 32768), self.assertRaises(ConfigError):
            self.ensure()
        self.assertFalse(io._path(self.target, self.pending).exists())
        self.assert_untouched()


if __name__ == '__main__':
    unittest.main(verbosity=2)
