"""Terminal Catch plans keep verified provenance across controller updates."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
from unittest.mock import patch
import copy,unittest
from poolrotater_host import catch_pilot_operations as ops
from poolrotater_host.config import ConfigError

class CatchControllerLineageTests(unittest.TestCase):
    def setUp(self):
        self.parent={'installation_id':'invented-install','generation':'invented-generation',
            'host_root':'/invented/root','service_uid':1000,'prefix':'/invented/prefix',
            'execution':'pilot-live','bundle_sha256':'b'*64}
        self.before={**self.parent,'bundle_sha256':'a'*64}
        self.value={'phase':'committed','before':self.before,'after':self.parent}
        self.target={'prefix':Path('/invented/prefix')}
        self.enterContext(patch('poolrotater_host.pilot_upgrade._record',side_effect=lambda v,*_:v))
        self.enterContext(patch('poolrotater_host.pilot_upgrade._transition',return_value={'reviewed':'transition'}))
        self.transition=self.enterContext(patch('poolrotater_host.runtime_lifecycle.read_transition',return_value={'reviewed':'transition'}))
        self.validate=self.enterContext(patch('poolrotater_host.runtime_lifecycle.validate_runtime',return_value={'manifest':{'files':{'poolrotater_host/catch_pilot_operations.py':'invented-hash'}}}))
        self.control=self.enterContext(patch.object(ops,'_control',return_value=self.value))
        self.ancestors=self.enterContext(patch('poolrotater_host.worker_releases._committed_ancestors',return_value=iter(())))
    def test_exact_committed_predecessor_remains_usable_without_rewriting_parent(self):
        before=copy.deepcopy(self.value)
        self.assertTrue(ops._historical_parent(self.target,self.parent,'a'*64))
        self.assertEqual(self.value,before);self.validate.assert_any_call(self.before,self.target['prefix'])
        self.ancestors.assert_not_called()
    def test_older_record_must_come_from_checked_native_history_chain(self):
        ancestor={**self.before,'bundle_sha256':'c'*64};self.ancestors.return_value=iter((ancestor,))
        self.assertTrue(ops._historical_parent(self.target,self.parent,'c'*64))
        self.ancestors.assert_called_once_with(self.parent,self.value)
    def test_unknown_foreign_or_unfinished_lineage_does_not_authorize_adoption(self):
        self.assertFalse(ops._historical_parent(self.target,self.parent,'d'*64))
        self.value['phase']='prepared';self.assertFalse(ops._historical_parent(self.target,self.parent,'a'*64))
        self.value['phase']='committed';self.value['after']={**self.parent,'generation':'foreign'}
        self.assertFalse(ops._historical_parent(self.target,self.parent,'a'*64))
    def test_transition_mismatch_or_changed_runtime_refuses(self):
        self.transition.return_value={'different':'transition'}
        with self.assertRaises(ConfigError):ops._historical_parent(self.target,self.parent,'a'*64)
        self.transition.return_value={'reviewed':'transition'};self.validate.side_effect=ConfigError('changed bytes')
        with self.assertRaises(ConfigError):ops._historical_parent(self.target,self.parent,'a'*64)
    def test_foreign_ancestor_or_pre_catch_payload_cannot_authorize_plan(self):
        self.before['generation']='foreign';self.assertFalse(ops._historical_parent(self.target,self.parent,'a'*64))
        self.before['generation']=self.parent['generation'];self.validate.return_value={'manifest':{'files':{}}}
        self.assertFalse(ops._historical_parent(self.target,self.parent,'a'*64))
    def test_only_terminal_same_namespace_plans_may_consult_history(self):
        marker={k:self.parent[k]for k in('installation_id','generation')}
        plan={'format':1,**marker,'parent_sha256':'a'*64,'phase':'applying'}
        self.control.return_value=plan
        with patch.object(ops,'_historical_parent',return_value=True)as check:
            with self.assertRaises(ConfigError):ops._load_job(self.target,marker,self.parent)
            check.assert_not_called()
            plan['phase']='committed';self.assertIs(ops._load_job(self.target,marker,self.parent),plan)
            plan['generation']='foreign'
            with self.assertRaises(ConfigError):ops._load_job(self.target,marker,self.parent)
