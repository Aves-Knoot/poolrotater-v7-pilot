"""Stopped root recovery excludes reconciliation and explicit network starts."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from poolrotater_host.config import ConfigError
from poolrotater_host.layout import file_lease,LeaseBusy
from poolrotater_host.stopped_recovery import admission
from poolrotater_host import pilot_operations as pilot


class Coordination(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.prefix=Path(temp.name)
        self.binding=({'prefix':self.prefix,'root':self.prefix/'runtime'}, {},
                      {'execution':'pilot-live','service_uid':12345})
        self.bound=patch('poolrotater_host.stopped_recovery.administrative_binding',return_value=self.binding)
        self.bound.start();self.addCleanup(self.bound.stop)
    def free(self):
        for path in ('deployment-admission.lock','network-admin.lock'):
            with file_lease(self.prefix/path):pass
    def test_both_helper_start_routes_excluded_until_exit(self):
        with admission('invented') as (target,record):
            self.assertEqual((target,record),(self.binding[0],self.binding[2]))
            for path in ('deployment-admission.lock','network-admin.lock'):
                with self.subTest(path=path),self.assertRaises(LeaseBusy):
                    with file_lease(self.prefix/path):pass
        self.free()
    def test_busy_reconcile_refuses_before_yield_without_mutating_any_state(self):
        with file_lease(self.prefix/'deployment-admission.lock'):
            with self.assertRaises(LeaseBusy):
                with admission('invented'):self.fail('must not enter recovery')
            with file_lease(self.prefix/'network-admin.lock'):pass
        self.free()
    def test_explicit_network_operation_busy_releases_deployment(self):
        with file_lease(self.prefix/'network-admin.lock'):
            with self.assertRaises(LeaseBusy):
                with admission('invented'):self.fail('must not enter recovery')
            with file_lease(self.prefix/'deployment-admission.lock'):pass
        self.free()
    def test_registration_change_after_lock_refuses(self):
        changed=(*self.binding[:2],{**self.binding[2],'bundle_sha256':'changed'})
        with patch('poolrotater_host.stopped_recovery.administrative_binding',side_effect=[self.binding,changed]):
            with self.assertRaisesRegex(ConfigError,'registration changed'):
                with admission('invented'):self.fail('old runtime must not execute')
        self.free()
    def test_exception_releases_all_admission_leases(self):
        with self.assertRaisesRegex(RuntimeError,'injected'):
            with admission('invented'):raise RuntimeError('injected')
        self.free()
    def test_ownership_child_retains_coordination_without_root_owner_lease(self):
        state={'MainPID':0,'ActiveState':'failed','UnitFileState':'disabled'}
        def service(target,record,arguments,timeout):
            self.assertEqual(arguments,['--ownership-recovery','review'])
            self.assertEqual(timeout,120)
            for path in ('deployment-admission.lock','network-admin.lock'):
                with self.assertRaises(LeaseBusy):
                    with file_lease(self.prefix/path):pass
            return {'read_only':True}
        with patch.object(pilot,'_unit',return_value=object()), \
             patch.object(pilot,'_service_state',return_value=state), \
             patch('poolrotater_host.pilot_installation.require_no_service_processes') as quiet, \
             patch.object(pilot,'_service_command',side_effect=service):
            self.assertEqual(pilot.ownership('invented','review'),{'read_only':True})
            quiet.assert_called_once_with(12345)
        self.free()
    def test_running_agent_or_bot_still_refused_never_stopped_implicitly(self):
        state={'MainPID':0,'ActiveState':'failed','UnitFileState':'disabled'}
        with patch.object(pilot,'_unit',return_value=object()), \
             patch.object(pilot,'_service_state',return_value=state), \
             patch('poolrotater_host.pilot_installation.require_no_service_processes',side_effect=ConfigError('dedicated processes')), \
             patch.object(pilot,'_service_command') as service:
            with self.assertRaisesRegex(ConfigError,'dedicated processes'):pilot.ownership('invented','review')
            service.assert_not_called()
        self.free()

    def test_inventory_uses_same_stopped_admission_with_bounded_longer_timeout(self):
        state={'MainPID':0,'ActiveState':'failed','UnitFileState':'disabled'}
        with patch.object(pilot,'_unit',return_value=object()), \
             patch.object(pilot,'_service_state',return_value=state), \
             patch('poolrotater_host.pilot_installation.require_no_service_processes'), \
             patch.object(pilot,'_service_command',return_value={'history_complete':True}) as service:
            self.assertEqual(pilot.ownership('invented','inventory'),{'history_complete':True})
            service.assert_called_once_with(self.binding[0],self.binding[2],
                ['--ownership-recovery','inventory'],timeout=360)
        self.free()

    def test_inventory_cli_has_no_mutation_or_candidate_arguments(self):
        import io
        from poolrotater_host import cli
        with patch.object(pilot,'ownership',return_value={'history_complete':True}) as operation, \
             patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(cli.main(['pilot-ownership-inventory','--instance','first']),0)
        operation.assert_called_once_with('first','inventory',journal_sha256=None,
            alias=None,room_id=None,confirm_lost_creation=False,transaction_id=None,decision=None)
        with patch.object(pilot,'ownership') as operation, \
             patch('sys.stderr',new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                cli.main(['pilot-ownership-inventory','--instance','first','--created-room-id','101'])
        operation.assert_not_called()
