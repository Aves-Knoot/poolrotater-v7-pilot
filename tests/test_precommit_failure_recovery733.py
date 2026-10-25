"""Recovery keeps the confirmed failed-controller admission receipt bound."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import unittest
import uuid
from unittest.mock import patch

from poolrotater_host.config import ConfigError
from poolrotater_host.network.contracts import fingerprint
from poolrotater_host.updates import recovery


class PrecommitFailureRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.before={'bundle_sha256':'a'*64,'installation_id':str(uuid.uuid4()),
            'generation':str(uuid.uuid4()),'prefix':'/opt/prefix','host_root':'/opt/host',
            'service_uid':957,'execution':'provider-live-pilot'}
        self.after={**self.before,'bundle_sha256':'b'*64}
        self.process={'pid':1234,'start':'12345','boot':str(uuid.uuid4())}
        self.proof={'process':self.process,'drain_request_id':str(uuid.uuid4()),
            'closed_aliases':['room1','room2'],'configuration_sha256':'c'*64,
            'projection_sha256':'d'*64,'provider_service_failure':{
                'pid':1234,'workers_reaped':True,'referee_child_reaped':True,
                'systemd_invocation':{'unit':'poolrotater-v7-fixture.service',
                    'invocation_id':'e'*32,'exec_main_pid':1234,
                    'exec_main_started_monotonic':1000000,
                    'exec_main_exited_monotonic':2000000,'cgroup_empty':True}}}
        self.job={'admission_kind':'confirmed_closed_controller_failure',
            'commit_started_at':1.0,'before':self.before,'after':self.after,
            'process':self.process,'rooms':['room1','room2'],
            'controller_update_drain':{'request_id':self.proof['drain_request_id']},
            'precommit_controller_failure_proof':self.proof,
            'precommit_controller_failure_proof_sha256':fingerprint(self.proof)}
        self.target={'unit':'poolrotater-v7-fixture.service'}

    def test_before_registration_revalidates_current_closed_proof_after_systemd_reset(self):
        stopped={'MainPID':0,'ActiveState':'inactive'}
        with patch('poolrotater_host.controller_update._closed_failed_controller_proof',
                   return_value=self.proof) as check:
            result=recovery._revalidate_precommit_controller_failure(
                'fixture',self.target,self.before,self.job,self.before,stopped)
        self.assertEqual(result,self.proof)
        check.assert_called_once_with('fixture',self.target,self.before,self.job,
            committing=True,after_native_stop=True)

    def test_replaced_or_live_predecessor_is_never_restarted_or_adopted(self):
        with patch('poolrotater_host.controller_update._closed_failed_controller_proof') as check:
            with self.assertRaises(ConfigError):
                recovery._revalidate_precommit_controller_failure('fixture',self.target,
                    self.before,self.job,self.before,{'MainPID':9876,'ActiveState':'active'})
            check.assert_not_called()

    def test_changed_current_closure_cannot_use_persisted_receipt_alone(self):
        with patch('poolrotater_host.controller_update._closed_failed_controller_proof',
                   return_value={**self.proof,'configuration_sha256':'f'*64}):
            with self.assertRaises(ConfigError):
                recovery._revalidate_precommit_controller_failure('fixture',self.target,
                    self.before,self.job,self.before,{'MainPID':0,'ActiveState':'failed'})

    def test_successor_registration_uses_root_receipt_and_existing_commit_checks(self):
        with patch('poolrotater_host.controller_update._closed_failed_controller_proof') as check:
            result=recovery._revalidate_precommit_controller_failure('fixture',self.target,
                self.before,self.job,self.after,{'MainPID':0,'ActiveState':'inactive'})
        self.assertEqual(result,self.proof)
        check.assert_not_called()

    def test_malformed_or_missing_root_receipt_refuses(self):
        for proof in (None,{**self.proof,'process':{**self.process,'pid':4321}},
                      {**self.proof,'provider_service_failure':{}}):
            with self.subTest(proof=proof):
                self.job['precommit_controller_failure_proof']=proof
                self.job['precommit_controller_failure_proof_sha256']=fingerprint(proof)
                with self.assertRaises(ConfigError):
                    recovery._revalidate_precommit_controller_failure('fixture',self.target,
                        self.before,self.job,self.after,{'MainPID':0,'ActiveState':'inactive'})


if __name__=='__main__':
    unittest.main()
