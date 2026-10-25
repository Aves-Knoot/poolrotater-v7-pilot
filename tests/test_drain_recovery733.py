"""Interrupted native commits retain fresh closure authority, never old creates."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from unittest.mock import patch
import time
import unittest
from poolrotater_host.layout import atomic_json,read_json
from poolrotater_host.config import ConfigError
from tests import test_update_recovery as fixtures


class DrainRecoveryTests(unittest.TestCase):
    run_recovery=fixtures.ControllerRecoveryTests.run_recovery
    def setUp(self):
        fixtures.ControllerRecoveryTests.setUp(self)
        self.service.update(MainPID=12,ActiveState='active')
        self.job.update(admission_kind='confirmed_maintenance_drained_rooms',
                        controller_update_drain={'request_id':'invented'},
                        phase='held')
        atomic_json(self.target['prefix']/self.native.NAME,self.job)
        self.rearm=self.enterContext(patch('poolrotater_host.update_drain.rearm',return_value={'phase':'rearmed'}))

    def test_fresh_original_controller_closed_proof_resumes_commit_once(self):
        def commit(instance,target,parent,job,save):save(phase='complete')
        with patch.object(self.native,'_drained_rows',return_value={'room1':{}}) as proof, \
             patch.object(self.native,'_commit',side_effect=commit) as apply:
            result=self.run_recovery('complete')
        self.assertTrue(result['independent_drain_revalidated'])
        apply.assert_called_once();self.fresh.assert_not_called()
        self.assertIsNotNone(proof.call_args.kwargs['after'])
        self.assertEqual(read_json(self.target['prefix']/self.native.NAME)['phase'],'complete')
        self.finish.assert_not_called();self.lifecycle.assert_not_called()

    def test_changed_controller_cannot_reuse_old_drain_against_new_rooms(self):
        self.rearm.side_effect=ConfigError('changed controller')
        with patch.object(self.native,'_drained_rows',return_value=None), \
             patch.object(self.native,'_commit') as apply:
            result=self.run_recovery('complete')
        apply.assert_not_called();self.fresh.assert_called_once()
        reset=read_json(self.target['prefix']/self.native.NAME)
        self.assertEqual(result['phase'],'waiting_empty')
        self.assertTrue(reset['independent_drain_disabled'])
        self.assertNotIn('controller_update_drain',reset)
        self.lifecycle.assert_not_called();self.finish.assert_not_called()

    def test_expired_post_validation_observation_cannot_authorize_commit(self):
        original=time.monotonic;calls=[]
        def clock():
            if not self.approval.called:return original()
            calls.append(1);return 1. if len(calls)==1 else 20.
        with patch.object(self.native,'_drained_rows',return_value={'room1':{}}), \
             patch('poolrotater_host.updates.recovery.time.monotonic',side_effect=clock), \
             patch.object(self.native,'_commit') as apply:
            with self.assertRaises(ConfigError):self.run_recovery('complete')
        apply.assert_not_called();self.finish.assert_not_called();self.lifecycle.assert_not_called()

    def test_partial_drain_rearms_same_request_without_fictitious_worker_health(self):
        with patch.object(self.native,'_drained_rows',return_value=None), \
             patch.object(self.native,'_commit') as apply:
            result=self.run_recovery('complete')
        apply.assert_not_called();self.fresh.assert_not_called()
        reset=read_json(self.target['prefix']/self.native.NAME)
        self.assertTrue(result['independent_drain_revalidated'])
        self.assertEqual(reset['phase'],'waiting_empty')
        self.assertIn('controller_update_drain',reset)
        self.assertIn('nonce',reset)
        self.assertNotIn('commit_started_at',reset)
        self.lifecycle.assert_not_called();self.finish.assert_not_called()
