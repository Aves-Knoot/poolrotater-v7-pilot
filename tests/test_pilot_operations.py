"""Operator command boundaries. Privileged installation runs separately on donor."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from contextlib import contextmanager
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import patch

from poolrotater_host import cli
from poolrotater_host import pilot_operations as operations
from poolrotater_host.config import ConfigError
from poolrotater_host.oauth import AuthError
from poolrotater_host.pilot_installation import paths


class OwnershipCommandTests(unittest.TestCase):
    def ownership_target(self,instance):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        target=paths(instance);target['prefix']=Path(temporary.name)
        return target
    def test_fresh_review_before_unit_installation_is_allowed_only_for_unloaded_stopped_unit(self):
        target=self.ownership_target('two');record={'execution':'pilot-live','service_uid':2001}
        with patch('poolrotater_host.stopped_recovery.administrative_binding',return_value=(target,{},record)), \
                patch.object(operations,'_unit',return_value=None) as unit, \
                patch.object(operations,'_service_state',return_value={
                    'MainPID':0,'LoadState':'not-found','ActiveState':'inactive','UnitFileState':''}) as state, \
                patch('poolrotater_host.pilot_installation.require_no_service_processes') as processes, \
                patch.object(operations,'_service_command',return_value={'rooms':[]}) as helper:
            self.assertEqual(operations.ownership('two','review'),{'rooms':[]})
            unit.assert_called_once_with('two',optional=True)
            processes.assert_called_once_with(2001)
            helper.assert_called_once_with(target,record,['--ownership-recovery','review'],timeout=120)
            helper.reset_mock()
            state.return_value['LoadState']='loaded'
            with self.assertRaises(ConfigError):operations.ownership('two','review')
            helper.assert_not_called()

    def test_reserved_owner_preserves_existing_service_resources_without_changing_tester_or_qa(self):
        owner=operations.unit_text('owner')
        for field in ('Nice=0\n','MemoryMax=infinity\n','MemorySwapMax=infinity\n','TasksMax=256\n'):
            self.assertIn(field,owner)
        for text in (operations.unit_text('first'),operations._unit_text('owner',qualification=True)):
            for field in ('Nice=15\n','MemoryMax=1G\n','MemorySwapMax=512M\n','TasksMax=128\n'):
                self.assertIn(field,text)
        self.assertIn('Restart=no\n',owner)
        self.assertIn('SendSIGKILL=yes\n',owner)
        self.assertIn('User=prv7-owner\n',owner)

    def test_code_proof_reads_no_authorization_or_service_and_rechecks_registration(self):
        target=paths('first')
        record={'bundle_sha256':'a'*64,'environment_id':'b'*64,'execution':'pilot-live'}
        runtime={'manifest':{'version':'v7.0.1','files':{'bot/bot.py':'c'*64}}}
        with patch.object(operations,'administrative_binding',return_value=(target,{},record)) as binding, \
                patch('poolrotater_host.runtime_lifecycle.validate_runtime',return_value=runtime), \
                patch.object(operations,'_service_command') as command, \
                patch.object(operations,'_service_state') as state:
            result=operations.code_proof('first')
        self.assertTrue(result['protected_runtime_checked'])
        self.assertFalse(result['operational_acceptance_verified'] or result['provider_dispatch'])
        self.assertEqual(result['source_files'],runtime['manifest']['files'])
        self.assertEqual(binding.call_count,2)
        command.assert_not_called();state.assert_not_called()

    def test_code_proof_refuses_changed_or_unverified_runtime(self):
        target=paths('first');record={'bundle_sha256':'a'*64,'environment_id':'b'*64,'execution':'pilot-live'}
        with patch.object(operations,'administrative_binding',side_effect=[(target,{},record),(target,{},dict(record,bundle_sha256='b'*64))]), \
                patch('poolrotater_host.runtime_lifecycle.validate_runtime',return_value={'manifest':{'version':'v7.0.1','files':{}}}):
            with self.assertRaisesRegex(ConfigError,'runtime changed'):operations.code_proof('first')
        with patch.object(operations,'administrative_binding',return_value=(target,{},record)), \
                patch('poolrotater_host.runtime_lifecycle.validate_runtime',side_effect=ConfigError('code changed')):
            with self.assertRaisesRegex(ConfigError,'code changed'):operations.code_proof('first')

    def test_startup_failure_keeps_safe_http_status_after_service_has_exited(self):
        target = paths('first')
        observation = {'phase':'failed','failure_code':'provider_api_unavailable',
                       'failure_http_status':403,'private_body':'must not appear'}
        from poolrotater_host.config import defaults
        with patch.object(operations, '_control', return_value=defaults(['standard-auto'])), \
                patch.object(operations, '_service_state', return_value={'MainPID':0,'ActiveState':'failed'}), \
                patch.object(operations, '_service_observation', side_effect=[None, observation]):
            with self.assertRaisesRegex(ConfigError, 'provider_api_unavailable \\(HTTP 403\\)') as caught:
                operations._wait_ready(target, {'service_uid':991}, timeout=1)
        self.assertNotIn('must not appear', str(caught.exception))

    def test_failed_service_with_closed_journal_reports_failure_and_clean_ownership_separately(self):
        target=paths('first');record={'execution':'pilot-live','service_uid':2001,
            'release':str(target['prefix']/'releases'/('v7.1.5-'+'a'*16)),
            'prefix':str(target['prefix']),'bundle_sha256':'a'*64}
        historical={'phase':'failed','failure_code':'provider_api_unavailable','failure_http_status':403}
        with patch.object(operations,'administrative_binding',return_value=(target,{},record)), \
                patch.object(operations,'check',return_value={'ok':True,'execution':'pilot-live'}), \
                patch.object(operations,'_unit',return_value=object()), \
                patch.object(operations,'_service_state',return_value={'MainPID':0,'ActiveState':'failed','UnitFileState':'disabled'}), \
                patch.object(operations,'_service_observation',side_effect=[None,historical]), \
                patch.object(operations,'_service_command',return_value={'room_phase':'stopped',
                    'ownership_recovery_phase':'committed','ownership_status_source':'journal_and_receipt'}), \
                patch('os.path.lexists',return_value=False):
            result=operations.status('first')
        self.assertEqual(result['room_phase'],'stopped')
        self.assertEqual(result['last_service_phase'],'failed')
        self.assertEqual(result['last_service_failure_code'],'provider_api_unavailable')
        self.assertEqual(result['last_service_failure_http_status'],403)
        self.assertNotIn('last_service_room_phase',result)

    def test_dead_observation_is_historical_and_never_current(self):
        target=paths('first')
        record={'service_uid':2001,'installation_id':'invented','generation':1}
        observation={'format':1,'execution':'provider-live-pilot','uid':2001,
                     'installation_id':'invented','generation':1,'pid':7661,'phase':'recovery_required'}
        with patch('os.path.lexists',return_value=True), \
                patch('poolrotater_host.provisioning._control',return_value=observation):
            self.assertIsNone(operations._service_observation(target,record,{'MainPID':0}))
            self.assertIsNone(operations._service_observation(target,record,{'MainPID':8000}))
            self.assertEqual(operations._service_observation(target,record,{'MainPID':7661}),observation)
            self.assertEqual(operations._service_observation(target,record,{'MainPID':0},historical=True),observation)

    def test_stopped_status_uses_authoritative_local_status_and_keeps_history_separate(self):
        target=paths('first');record={'execution':'pilot-live','service_uid':2001,
            'release':str(target['prefix']/'releases'/('v7.1.5-'+'a'*16)),
            'prefix':str(target['prefix']),'bundle_sha256':'a'*64}
        with patch.object(operations,'administrative_binding',return_value=(target,{},record)), \
                patch.object(operations,'check',return_value={'ok':True,'execution':'pilot-live'}), \
                patch.object(operations,'_unit',return_value=object()), \
                patch.object(operations,'_service_state',return_value={'MainPID':0,'ActiveState':'failed','UnitFileState':'disabled'}), \
                patch.object(operations,'_service_observation',side_effect=[None,{'phase':'recovery_required'}]), \
                patch.object(operations,'_service_command',return_value={'room_phase':'stopped',
                    'ownership_recovery_phase':'committed','ownership_status_source':'journal_and_receipt'}) as helper, \
                patch('os.path.lexists',return_value=False):
            result=operations.status('first')
            self.assertEqual(result['room_phase'],'stopped')
            self.assertEqual(result['last_service_room_phase'],'recovery_required')
            self.assertFalse(result['service_observation_current'])
            helper.assert_called_once_with(target,record,['--ownership-local-status'])

    def test_clear_cli_forwards_explicit_association_and_journal_digest(self):
        with patch.object(operations,'ownership',return_value={'ok':True}) as action, \
                patch('sys.stdout',new_callable=io.StringIO):
            result=cli.main(['pilot-ownership-clear','--instance','first','--journal-sha256','a'*64,
                '--room-alias','room1','--created-room-id','101','--confirm-lost-creation'])
        self.assertEqual(result,0)
        action.assert_called_once_with('first','clear',journal_sha256='a'*64,alias='room1',room_id=101,
                                      confirm_lost_creation=True,transaction_id=None,decision=None)

    def test_stopped_guard_precedes_service_helper_and_only_known_arguments_are_forwarded(self):
        target=self.ownership_target('first');record={'execution':'pilot-live','service_uid':2001,
            'release':str(target['prefix']/'releases'/('v7.1.5-'+'a'*16)),
            'prefix':str(target['prefix']),'bundle_sha256':'a'*64}
        with patch('poolrotater_host.stopped_recovery.administrative_binding',return_value=(target,{},record)), \
                patch.object(operations,'_unit'), \
                patch.object(operations,'_service_state',return_value={'MainPID':1,'ActiveState':'active','UnitFileState':'disabled'}) as state, \
                patch('poolrotater_host.pilot_installation.require_no_service_processes') as processes, \
                patch.object(operations,'_service_command',return_value={'ok':True}) as helper:
            with self.assertRaises(ConfigError):operations.ownership('first','review')
            helper.assert_not_called();processes.assert_not_called()
            state.return_value={'MainPID':0,'ActiveState':'failed','UnitFileState':'disabled'}
            operations.ownership('first','clear',journal_sha256='a'*64,alias='room1',room_id=101,confirm_lost_creation=True)
            processes.assert_called_once_with(2001)
            helper.assert_called_once_with(target,record,['--ownership-recovery','clear','--journal-sha256','a'*64,
                '--room-alias','room1','--created-room-id','101','--confirm-lost-creation'],timeout=120)


@contextmanager
def stopped(*_): yield


class PilotOperationsTests(unittest.TestCase):
    def setUp(self):
        self.target = paths('alice'); self.record = {'service_uid': 991, 'execution': 'pilot-candidate'}
        self.binding = self.enterContext(patch.object(operations, 'administrative_binding',
            return_value=(self.target, {}, self.record)))
        self.child = self.enterContext(patch.object(operations, '_service_command',
            return_value={'ok': True, 'version': 'v7.0.0'}))

    def test_unit_has_only_fixed_identity_paths_and_bounded_graceful_shutdown(self):
        text = operations.unit_text('alice')
        for value in ('User=prv7-alice', 'Nice=15', 'NoNewPrivileges=yes', 'ProtectSystem=strict',
                      'ReadWritePaths=/var/lib/poolrotater-v7-pilot/alice', 'SendSIGKILL=yes',
                      'Restart=no', 'KillSignal=SIGTERM', 'TimeoutStopSec=90', 'MemoryMax=1G',
                      '/usr/bin/python3.12 -I -S /opt/poolrotater-v7-pilot/alice/bootstrap.py'):
            self.assertIn(value, text)
        self.assertNotIn('POOLROTATER_OFFLINE_SANDBOX', text)
        self.assertNotIn('POOLROTATER_CLIENT_SECRET', text)
        self.assertNotIn('poolrotator.service', text)

    def test_check_discards_service_uid_and_arbitrary_child_details(self):
        self.child.return_value.update(secret='invented-secret', uid=991, root='/private')
        result = operations.check('alice')
        self.assertTrue(result['ok']); self.assertFalse(result['live_ready'])
        for value in ('invented-secret', '/private', '991'): self.assertNotIn(value, json.dumps(result))
        self.child.assert_called_once_with(self.target, self.record, ['--pilot-check'])

    def test_configure_checks_service_uid_integrity_before_private_input(self):
        class Unreadable:
            def read(self, *_): raise AssertionError('private input read before guard')
        self.child.side_effect = ConfigError('invented changed protected environment')
        with self.assertRaises(ConfigError): operations.operator('alice', 'configure',
            credentials_stream=Unreadable(), expected_user_id=99,
            redirect_uri='http://127.0.0.1:51111/oauth/callback')

    def test_configure_keeps_secret_out_of_child_arguments(self):
        operations.operator('alice', 'configure', credentials_stream=io.BytesIO(
            b'{"client_id":123,"client_secret":"invented-client-secret"}'),
            expected_user_id=99, redirect_uri='http://127.0.0.1:51111/oauth/callback')
        args, kwargs = self.child.call_args
        self.assertNotIn('invented-client-secret', repr(args))
        self.assertEqual(json.loads(kwargs['input_data']),
                         {'client_id': 123, 'client_secret': 'invented-client-secret'})
        self.assertIn('--credentials-stdin', args[2])

    def test_authorize_and_recovery_refuse_before_private_child_operation(self):
        for action in ('authorize', 'recover'):
            self.child.reset_mock()
            with self.assertRaises(AuthError) as caught: operations.operator('alice', action)
            self.assertEqual(caught.exception.code, 'live_provider_disabled')
            self.child.assert_called_once_with(self.target, self.record, ['--pilot-check'])

    def test_start_refuses_without_systemctl_or_provider_dispatch(self):
        with patch.object(operations, '_unit'), patch.object(operations, '_systemctl') as manager:
            with self.assertRaises(AuthError): operations.lifecycle('alice', 'start')
            manager.assert_not_called(); self.child.assert_not_called()

    def test_stop_targets_only_dedicated_unit_and_checks_known_stopped_ownership(self):
        with patch.object(operations, '_unit'), patch.object(operations, '_systemctl') as manager, \
             patch.object(operations, '_service_state', return_value={'MainPID': 0, 'ActiveState': 'inactive'}), \
             patch.object(operations, '_stopped', side_effect=stopped) as ownership:
            result = operations.lifecycle('alice', 'stop')
            manager.assert_called_once_with('stop', self.target['unit'], timeout=100)
            ownership.assert_called_once_with(self.target['root'], 991)
            self.assertTrue(result['ok'])

    def test_incomplete_shutdown_cannot_claim_success_or_clear_ownership(self):
        with patch.object(operations, '_unit'), patch.object(operations, '_systemctl'), \
             patch.object(operations, '_service_state', return_value={'MainPID': 123, 'ActiveState': 'deactivating'}), \
             patch.object(operations, '_stopped') as ownership:
            with self.assertRaises(ConfigError): operations.lifecycle('alice', 'stop')
            ownership.assert_not_called()

    def test_remove_refuses_active_or_enabled_unit_without_deleting_it(self):
        for state in ({'MainPID': 123, 'ActiveState': 'active', 'UnitFileState': 'disabled'},
                      {'MainPID': 0, 'ActiveState': 'inactive', 'UnitFileState': 'enabled'}):
            unit = SimpleNamespace(unlink=lambda: self.fail('removed unsafe service'))
            with patch.object(operations, '_stopped', side_effect=stopped), \
                 patch.object(operations, '_unit', return_value=unit), \
                 patch.object(operations, '_service_state', return_value=state):
                with self.assertRaises(ConfigError): operations.remove_unit('alice')

    def test_absent_unit_removal_retains_private_data_and_identity(self):
        with patch.object(operations, '_stopped', side_effect=stopped), \
             patch.object(operations, '_unit', return_value=None), \
             patch.object(operations, '_systemctl') as manager:
            result = operations.remove_unit('alice')
            self.assertTrue(result['code_and_private_state_retained'])
            self.assertTrue(result['service_identity_retained']); manager.assert_not_called()

    def test_invalid_operator_choices_refuse_before_binding_or_input(self):
        for options in ({'action': 'delete'}, {'action': 'status', 'mode': 'remote'},
                        {'action': 'configure'}, {'action': 'pacing-recover'},
                        {'action': 'status', 'credentials_stream': io.BytesIO(b'private')}):
            with self.assertRaises(ConfigError): operations.operator('alice', **options)
        self.binding.assert_not_called(); self.child.assert_not_called()


class PilotCliTests(unittest.TestCase):
    def test_cli_install_passes_exact_explicit_artifact_and_preset(self):
        with patch('poolrotater_host.pilot_installation.install', return_value={'ok': True}) as install, \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(['pilot-install', '--instance', 'alice', '--bundle', '/approved.zip',
                                      '--sha256', 'a'*64, '--preset', 'standard-fixed']), 0)
        install.assert_called_once_with('alice', Path('/approved.zip'), 'a'*64, presets=['standard-fixed'])

    def test_status_and_stop_cli_do_not_start_service(self):
        with patch.object(operations, 'status', return_value={'ok': True}) as status, \
             patch.object(operations, 'lifecycle', return_value={'ok': True}) as lifecycle, \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(['pilot-status', '--instance', 'alice']), 0)
            self.assertEqual(cli.main(['pilot-stop', '--instance', 'alice']), 0)
        status.assert_called_once_with('alice'); lifecycle.assert_called_once_with('alice', 'stop')

    def test_local_catalog_cli_cannot_accept_ambient_or_irrelevant_tls_inputs(self):
        with patch.object(operations, 'import_catalogs') as importer, \
             patch('sys.stderr', new_callable=io.StringIO):
            result = cli.main(['pilot-catalog-import', '--instance', 'alice', '--repository', '/repo',
                '--trust-root', '/root.json', '--trust-sha256', 'a'*64, '--mode', 'standard', '--ca-file', '/ca.crt'])
        self.assertEqual(result, 2); importer.assert_not_called()

    def test_https_import_constructs_bound_persistent_transport(self):
        with patch.object(operations, 'https_repository', return_value='invented transport') as origin, \
             patch.object(operations, 'import_catalogs', return_value={'ok': True}) as importer, \
             patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(cli.main(['pilot-catalog-import', '--instance', 'alice',
                '--origin', 'https://192.0.2.1/', '--ca-file', '/ca.crt', '--ca-sha256', 'b'*64,
                '--trust-root', '/root.json', '--trust-sha256', 'a'*64, '--mode', 'standard']), 0)
        origin.assert_called_once_with('alice', 'https://192.0.2.1/', Path('/ca.crt'), 'b'*64)
        self.assertEqual(importer.call_args.kwargs['repository'], 'invented transport')

    def test_cli_error_discards_arbitrary_os_error_paths(self):
        with patch.object(operations, 'check', side_effect=OSError('/private/credential')), \
             patch('sys.stderr', new_callable=io.StringIO) as stream:
            self.assertEqual(cli.main(['pilot-check', '--instance', 'alice']), 2)
        self.assertNotIn('/private', stream.getvalue())
