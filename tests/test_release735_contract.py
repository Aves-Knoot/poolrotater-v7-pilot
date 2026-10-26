"""Qualification of the exact candidate and both deployed predecessor contracts."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import json
from pathlib import Path
import unittest
import zipfile

from poolrotater_host.config import ConfigError
from poolrotater_host.pilot_upgrade import require_repair_compatible
from poolrotater_host.runtime_package import inspect
from poolrotater_host.updates.publisher import classify
from scripts.prepare_v71_runtime import fingerprints

BASE = Path('/root/poolrotater-catch-integration-qk9y_929/work/validation/maintenance735')
PINS = {'v7.3.3': '45254e8a24bee3b4c86259a31528e3e06a11b54aa201afca54e63981f0e45a41',
        'v7.3.4': '1392bb5226526228c6f5d02803f1cfaac5dc438ee7be403c0dd570469d1006ce'}
CONTEXT = {'protocol': 1, 'storage_epoch': 1, 'requirements': 'same',
           'python_hash': 'same', 'environment_hash': 'same', 'migrations': []}


def manifest(path, sha):
    item = inspect(path, sha, execution='pilot-live')
    with zipfile.ZipFile(path) as bundle:
        fingerprint = fingerprints(item['files'], bundle.read('payload/rolling/release.py').decode())
    return item, fingerprint


class ExactReleaseContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        freeze = json.loads((BASE / 'candidate/freeze.json').read_text())
        cls.candidate, cls.after = manifest(BASE / 'candidate/runtime.zip', freeze['runtime_sha256'])
        cls.baselines = {version: manifest(BASE / 'inputs' / (version + '.zip'), digest)
                         for version, digest in PINS.items()}

    def test_exact_predecessors_are_controller_upgrades_with_unchanged_storage(self):
        for version, (old, before) in self.baselines.items():
            with self.subTest(version=version):
                self.assertEqual(before['storage'], self.after['storage'])
                self.assertEqual(old['environment_id'], self.candidate['environment_id'])
                self.assertEqual(classify(old, self.candidate), 'controller')
                require_repair_compatible({**CONTEXT, **before, 'version': version},
                                         {**CONTEXT, **self.after, 'version': 'v7.3.5'})

    def test_unreviewed_contract_and_version_are_rejected(self):
        for version, (_, before) in self.baselines.items():
            with self.subTest(version=version):
                altered = {**before, 'shared': dict(before['shared'])}
                altered['shared']['rolling/broker.py'] = '0' * 64
                with self.assertRaises(ConfigError):
                    require_repair_compatible({**CONTEXT, **altered, 'version': version},
                                             {**CONTEXT, **self.after, 'version': 'v7.3.5'})
                changed_storage = {**self.after, 'storage': dict(self.after['storage'])}
                changed_storage['storage']['bot/player_preferences.py'] = '0' * 64
                with self.assertRaises(ConfigError):
                    require_repair_compatible({**CONTEXT, **before, 'version': version},
                                             {**CONTEXT, **changed_storage, 'version': 'v7.3.5'})
                with self.assertRaises(ConfigError):
                    require_repair_compatible({**CONTEXT, **before, 'version': 'v7.3.2'},
                                             {**CONTEXT, **self.after, 'version': 'v7.3.5'})
