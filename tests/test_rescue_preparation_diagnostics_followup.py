"""Offline coverage for redacted held-update preparation diagnostics."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poolrotater_host import controller_update
from poolrotater_host.rescue import operations


class RescuePreparationDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.instance = 'first'
        self.root = Path('/var/lib/poolrotater-v7-pilot/first')
        self.prefix = Path('/opt/poolrotater-v7-pilot/first')
        self.installation_id = str(uuid.uuid4())
        self.generation = str(uuid.uuid4())
        self.operation_id = str(uuid.uuid4())
        self.before = {'installation_id': self.installation_id, 'generation': self.generation,
            'service_uid': 954, 'host_root': str(self.root), 'prefix': str(self.prefix),
            'execution': 'pilot-live', 'bundle_sha256': 'a' * 64,
            'release': str(self.prefix / 'releases/v7.3.2-test')}
        self.after = {**self.before, 'bundle_sha256': 'b' * 64,
            'release': str(self.prefix / 'releases/v7.3.3-test')}
        self.configuration = {'config/host.json': 'c' * 64,
            'config/rooms/room1.json': 'd' * 64}
        self.job = {'format': 1, 'id': self.operation_id, 'phase': 'held',
            'reason': 'commit_requires_review_retain_all_state', 'stage': 'configuration',
            'failure_stage': 'configuration', 'failure_code': 'update_guard_requires_review',
            'admission_kind': 'confirmed_maintenance_drained_rooms',
            'before': self.before, 'after': self.after, 'configuration': self.configuration,
            'queued_at': 100.0, 'commit_started_at': 101.0, 'commit_admitted_at': 101.0,
            'services_stopped_at': 102.0, 'startup_attempted_at': None,
            'release_approval_required': True, 'password': 'DO_NOT_LOG_PASSWORD',
            'provider_payload': 'DO_NOT_LOG_PROVIDER_PAYLOAD',
            'managed_values': {'token': 'DO_NOT_LOG_TOKEN'}}
        self.pointer = {'format': 1, 'installation_id': self.installation_id,
            'generation': self.generation, 'host_root': str(self.root), 'service_uid': 954,
            'store': str(self.prefix / 'catalog-stores' / self.installation_id),
            'fixture': False, 'execution': 'pilot-live'}
        self.catalog_current = {'format': 1, 'serial': 7, 'set_id': 'e' * 64,
            'modes': {'standard': 'f' * 64, 'taiko': '1' * 64}}
        self.catalog_after = {'format': 1, 'serial': 8, 'set_id': '2' * 64,
            'modes': {'standard': '3' * 64, 'taiko': '1' * 64}}
        self.catalog_transaction = {'format': 1, 'before': self.catalog_current,
            'after': self.catalog_after}
        from poolrotater_host.managed_host.upgrade import _binding
        self.preparation_binding = _binding({'root': self.root, 'prefix': self.prefix}, self.job)
        self.initial_inventory = {**self.configuration,
            'state/runtime-config.json': '4' * 64, 'state/managed-slots.json': '5' * 64}
        from poolrotater_host.managed_host.upgrade import _sha
        self.runtime_secret = {'profiles': [{'password': 'DO_NOT_LOG_PREPARATION_SECRET'}]}
        self.profile_after = {**self.initial_inventory,
            'state/runtime-config.json': _sha(self.runtime_secret)}
        self.projection_secret = {'slots': [{'token': 'DO_NOT_LOG_PROJECTION_SECRET'}]}
        self.projection_after = {**self.profile_after,
            'state/managed-slots.json': _sha(self.projection_secret)}
        self.preparation = {'format': 1, 'binding': self.preparation_binding,
            'initial': self.initial_inventory, 'complete': False,
            'steps': [
                {'name': 'profiles', 'before': self.initial_inventory,
                 'after': self.profile_after, 'writes': {'state/runtime-config.json': {
                     'kind': 'json', 'value': self.runtime_secret}},
                 'complete': True},
                {'name': 'projection', 'before': self.profile_after,
                 'after': self.projection_after, 'writes': {'state/managed-slots.json': {
                     'kind': 'json', 'value': self.projection_secret}},
                 'complete': False},
            ]}

    def _backend(self):
        backend = object.__new__(operations.Backend)
        backend.instance = self.instance
        backend.target = {'root': self.root, 'prefix': self.prefix, 'unit': 'pilot-first.service'}
        backend.pinned = {'installation_id': self.installation_id,
            'generation': self.generation, 'service_uid': 954,
            'host_root': str(self.root), 'prefix': str(self.prefix), 'execution': 'pilot-live'}
        backend.directory = self.prefix / 'recovery'
        backend.job = lambda: self.job
        return backend

    def _control(self, path, *args, **kwargs):
        name = str(path)
        if name.endswith('/managed-preparation-' + self.operation_id + '.json'):
            return self.preparation
        if name.endswith('/installations/' + self.installation_id + '.json'):
            return self.after
        if name.endswith('/pilot-upgrade.json'):
            return {'phase': 'committed', 'before': self.before, 'after': self.after}
        if name.endswith('/state/catalog-installation.json'):
            return self.pointer
        if name.endswith('/current.json'):
            return self.catalog_current
        if name.endswith('/pending.json') or name.endswith('/candidate.json'):
            return self.catalog_transaction
        if name.endswith('/managed-prewarm.json'):
            return {'format': 1, 'phase': 'ready', 'before': self.catalog_current,
                    'after': self.catalog_after, 'binding': {}}
        if name.endswith('/managed-standard-refresh.json'):
            return {'format': 1, 'phase': 'staging', 'before': self.catalog_current,
                    'after': None, 'binding': {}}
        raise AssertionError('unexpected diagnostic read path')

    def test_held_case_adds_bound_redacted_managed_and_catalog_progress(self):
        backend = self._backend()
        from poolrotater_host.managed_host import upgrade as managed_upgrade
        from poolrotater_host import provisioning
        with patch.object(operations, '_control', side_effect=self._control), \
             patch.object(provisioning, '_control', side_effect=self._control), \
             patch('poolrotater_host.pilot_operations._service_state',
                   return_value={'ActiveState': 'inactive', 'MainPID': 0}), \
             patch.object(controller_update, '_config_hashes', return_value=self.configuration), \
             patch.object(managed_upgrade, '_inventory', return_value=self.profile_after):
            frame = backend.inspect()
        encoded = json.dumps(frame, sort_keys=True)
        for secret in ('DO_NOT_LOG', 'DO_NOT_LOG_PASSWORD', 'DO_NOT_LOG_TOKEN',
                       'DO_NOT_LOG_PROVIDER_PAYLOAD', 'DO_NOT_LOG_PREPARATION_SECRET',
                       'DO_NOT_LOG_PROJECTION_SECRET', 'managed_values', 'provider_payload'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(frame['registration'], 'after')
        self.assertEqual(frame['upgrade'], 'committed')
        self.assertEqual(frame['configuration'], 'unchanged')
        self.assertEqual(frame['configuration_detail']['status'], 'same')
        preparation = frame['managed_preparation']
        self.assertEqual(preparation['status'], 'valid')
        self.assertTrue(preparation['operation_bound'])
        self.assertEqual(preparation['step_names'], ['profiles', 'projection'])
        self.assertEqual([step['complete'] for step in preparation['steps']], [True, False])
        self.assertTrue(preparation['steps'][0]['current_inventory_matches_after'])
        self.assertFalse(preparation['steps'][1]['current_inventory_matches_after'])
        self.assertTrue(preparation['current_inventory_matches_recorded_state'])
        catalogs = frame['catalogs']
        self.assertEqual(catalogs['status'], 'bound')
        self.assertEqual(catalogs['current']['serial'], 7)
        self.assertEqual(catalogs['current']['modes']['taiko'], '1' * 64)
        self.assertEqual(catalogs['pending']['status'], 'present')
        self.assertEqual(catalogs['prewarm']['phase'], 'ready')
        self.assertEqual(catalogs['standard_refresh']['phase'], 'staging')

    def test_missing_wrong_binding_and_malformed_preparation_are_bounded(self):
        backend = self._backend()
        from poolrotater_host.managed_host import upgrade as managed_upgrade
        with patch.object(operations, '_control', return_value=None), \
             patch.object(managed_upgrade, '_inventory', return_value=self.initial_inventory):
            self.assertEqual(backend._managed_preparation_diagnostic(self.job)['status'], 'absent')

        wrong = copy.deepcopy(self.preparation)
        wrong['binding']['operation_id'] = str(uuid.uuid4())
        with patch.object(operations, '_control', return_value=wrong):
            frame = backend._managed_preparation_diagnostic(self.job)
        self.assertEqual(frame, {'status': 'wrong_binding', 'operation_bound': False})

        malformed = copy.deepcopy(self.preparation)
        malformed['steps'][1]['name'] = 'unexpected'
        with patch.object(operations, '_control', return_value=malformed):
            frame = backend._managed_preparation_diagnostic(self.job)
        self.assertEqual(frame, {'status': 'invalid', 'operation_bound': False})

    def test_catalog_pointer_is_fixed_and_unavailable_data_is_bounded(self):
        backend = self._backend()
        from poolrotater_host import provisioning
        seen = []
        def control(path, *args, **kwargs):
            seen.append(str(path))
            if str(path).endswith('/state/catalog-installation.json'):
                bad = dict(self.pointer)
                bad['store'] = '/outside/catalog-store'
                return bad
            raise AssertionError('store path must not be followed after pointer mismatch')
        with patch.object(provisioning, '_control', side_effect=control):
            result = backend._catalog_diagnostic(self.job)
        self.assertEqual(result, {'status': 'pointer_binding_mismatch'})
        self.assertEqual(len(seen), 1)

        with patch.object(provisioning, '_control', return_value=None):
            self.assertEqual(backend._catalog_diagnostic(self.job), {'status': 'pointer_absent'})

    def test_configuration_differences_are_bounded_and_hashes_hidden(self):
        baseline = {f'config/rooms/room{i}.json': f'{i:064x}' for i in range(40)}
        baseline['config/private-DO_NOT_LOG_FILENAME_SECRET.json'] = 'a' * 64
        baseline['config/host.json'] = 'b' * 64
        current = {path: 'f' * 64 for path in baseline}
        result = operations._configuration_diagnostic({'configuration': baseline}, current)
        self.assertEqual(result['status'], 'changed')
        self.assertEqual(result['differing_paths'], ['config/host.json',
            'config/rooms/room1.json','config/rooms/room2.json',
            'config/rooms/room3.json','config/rooms/room4.json'])
        self.assertEqual(result['unreported_difference_count'], 37)
        self.assertNotIn('DO_NOT_LOG_FILENAME_SECRET', json.dumps(result))
        self.assertNotIn('f' * 64, json.dumps(result))

    def test_only_allowlisted_restoration_failure_code_is_reported(self):
        backend = self._backend()
        from poolrotater_host import provisioning
        from poolrotater_host.managed_host import upgrade as managed_upgrade
        from poolrotater_host.update_drain import RESTORATION_FAILURE_CODES
        self.job['drain_restoration_failure_code'] = next(iter(RESTORATION_FAILURE_CODES))
        with patch.object(operations, '_control', side_effect=self._control), \
             patch.object(provisioning, '_control', side_effect=self._control), \
             patch('poolrotater_host.pilot_operations._service_state',
                   return_value={'ActiveState': 'inactive', 'MainPID': 0}), \
             patch.object(controller_update, '_config_hashes', return_value=self.configuration), \
             patch.object(managed_upgrade, '_inventory', return_value=self.profile_after):
            frame = backend.inspect()
        self.assertEqual(frame['drain_restoration_failure_code'], self.job['drain_restoration_failure_code'])

        self.job['drain_restoration_failure_code'] = 'DO_NOT_LOG_PRIVATE_FAILURE'
        with patch.object(operations, '_control', side_effect=self._control), \
             patch.object(provisioning, '_control', side_effect=self._control), \
             patch('poolrotater_host.pilot_operations._service_state',
                   return_value={'ActiveState': 'inactive', 'MainPID': 0}), \
             patch.object(controller_update, '_config_hashes', return_value=self.configuration), \
             patch.object(managed_upgrade, '_inventory', return_value=self.profile_after):
            frame = backend.inspect()
        self.assertNotIn('drain_restoration_failure_code', frame)
        self.assertNotIn('DO_NOT_LOG_PRIVATE_FAILURE', json.dumps(frame))

    def test_storage_reserve_and_missing_cache_are_read_only(self):
        backend = self._backend()
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            backend.target['prefix'] = prefix
            backend.pinned['service_uid'] = os.geteuid()
            with patch.object(operations, 'protected', side_effect=lambda path: path), \
                 patch.object(operations.shutil, 'disk_usage', return_value=SimpleNamespace(free=6*1024**3)):
                absent = backend._catalog_storage_diagnostic()
            self.assertEqual(absent['status'], 'absent')
            self.assertTrue(absent['reserve_satisfied'])
            self.assertEqual(absent['cache_entry_count'], 0)
            self.assertFalse((prefix/'managed-catalog-delivery').exists())

            cache = prefix/'managed-catalog-delivery'
            cache.mkdir(mode=0o700)
            backend.pinned['service_uid'] = os.geteuid()+1
            for name in ('.lease','origin.json','a'*64+'.data','b'*64+'.part','c'*64+'.json'):
                path=cache/name
                path.write_text('fixture-only')
                path.chmod(0o600)
            with patch.object(operations, 'protected', side_effect=lambda path: path), \
                 patch.object(operations.shutil, 'disk_usage', return_value=SimpleNamespace(free=4*1024**3)):
                low = backend._catalog_storage_diagnostic()
            self.assertEqual(low['status'], 'available')
            self.assertFalse(low['reserve_satisfied'])
            self.assertEqual(low['filesystem_free_bytes'], 4*1024**3)
            self.assertEqual(low['custody_fault_count'], 0)
            self.assertEqual(low['cache_entry_count'], 5)

    def test_storage_counts_symlink_special_and_custody_faults_without_names(self):
        backend = self._backend()
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            backend.target['prefix'] = prefix
            backend.pinned['service_uid'] = os.geteuid()
            cache = prefix/'managed-catalog-delivery'
            cache.mkdir(mode=0o700)
            outside = prefix/'outside-secret-content'
            outside.write_text('DO_NOT_READ')
            (cache/'DO_NOT_LOG_NAME').symlink_to(outside)
            os.mkfifo(cache/'DO_NOT_LOG_FIFO')
            (cache/'DO_NOT_LOG_UNEXPECTED').write_text('DO_NOT_LOG_CONTENT')
            with patch.object(operations, 'protected', side_effect=lambda path: path), \
                 patch.object(operations.shutil, 'disk_usage', return_value=SimpleNamespace(free=6*1024**3)):
                result = backend._catalog_storage_diagnostic()
            encoded = json.dumps(result)
            self.assertEqual(result['status'], 'unsafe')
            self.assertEqual(result['cache_entry_count'], 3)
            self.assertEqual(result['symlink_count'], 1)
            self.assertEqual(result['special_entry_count'], 1)
            self.assertEqual(result['custody_fault_count'], 3)
            self.assertNotIn('DO_NOT_LOG', encoded)
            self.assertNotIn('DO_NOT_READ', encoded)

    def test_storage_entry_count_is_capped(self):
        backend = self._backend()
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary)
            backend.target['prefix'] = prefix
            backend.pinned['service_uid'] = os.geteuid()
            cache = prefix/'managed-catalog-delivery'
            cache.mkdir(mode=0o700)
            for index in range(258):
                (cache/f'{index:064x}.data').touch(mode=0o600)
            with patch.object(operations, 'protected', side_effect=lambda path: path), \
                 patch.object(operations.shutil, 'disk_usage', return_value=SimpleNamespace(free=6*1024**3)):
                result = backend._catalog_storage_diagnostic()
            self.assertEqual(result['cache_entry_count'], 257)
            self.assertTrue(result['entry_count_capped'])
            self.assertEqual(result['custody_fault_count'], 0)

    def test_storage_does_not_probe_before_fixed_prefix_validation(self):
        backend = self._backend()
        with patch.object(operations, 'protected', side_effect=ValueError('unsafe')), \
             patch.object(operations.shutil, 'disk_usage') as disk_usage:
            result = backend._catalog_storage_diagnostic()
        self.assertEqual(result, {'status': 'unavailable',
                                  'failure_code': 'catalog_storage_diagnostic_unavailable'})
        disk_usage.assert_not_called()


if __name__ == '__main__':
    unittest.main()
