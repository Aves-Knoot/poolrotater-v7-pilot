"""A later update retains exact recovery evidence without granting authority."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import copy
from contextlib import ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from poolrotater_host import controller_history as history
from poolrotater_host.config import ConfigError


class TerminalHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.prefix = Path(temporary.name) / 'prefix'
        self.prefix.mkdir(mode=0o700)
        binding = dict(installation_id=str(uuid.uuid4()), generation=str(uuid.uuid4()),
                       prefix=str(self.prefix), host_root='/invented-host',
                       execution='pilot-live', service_uid=os.geteuid())
        self.before = {**binding, 'bundle_sha256': 'a' * 64}
        self.after = {**binding, 'bundle_sha256': 'b' * 64}
        self.target = {'prefix': self.prefix}
        self.job = dict(format=1, id=str(uuid.uuid4()), phase='complete',
                        before=self.before, after=self.after,
                        precommit_controller_failure_proof={'invented': True},
                        controller_update_drain={'original': 'retained'})
        read, write = history._control, history._write
        self.enterContext(patch.object(history, 'protected', side_effect=lambda p: p))
        self.enterContext(patch.object(history, '_control',
            side_effect=lambda p, **kw: read(p, os.geteuid(), **kw)))
        self.enterContext(patch.object(history, '_write',
            side_effect=lambda p, v, **kw: write(p, v, owner=os.geteuid(), **kw)))

    def archive(self, job=None, parent=None):
        return history.archive_terminal(self.target, parent or self.after,
                                        self.job if job is None else job)

    def test_full_fault_and_drain_evidence_survives_and_replay_does_not_rewrite(self):
        original = copy.deepcopy(self.job)
        path = self.archive()
        before = path.stat()
        self.assertEqual(json.loads(path.read_text())['job'], original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.archive(), path)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.job, original)
        self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_cancelled_records_bind_to_unchanged_predecessor(self):
        job = {**self.job, 'phase': 'cancelled'}
        path = self.archive(job, self.before)
        self.assertEqual(json.loads(path.read_text())['job'], job)
        # A later stopped upgrade may retain an older cancelled record. Keep
        # it as evidence without allowing recovery to adopt its old endpoint.
        self.assertEqual(self.archive(job, self.after), path)
        with self.assertRaises(ConfigError):
            history.read_terminal(self.target, self.after, history.terminal_reference(job))

    def test_active_unknown_or_foreign_records_are_not_archived(self):
        for change in ({'phase': 'held'}, {'phase': 'committing'}, {'format': True},
                       {'id': '../elsewhere'},
                       {'before': {**self.before, 'generation': str(uuid.uuid4())}}):
            with self.subTest(change=change), self.assertRaises(ConfigError):
                self.archive({**self.job, **change})
        self.assertFalse((self.prefix / 'controller-terminal-history').exists())

    def test_changed_existing_receipt_is_never_overwritten(self):
        path = self.archive()
        path.write_text('{}')
        with self.assertRaises(ConfigError):
            self.archive()
        self.assertEqual(path.read_text(), '{}')

    def test_no_previous_job_is_inert(self):
        self.assertIsNone(history.archive_terminal(self.target, self.after, None))
        self.assertEqual(list(self.prefix.iterdir()), [])

    def test_history_cannot_be_redirected_through_a_link(self):
        other = self.prefix.parent / 'elsewhere'
        other.mkdir()
        (self.prefix / 'controller-terminal-history').symlink_to(other, target_is_directory=True)
        with self.assertRaises(ConfigError):
            self.archive()
        self.assertEqual(list(other.iterdir()), [])

    def test_exact_reference_reads_original_without_rewriting_or_searching(self):
        path = self.archive()
        reference = history.terminal_reference(self.job)
        before = path.stat().st_mtime_ns
        unrelated = path.parent / 'unrelated.json'
        unrelated.write_text('not json and never read')
        self.assertEqual(history.read_terminal(self.target, self.after, reference), self.job)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(unrelated.read_text(), 'not json and never read')
        self.assertIsNone(history.terminal_reference(None))

    def test_reference_cannot_select_another_path_or_relax_the_binding(self):
        self.archive()
        reference = history.terminal_reference(self.job)
        for change in ({'format': True}, {'operation_id': '../other'},
                       {'job_sha256': '../other'}, {'job_sha256': 'F' * 64},
                       {'extra': True}):
            with self.subTest(change=change), self.assertRaises(ConfigError):
                history.read_terminal(self.target, self.after, {**reference, **change})
        with self.assertRaises(ConfigError):
            history.read_terminal(self.target, self.before, reference)
        with self.assertRaises(ConfigError):
            history.read_terminal(self.target, self.after,
                                  {**reference, 'job_sha256': '0' * 64})

    def test_self_consistent_foreign_or_active_archive_still_refuses(self):
        path = self.archive()
        value = json.loads(path.read_text())
        for change in ({'phase': 'held'},
                       {'after': {**self.after, 'generation': str(uuid.uuid4())}}):
            job = {**value['job'], **change}
            reference = history.terminal_reference(job)
            other = path.parent / (job['id'] + '-' + reference['job_sha256'] + '.json')
            other.write_text(json.dumps({'format': 1, 'job_sha256': reference['job_sha256'],
                                         'job': job}))
            other.chmod(0o600)
            with self.subTest(change=change), self.assertRaises(ConfigError):
                history.read_terminal(self.target, self.after, reference)

    def test_missing_or_modified_evidence_is_not_recreated_by_read(self):
        path = self.archive()
        reference = history.terminal_reference(self.job)
        path.write_text('{}')
        with self.assertRaises(ConfigError):
            history.read_terminal(self.target, self.after, reference)
        path.unlink()
        with self.assertRaises(ConfigError):
            history.read_terminal(self.target, self.after, reference)
        self.assertFalse(path.exists())

    def test_older_completed_job_survives_stopped_upgrade_without_becoming_recovery_authority(self):
        older = {**self.job, 'after': {**self.after, 'bundle_sha256': 'c' * 64}}
        path = self.archive(older)
        self.assertEqual(json.loads(path.read_text())['job'], older)
        self.assertEqual(self.archive(older), path)
        with self.assertRaises(ConfigError):
            history.read_terminal(self.target, self.after, history.terminal_reference(older))

    def exercise_queue(self, *, current_evidence=True):
        from poolrotater_host import controller_update as update
        root = self.prefix.parent / 'host'
        (root / 'config').mkdir(parents=True)
        target = {**self.target, 'root': root}
        marker = {'installation_id': self.after['installation_id']}
        current_path = self.prefix / update.NAME
        history._write(current_path, self.job)
        bundle = self.prefix.parent / 'candidate.zip'
        bundle.write_bytes(b'invented archive; runtime checks are separate installed tests')
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        candidate = {**self.after, 'bundle_sha256': digest}
        original_control = history._control

        def read(path, *args, **kwargs):
            if Path(path).name == 'status.json':
                return {'rooms': {'room1': {}}}
            if 'candidates' in Path(path).parts:
                return candidate
            return original_control(path, **kwargs)

        def provision(*args, **kwargs):
            # The previous job still exists, and its durable evidence is already
            # available even if provisioning or a later admission check fails.
            self.assertEqual(original_control(current_path), self.job)
            reference = history.terminal_reference(self.job)
            if current_evidence:
                self.assertEqual(history.read_terminal(target, self.after, reference), self.job)
            else:
                with self.assertRaises(ConfigError):
                    history.read_terminal(target, self.after, reference)
            return {'release': '/invented-candidate'}

        with ExitStack() as stack:
            patches = [
                patch('poolrotater_host.pilot_installation.administrative_binding',
                      return_value=(target, marker, self.after)),
                patch('poolrotater_host.pilot_operations._service_state',
                      return_value={'MainPID': 2000, 'ActiveState': 'active',
                                    'UnitFileState': 'disabled'}),
                patch('poolrotater_host.network.operations._state',
                      return_value={'ActiveState': 'active', 'UnitFileState': 'enabled'}),
                patch.object(update, 'file_lease', return_value=nullcontext()),
                patch.object(update, '_control', side_effect=read),
                patch.object(update, '_write', side_effect=history._write),
                patch.object(update, '_provision', side_effect=provision),
                patch.object(update, 'validate_runtime', return_value={'manifest': {}}),
                patch.object(update, '_process', return_value={'pid': 2000}),
                patch('poolrotater_host.updates.approval.require_upgrade'),
                patch('poolrotater_host.updates.native.require_idle_worker_deployment'),
                patch('poolrotater_host.updates.native.require_registered_workers'),
                patch('poolrotater_host.ratings.operations._identity', return_value=nullcontext()),
                patch('poolrotater_host.layout.load',
                      return_value=(marker, {'rooms': [{'alias': 'room1'}]})),
                patch('poolrotater_host.managed_host.projection.read', return_value=None),
            ]
            for item in patches:
                stack.enter_context(item)
            result = update.queue('invented', bundle, digest,
                                  from_sha256=self.after['bundle_sha256'])
        queued = original_control(current_path)
        self.assertEqual(result['phase'], 'waiting_empty')
        self.assertNotEqual(queued['id'], self.job['id'])
        self.assertEqual(queued[history.REFERENCE], history.terminal_reference(self.job))
        if current_evidence:
            self.assertEqual(history.read_terminal(target, self.after, queued[history.REFERENCE]), self.job)
        else:
            with self.assertRaises(ConfigError):
                history.read_terminal(target, self.after, queued[history.REFERENCE])

    def test_queue_keeps_original_proof_before_provisioning_and_addresses_it_in_new_job(self):
        self.exercise_queue()

    def test_queue_after_stopped_upgrade_keeps_older_terminal_without_refusing_update(self):
        self.job['after'] = {**self.after, 'bundle_sha256': 'c' * 64}
        self.exercise_queue(current_evidence=False)


if __name__ == '__main__':
    unittest.main()
