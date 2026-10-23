"""A stopped reviewed upgrade pair never grants arbitrary storage changes."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import unittest
from unittest.mock import patch

from poolrotater_host.config import ConfigError
from poolrotater_host import integrated_upgrade
from poolrotater_host.pilot_upgrade import require_repair_compatible
from poolrotater_host.runtime_package import _reviewed_live_policy, json_bytes, REVIEWED_SINGLE_ROOM_BASELINES
from poolrotater_host.live_release import policy, policy_bytes


class IntegratedUpgradeTests(unittest.TestCase):
    def test_historical_policy_inspection_requires_exact_published_digest(self):
        old = json_bytes({**policy(), 'release':'v7.0.1', 'maximum_rooms': 1})
        for digest in REVIEWED_SINGLE_ROOM_BASELINES:
            self.assertTrue(_reviewed_live_policy(digest, old))
            self.assertFalse(_reviewed_live_policy(digest, json_bytes({**policy(), 'maximum_rooms': 5})))
        self.assertFalse(_reviewed_live_policy('0' * 64, old))
        self.assertTrue(_reviewed_live_policy('0' * 64, policy_bytes()))

    def setUp(self):
        common = {'version': 'v7.0.1', 'protocol': 1, 'storage_epoch': 'unchanged',
                  'requirements': 'pins', 'python_hash': 'python',
                  'environment_hash': 'environment', 'migrations': []}
        self.before = {**common, 'shared': {'fixed.py': 'old'}, 'storage': {'ratings.py': 'legacy'}}
        self.after = {**common, 'shared': {'fixed.py': 'new'}, 'storage': {'ratings.py': 'm4'}}
        self.contract = ({k: copy.deepcopy(self.before[k]) for k in ('shared','storage')},
                         {k: copy.deepcopy(self.after[k]) for k in ('shared','storage')})

    def test_checkout_has_no_integrated_upgrade_grant(self):
        # Frozen successor artifacts append reviewed pairs. Verify historical
        # grants and all executable policy code against the exact public base.
        import ast,zipfile
        from pathlib import Path
        from full_catalog_fixtures import historical_runtime
        baseline=historical_runtime('v7.1.20')
        with zipfile.ZipFile(baseline)as archive:
            before=ast.parse(archive.read('payload/poolrotater_host/integrated_upgrade.py'))
        after=ast.parse(Path(integrated_upgrade.__file__).read_bytes())
        def split(tree):
            node=next(n for n in tree.body if isinstance(n,ast.Assign) and any(
                isinstance(t,ast.Name) and t.id=='REVIEWED_TRANSITIONS' for t in n.targets))
            pairs=ast.literal_eval(node.value);tree.body.remove(node)
            for extra in list(tree.body):
                if isinstance(extra,ast.AugAssign) and isinstance(extra.target,ast.Name) and extra.target.id=='REVIEWED_TRANSITIONS':
                    self.assertIsInstance(extra.op,ast.Add)
                    additions=ast.literal_eval(extra.value)
                    self.assertIsInstance(additions,tuple)
                    for pair in additions:
                        self.assertEqual(len(pair),2)
                        for endpoint in pair:
                            self.assertEqual(set(endpoint),{'shared','storage'})
                            for group in endpoint.values():
                                self.assertIsInstance(group,dict)
                                for digest in group.values():
                                    self.assertRegex(digest,r'^[0-9a-f]{64}$')
                    pairs+=additions;tree.body.remove(extra)
            return pairs,ast.dump(tree,include_attributes=False)
        old_pairs,old_code=split(before);new_pairs,new_code=split(after)
        # Reviewed sets may be reordered by later frozen releases; membership
        # and executable policy are invariant, not tuple insertion order.
        for pair in old_pairs:
            self.assertIn(pair,new_pairs)
        self.assertEqual(new_code,old_code)
        with self.assertRaises(ConfigError):
            require_repair_compatible(self.before, self.after)

    def test_exact_generated_pair_admits_only_stopped_compatibility(self):
        with patch.object(integrated_upgrade, 'REVIEWED_TRANSITIONS', (self.contract,)):
            require_repair_compatible(self.before, self.after)
            self.assertEqual(self.after['migrations'], [])
            with self.assertRaises(ConfigError):
                require_repair_compatible(self.after, self.before)

    def test_arbitrary_storage_or_shared_byte_change_refused(self):
        with patch.object(integrated_upgrade, 'REVIEWED_TRANSITIONS', (self.contract,)):
            for part in ('shared', 'storage'):
                changed = copy.deepcopy(self.after)
                changed[part][next(iter(changed[part]))] = 'unreviewed'
                with self.subTest(part=part), self.assertRaises(ConfigError):
                    require_repair_compatible(self.before, changed)

    def test_pair_cannot_change_runtime_contract_or_run_migrations(self):
        with patch.object(integrated_upgrade, 'REVIEWED_TRANSITIONS', (self.contract,)):
            for key in ('version','protocol','storage_epoch','requirements','python_hash','environment_hash','migrations'):
                changed = copy.deepcopy(self.after)
                changed[key] = ['DDL'] if key == 'migrations' else 'changed'
                with self.subTest(key=key), self.assertRaises(ConfigError):
                    require_repair_compatible(self.before, changed)


if __name__ == '__main__':
    unittest.main()
