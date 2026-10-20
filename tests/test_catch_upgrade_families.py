"""Manual Catch repair requires both the reviewed family and exact contracts."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import unittest
from unittest.mock import patch
from poolrotater_host.pilot_upgrade import require_repair_compatible
from poolrotater_host.config import ConfigError

class CatchUpgradeFamilyTests(unittest.TestCase):
    def manifest(self,version):
        return dict(version=version,protocol=1,storage_epoch='invented-unchanged',
            requirements='invented-unchanged',python_hash='invented-unchanged',
            environment_hash='invented-unchanged',migrations=[],shared={})
    def test_only_declared_family_pairs_reach_exact_contract_review(self):
        for before,after in [('v7.1.20','v7.2.1'),('v7.2.0','v7.2.1'),('v7.2.1','v7.2.0'),('v7.2.1','v7.1.20')]:
            with self.subTest(before=before,after=after),patch('poolrotater_host.integrated_upgrade.admits',return_value=True)as reviewed:
                require_repair_compatible(self.manifest(before),self.manifest(after));reviewed.assert_called_once()
        with patch('poolrotater_host.integrated_upgrade.admits',return_value=True)as reviewed:
            with self.assertRaisesRegex(ConfigError,'reviewed runtime family'):
                require_repair_compatible(self.manifest('v7.2.1'),self.manifest('v7.2.2'))
            reviewed.assert_not_called()
    def test_exact_review_and_persistent_contract_are_still_required(self):
        before=self.manifest('v7.2.0');after=self.manifest('v7.2.1')
        with patch('poolrotater_host.integrated_upgrade.admits',return_value=False):
            with self.assertRaisesRegex(ConfigError,'exact reviewed transition'):require_repair_compatible(before,after)
        after['environment_hash']='changed'
        with patch('poolrotater_host.integrated_upgrade.admits',return_value=True)as reviewed:
            with self.assertRaisesRegex(ConfigError,'persistent/runtime contract'):require_repair_compatible(before,after)
            reviewed.assert_not_called()
