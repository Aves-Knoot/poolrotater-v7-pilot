"""Actual child startup/IPC failure and pre-reservation contention regression."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from poolrotater_host import process_startup
from poolrotater_host.layout import LeaseBusy, file_lease, read_json, setup
from poolrotater_host.oauth import AuthError
from poolrotater_host.provider_pacing import PersistentRequestGate, ProviderAdmissionDeferred, STATE_PATH


def echoes(pipe, payload):
    pipe.send_bytes(payload)


def never_reads(pipe, target, parent_pid):
    time.sleep(30)


def exits_before_arguments(pipe, target, parent_pid):
    pipe.close()


class StartupTests(unittest.TestCase):
    def test_large_private_arguments_transfer_after_start_and_child_is_reaped(self):
        raw = b'invented-private-material-' * 12000
        process, target, pipe = process_startup.start(echoes, (raw,), time.monotonic()+3)
        try:
            self.assertTrue(pipe.poll(2))
            self.assertEqual(pipe.recv_bytes(), raw)
        finally:
            pipe.close(); target.kill(); process.join(1); target.close(); process.close()

    def test_failed_child_before_large_transfer_does_not_hold_caller(self):
        for bootstrap in (never_reads, exits_before_arguments):
            before = {c.pid for c in multiprocessing.active_children()}
            started = time.monotonic()
            with patch.object(process_startup, '_bootstrap', bootstrap):
                with self.assertRaises((AuthError, OSError)):
                    process_startup.start(echoes, (b'x'*1000000,), started+.2)
            self.assertLess(time.monotonic()-started, 1.3)
            self.assertEqual({c.pid for c in multiprocessing.active_children()}, before)

    def test_inaccessible_or_oversized_preparation_refused_before_process(self):
        with patch.object(process_startup.os, 'access', return_value=False), patch('multiprocessing.process.BaseProcess.start') as start:
            with self.assertRaises(AuthError) as error: process_startup.preflight()
            self.assertEqual(error.exception.code, 'provider_child_setup_refused')
            start.assert_not_called()
        with patch.object(process_startup.spawn, 'get_preparation_data', return_value={'authkey':b'x', 'path':'x'*5000}):
            with self.assertRaises(AuthError): process_startup.preflight()

    def test_pidfd_acquisition_failure_reaps_bootstrap_without_transferring_arguments(self):
        before={c.pid for c in multiprocessing.active_children()}
        with patch.object(process_startup,'OwnedChild',side_effect=AuthError('process_target_refused')):
            with self.assertRaisesRegex(AuthError,'process_target_refused'):
                process_startup.start(echoes,(b'invented-private-arguments',),time.monotonic()+3)
        self.assertEqual({c.pid for c in multiprocessing.active_children()},before)


class ContentionTests(unittest.TestCase):
    def test_only_validated_lock_contention_is_typed_and_ledger_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'host';setup(root);gate=PersistentRequestGate(root,123)
            before=read_json(root/STATE_PATH)
            with file_lease(root/gate.lock_path):
                with self.assertRaises(ProviderAdmissionDeferred) as error:
                    with gate.admission(time.monotonic()+.08): self.fail('not admitted')
            self.assertEqual(error.exception.code,'provider_pacing_deferred')
            self.assertEqual(read_json(root/STATE_PATH),before)
            with gate.admission(time.monotonic()+1): pass

    def test_invalid_lock_permissions_are_fatal_not_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'host';setup(root);gate=PersistentRequestGate(root,123)
            (root/gate.lock_path).chmod(0o644)
            with self.assertRaises(AuthError) as error:
                with gate.admission(time.monotonic()+.08): self.fail('not admitted')
            self.assertNotIsInstance(error.exception,ProviderAdmissionDeferred)
            self.assertEqual(error.exception.code,'provider_pacing_busy')
