"""Remote recovery may start only an exited service with positive closed proof."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import nullcontext
from poolrotater_host.network.contracts import NetworkError, fingerprint
from poolrotater_host.rescue.operations import Backend
from poolrotater_host.provisioning import _control
from poolrotater_host.layout import setup


class FailedServiceRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.prefix=Path(temporary.name);self.root=self.prefix/'host';setup(self.root,['standard-auto'])
        (self.prefix/'recovery').mkdir()
        self.target={'prefix':self.prefix,'root':self.root}
        self.record={'service_uid':1000,'bundle_sha256':'a'*64,'release':str(self.prefix/'release')}
        self.journal={'format':1,'rooms':{'room1':{'phase':'stopped','operation':'closed'}}}
        self.enterContext(patch('poolrotater_host.rescue.operations.target_for',return_value=self.target))
        self.backend=Backend('invented',self.record)
        self.enterContext(patch.object(self.backend,'failed_service_evidence',return_value=(self.record,self.journal)))
        self.clean=self.enterContext(patch.object(self.backend,'require_clean_closed'))
        self.state={'ActiveState':'failed','MainPID':0}
        self.enterContext(patch('poolrotater_host.pilot_operations._service_state',side_effect=lambda _:dict(self.state)))
        self.enterContext(patch('poolrotater_host.network.operations._state',return_value={'MainPID':0}))
        self.network=self.enterContext(patch('poolrotater_host.network.operations.unit'))
        self.enterContext(patch('poolrotater_host.ratings.operations._identity',side_effect=lambda _:nullcontext()))
        self.wait=self.enterContext(patch('poolrotater_host.update_lifecycle.wait_stopped'))
        self.validation=self.enterContext(patch('poolrotater_host.runtime_lifecycle.validate_runtime',
            return_value={'release':Path(self.record['release']), 'manifest':{'files':{}}}))
        self.run=self.enterContext(patch('subprocess.run',return_value=SimpleNamespace(returncode=0)))
        self.enterContext(patch('poolrotater_host.rescue.operations._control',side_effect=lambda path,*a,**kw:
            _control(path,os.geteuid(),**kw)))
        self.command={'action':'start-failed-service','operation_id':None,'candidate_sha256':'a'*64,
            'job_sha256':fingerprint({'registration':self.record,'journal':self.journal})}

    def test_failed_clean_service_uses_checked_current_admin_entry_once(self):
        self.backend.start_failed_service(self.command)
        self.clean.assert_called_once_with({'rooms':['room1']})
        self.wait.assert_called_once_with(self.root,1000)
        self.assertEqual(self.run.call_args.args[0][-3:],['--instance','invented','start'])
        self.assertIn(str(self.prefix/'release/poolrotater_host/pilot_admin_entry.py'),self.run.call_args.args[0])

    def test_active_inactive_or_deactivating_service_is_never_started(self):
        for state in ({'ActiveState':'active','MainPID':42},{'ActiveState':'inactive','MainPID':0},
                      {'ActiveState':'deactivating','MainPID':42}):
            self.state=state
            with self.subTest(state=state),self.assertRaises(NetworkError):
                self.backend.start_failed_service(self.command)
        self.run.assert_not_called();self.network.assert_not_called()

    def test_changed_ownership_proof_or_installed_release_is_refused(self):
        for change in ({'candidate_sha256':'b'*64},{'job_sha256':'c'*64},{'operation_id':'unexpected'}):
            with self.subTest(change=change),self.assertRaises(NetworkError):
                self.backend.start_failed_service({**self.command,**change})
        self.run.assert_not_called()

    def test_uncertain_room_or_live_process_refuses_start(self):
        self.clean.side_effect=NetworkError('rescue_ownership_requires_review')
        with self.assertRaises(NetworkError):self.backend.start_failed_service(self.command)
        self.run.assert_not_called();self.network.assert_not_called()

    def test_local_network_agent_is_restored_after_failed_start(self):
        with patch('poolrotater_host.network.operations._state',return_value={'MainPID':12}):
            self.run.return_value=SimpleNamespace(returncode=2)
            with self.assertRaises(NetworkError):self.backend.start_failed_service(self.command)
        self.assertEqual([c.args for c in self.network.call_args_list],
                         [('invented','stop'),('invented','start')])

    def test_preflight_failure_does_not_stop_network(self):
        self.validation.side_effect=RuntimeError('invented changed code')
        with self.assertRaises(RuntimeError):self.backend.start_failed_service(self.command)
        self.network.assert_not_called();self.run.assert_not_called()

    def test_stopped_guard_failure_restores_network_and_never_starts_rooms(self):
        with patch('poolrotater_host.network.operations._state',return_value={'MainPID':12}):
            self.wait.side_effect=RuntimeError('invented remaining process')
            with self.assertRaises(RuntimeError):self.backend.start_failed_service(self.command)
        self.run.assert_not_called()
        self.assertEqual([c.args for c in self.network.call_args_list],
                         [('invented','stop'),('invented','start')])

    def test_new_service_failure_does_not_reuse_a_completed_updates_failure_label(self):
        with patch.object(self.backend,'job',return_value={'id':'00000000-0000-0000-0000-000000000001',
                'phase':'complete','after':{'bundle_sha256':'a'*64},'failure_stage':'cleanup',
                'failure_code':'update_cleanup_timeout','startup_attempted_at':1000}):
            value=self.backend.observe()
        self.assertEqual(value['phase'],'service_failed')
        self.assertIsNone(value['operation_id']);self.assertIsNone(value['failure_code'])
        self.assertIsNone(value['failure_stage']);self.assertFalse(value['startup_attempted'])


if __name__=='__main__':unittest.main()
