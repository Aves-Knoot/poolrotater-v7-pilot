"""The actual predecessor Backend.advance must use checked staged code."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from poolrotater_host.updates.native import Backend
from poolrotater_host.updates import executor
from poolrotater_host.config import ConfigError


class SuccessorDispatchTests(unittest.TestCase):
    def setUp(self):
        self.operation = {'id': str(uuid.uuid4()), 'release': {'kind': 'controller', 'artifact': {'sha256': 'c'*64}}}
        self.job = {'id': self.operation['id'], 'after': {'bundle_sha256':'c'*64}}
        self.target = {'prefix': Path('/invented/prefix')}
        self.backend = Backend('invented', None)
        self.enterContext(patch.object(self.backend,'binding',return_value=(self.target,{},{})))
        self.enterContext(patch('poolrotater_host.updates.native._control',return_value=self.job))
        self.validation = self.enterContext(patch.object(executor,'validate_runtime',
            return_value={'release':Path('/invented/prefix/releases/successor')}))
        self.run = self.enterContext(patch.object(executor.subprocess,'run',return_value=SimpleNamespace(returncode=0)))
        self.old = self.enterContext(patch('poolrotater_host.controller_update.step',side_effect=AssertionError('old code called')))

    def test_predecessor_advance_executes_staged_successor_with_isolated_system_python(self):
        self.backend.advance(self.operation)
        self.old.assert_not_called()
        self.validation.assert_called_once_with(self.job['after'],self.target['prefix'])
        self.assertEqual(self.run.call_args.args[0], ['/usr/bin/python3.12','-I','-S','-B',
            '/invented/prefix/releases/successor/poolrotater_host/controller_update_entry.py','invented'])
        self.assertNotIn('HOME',self.run.call_args.kwargs['env'])
        self.assertLessEqual(self.run.call_args.kwargs['timeout'],1200)

    def test_candidate_validation_failure_never_executes_any_release(self):
        self.validation.side_effect=ConfigError('changed source')
        with self.assertRaises(ConfigError):self.backend.advance(self.operation)
        self.run.assert_not_called();self.old.assert_not_called()

    def test_foreign_job_never_dispatches_candidate(self):
        self.job['id']=str(uuid.uuid4())
        with self.assertRaises(ConfigError):self.backend.advance(self.operation)
        self.validation.assert_not_called();self.run.assert_not_called()

    def test_nonzero_child_is_a_closed_error_without_response_text(self):
        self.run.return_value=SimpleNamespace(returncode=2)
        with self.assertRaisesRegex(ConfigError,'^update_installation_requires_review$'):
            self.backend.advance(self.operation)

    def test_recovery_uses_same_checked_entry_and_bound_operation(self):
        executor.controller('invented',self.target,self.job,decision='complete')
        self.assertEqual(self.run.call_args.args[0][-4:],
            ['--operation-id',self.job['id'],'--decision','complete'])
        with self.assertRaises(ConfigError):
            executor.controller('invented',self.target,self.job,decision='arbitrary')


if __name__=='__main__':unittest.main()
