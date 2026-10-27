"""Diagnosis never rewrites a rejected recovery pin or exposes record values."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,json,unittest
from unittest.mock import patch
from poolrotater_host.rescue import pin_refresh


class LineageDiagnostic(unittest.TestCase):
    def setUp(self):
        self.current={k:'invented-'+k for k in pin_refresh._IDENTITY_FIELDS}
        self.current.update(bundle_sha256='b'*64,environment='private-path-must-stay-local')
        self.old={**self.current,'bundle_sha256':'a'*64}
    def diagnostic(self,nodes,ancestor):
        before=copy.deepcopy(self.old)
        with patch.object(pin_refresh,'_upgrade_lineage',return_value=({},nodes,ancestor)), \
             patch.object(pin_refresh,'atomic_json',side_effect=AssertionError('no writes')):
            result=pin_refresh.lineage_diagnostic('/invented',self.current,self.old)
        self.assertEqual(self.old,before)
        self.assertNotIn('private-path-must-stay-local',json.dumps(result))
        self.assertFalse(result['pin_refreshed']);return result
    def test_exact_ancestor_is_reported_without_changing_pin(self):
        result=self.diagnostic([self.current,self.old],True)
        self.assertEqual(result['status'],'ancestor');self.assertTrue(result['lineage_qualified'])
        self.assertEqual(result['matching_bundle_records'],[{'exact_record':True,'differing_fields':[]}])
    def test_same_bundle_with_changed_inventory_is_not_promoted_to_ancestor(self):
        node={**self.old,'release_snapshot':{'files':{'invented':'c'*64}}}
        result=self.diagnostic([self.current,node],False)
        self.assertEqual(result['status'],'not-ancestor');self.assertFalse(result['lineage_qualified'])
        self.assertEqual(result['matching_bundle_records'],[{'exact_record':False,'differing_fields':['release_snapshot']}])
    def test_unrelated_pin_remains_unqualified(self):
        result=self.diagnostic([self.current],False)
        self.assertEqual(result['matching_bundle_records'],[]);self.assertFalse(result['lineage_qualified'])
    def test_foreign_identity_refuses_before_lineage_walk(self):
        self.old['generation']='other'
        with patch.object(pin_refresh,'_upgrade_lineage') as walk:
            with self.assertRaises(pin_refresh._Refusal)as error:
                pin_refresh.lineage_diagnostic('/invented',self.current,self.old)
            self.assertEqual(error.exception.reason,'recovery_pin_identity_changed');walk.assert_not_called()
    def test_history_validation_errors_remain_refusals(self):
        with patch.object(pin_refresh,'_upgrade_lineage',side_effect=pin_refresh._Refusal('upgrade_history_gap')):
            with self.assertRaises(pin_refresh._Refusal)as error:
                pin_refresh.lineage_diagnostic('/invented',self.current,self.old)
        self.assertEqual(error.exception.reason,'upgrade_history_gap')
