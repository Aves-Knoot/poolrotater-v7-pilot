"""Update policy, outage containment and durable operation reconciliation."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import copy
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

from poolrotater_host.layout import atomic_json
from poolrotater_host.updates.agent import Agent,configure
from poolrotater_host.updates.client import read
from poolrotater_host.config import ConfigError
from tests import test_update_release_delivery as delivery


class UpdateAgentTests(unittest.TestCase):
    setUp=delivery.ReleaseDeliveryTests.setUp
    publish=delivery.ReleaseDeliveryTests.publish
    serve=delivery.ReleaseDeliveryTests.serve
    client=delivery.ReleaseDeliveryTests.client

    def setup_agent(self,**choices):
        self.publish();self.serve();self.host=self.client()
        configure(self.host,channel='pilot',**{'automatic_download':False,'automatic_install':False,'controller_updates':False,'report_to_hub':False,**choices})
        self.native={'phase':'absent','waiting_rooms':['room1']}
        self.backend=Mock()
        self.backend.current.return_value={'controller_sha256':self.before_sha,
            'effective_sha256':self.before_sha,'version':'v7.0.2','deployment_busy':False}
        self.backend.observe.side_effect=lambda op:dict(self.native)
        def queue(path,item,identifier):self.native['phase']='waiting_empty'
        self.backend.queue.side_effect=queue
        return Agent(self.host,self.backend,clock=lambda:self.now,jitter=lambda a,b:0)

    def test_explicit_check_only_policy_never_downloads_or_applies(self):
        agent=self.setup_agent()
        result=agent.step();self.assertEqual(result['phase'],'available')
        self.assertFalse(list((self.host.root/'downloads').iterdir()))
        self.backend.stage.assert_not_called();self.backend.queue.assert_not_called()

    def test_download_only_consent_and_manual_apply_are_distinct(self):
        agent=self.setup_agent(automatic_download=True)
        self.assertEqual(agent.step()['phase'],'downloaded')
        self.backend.queue.assert_not_called()
        self.assertEqual(agent.step('apply')['phase'],'waiting_empty')
        self.assertEqual(self.backend.queue.call_count,1)

    def test_occupied_room_waits_across_agent_restart_and_is_not_requeued(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        self.assertEqual(agent.step()['phase'],'waiting_empty')
        operation=agent.state()['operation']['id']
        restarted=Agent(self.host,self.backend,clock=lambda:self.now,jitter=lambda a,b:0)
        for _ in range(4):
            result=restarted.step();self.assertEqual(result['phase'],'waiting_empty')
            self.assertEqual(result['waiting_rooms'],['room1'])
        self.backend.queue.assert_called_once()
        self.assertEqual(restarted.state()['operation']['id'],operation)
        self.native.update(phase='complete',waiting_rooms=[])
        self.assertEqual(restarted.step()['phase'],'installed')
        self.assertIsNone(restarted.state()['operation'])

    def test_failed_installation_is_held_without_an_endless_retry_loop(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        self.backend.stage.side_effect=ConfigError('private implementation detail must not escape')
        self.assertEqual(agent.step()['phase'],'held')
        for _ in range(5):
            result=agent.step();self.assertEqual(result['phase'],'held')
            self.assertNotIn('private',str(result))
        self.backend.stage.assert_called_once();self.backend.queue.assert_not_called()

    def test_crash_after_queue_write_is_reconciled_without_duplicate_dispatch(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        def crash_after_write(path,item,identifier):
            self.native['phase']='waiting_empty';raise SystemExit('simulated power loss')
        self.backend.queue.side_effect=crash_after_write
        with self.assertRaises(SystemExit):agent.step()
        self.assertEqual(agent.state()['phase'],'dispatching')
        restarted=Agent(self.host,self.backend,clock=lambda:self.now,jitter=lambda a,b:0)
        self.assertEqual(restarted.step()['phase'],'waiting_empty')
        self.backend.queue.assert_called_once()

    def test_crash_before_queue_write_reuses_same_operation_uuid(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        self.backend.queue.side_effect=SystemExit('before native durable write')
        with self.assertRaises(SystemExit):agent.step()
        expected=agent.state()['operation']['id']
        self.backend.queue.side_effect=lambda *args:self.native.update(phase='waiting_empty')
        restarted=Agent(self.host,self.backend,clock=lambda:self.now,jitter=lambda a,b:0)
        self.assertEqual(restarted.step()['phase'],'waiting_empty')
        self.assertEqual(self.backend.queue.call_args.args[2],expected)

    def test_pause_stops_new_submissions_but_does_not_kill_an_accepted_update(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        self.assertTrue(agent.step('pause')['paused'])
        self.assertEqual(agent.step()['phase'],'paused')
        self.backend.queue.assert_not_called()
        agent.step('resume');agent.step()
        agent.step('pause');agent.step()
        self.backend.queue.assert_called_once()
        self.backend.observe.assert_called()

    def test_hub_outage_uses_backoff_and_never_calls_room_lifecycle(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        with patch.object(self.host,'check',side_effect=OSError('redact endpoint and secrets')):
            result=agent.step();self.assertEqual(result['phase'],'retrying')
            self.assertEqual(result['next_check_at'],self.now+30)
            for _ in range(10):agent.step()
            self.backend.stage.assert_not_called();self.backend.queue.assert_not_called()
            self.assertEqual(agent.state()['failures'],1)

    def test_foreign_native_job_is_never_overwritten(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        self.backend.current.return_value['deployment_busy']=True
        result=agent.step();self.assertEqual(result['error'],'update_other_deployment_pending')
        self.backend.queue.assert_not_called()

    def test_unexpected_native_receipt_is_held_and_no_unknown_state_is_adopted(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True);agent.step()
        self.native['phase']='conflict'
        self.assertEqual(agent.step()['phase'],'held')
        self.backend.queue.assert_called_once()

    def test_controller_updates_require_separate_consent(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        # Publisher classifies actual controller file changes independently of
        # the worker version/title. Re-signing is done through normal publication.
        state=self.publisher
        newer=self.base/'controller.zip';newer.write_bytes(b'controller')
        import hashlib
        sha=hashlib.sha256(newer.read_bytes()).hexdigest()
        spec=copy.deepcopy(self.specs[self.after_sha]);spec['version']='v7.0.4'
        spec['files']['payload/controller.py']='changed';self.specs[sha]=spec
        state.publish(newer,sha,before_bundle=self.before,before_sha256=self.before_sha,execution='offline-fixture')
        self.assertEqual(agent.step()['phase'],'controller_consent_required')
        self.backend.queue.assert_not_called()

    def test_explicit_recovery_retries_only_proven_unsubmitted_stage_with_same_uuid(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        self.backend.stage.side_effect=ConfigError('stage rejected before native queue')
        self.assertEqual(agent.step()['phase'],'held');operation=agent.state()['operation']['id']
        self.backend.retryable.return_value=True;self.backend.stage.side_effect=None
        self.assertEqual(agent.step('recover')['phase'],'waiting_empty')
        self.assertEqual(self.backend.queue.call_args.args[2],operation)
        self.backend.queue.assert_called_once()

    def test_recovery_never_requeues_absent_intent_without_native_qualification(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        self.backend.stage.side_effect=ConfigError('stage failed');agent.step()
        self.backend.retryable.return_value=False
        self.assertEqual(agent.step('recover')['phase'],'held')
        self.backend.queue.assert_not_called()

    def test_failed_native_receipt_requires_native_resolution_before_recovery(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True);agent.step()
        self.native['phase']='failed'
        self.assertEqual(agent.step('recover')['phase'],'held')
        self.backend.queue.assert_called_once();self.backend.retryable.assert_not_called()

    def test_corrupt_operation_identity_cannot_reach_native_path_lookup(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True);agent.step()
        state=agent.state();state['operation']['id']='../../private'
        atomic_json(agent.path,state);self.backend.observe.reset_mock()
        with self.assertRaises(ConfigError):agent.step()
        self.backend.observe.assert_not_called()

    def test_stopped_host_can_download_without_starting_or_queuing_rooms(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        self.backend.current.return_value['service_active']=False
        summary=agent.step();self.assertEqual(summary['phase'],'downloaded')
        self.assertEqual(summary['error'],'update_host_not_running')
        self.backend.stage.assert_not_called();self.backend.queue.assert_not_called()
        self.assertTrue(list((self.host.root/'downloads').glob('*.zip')))

    def test_reviewed_cancel_suppresses_automatic_retry_but_explicit_apply_can_retry(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True);agent.step()
        original=agent.state()['operation']['id']
        self.backend.recover.return_value={'ok':True,'phase':'cancel_requested'}
        self.assertEqual(agent.resolve(original,'cancel')['resolution'],'cancel')
        self.native['phase']='cancelled'
        self.assertEqual(agent.step()['phase'],'idle');self.assertIsNone(agent.state()['operation'])
        self.assertTrue(agent.resolve(original,'cancel')['replayed'])
        self.now+=1900;self.native['phase']='absent'
        self.assertEqual(agent.step()['error'],'update_release_requires_manual_retry')
        self.backend.queue.assert_called_once()
        self.assertEqual(agent.step('apply')['phase'],'waiting_empty')
        self.assertNotEqual(agent.state()['operation']['id'],original)
        self.assertEqual(self.backend.queue.call_count,2)

    def test_cancel_receipt_after_process_loss_is_reviewed_without_requeue(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True);agent.step()
        operation=agent.state()['operation']['id']
        def crash_after_native_cancel(*args):
            self.native['phase']='cancelled';raise SystemExit('lost cancel reply')
        self.backend.recover.side_effect=crash_after_native_cancel
        with self.assertRaises(SystemExit):agent.resolve(operation,'cancel')
        restarted=Agent(self.host,self.backend,clock=lambda:self.now,jitter=lambda a,b:0)
        self.assertEqual(restarted.step()['phase'],'idle')
        self.backend.queue.assert_called_once()
        self.assertEqual(restarted.state()['last_reviewed']['id'],operation)

    def test_recovery_identity_mismatch_never_reaches_native_action(self):
        import uuid
        agent=self.setup_agent(automatic_download=True,automatic_install=True);agent.step()
        with self.assertRaises(ConfigError):agent.resolve(str(uuid.uuid4()),'cancel')
        self.backend.recover.assert_not_called()

    def test_held_operation_can_refresh_expired_approval_without_resubmission(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        self.backend.stage.side_effect=ConfigError('stage failed');agent.step()
        operation=agent.state()['operation']['id']
        self.now+=7*86400+1;self.publisher.renew()
        checked=agent.step('check');self.assertIsNone(checked['error'])
        self.backend.queue.assert_not_called()
        self.backend.stage.side_effect=None;self.backend.retryable.return_value=True
        self.assertEqual(agent.resolve(operation,'retry')['phase'],'waiting_empty')
        self.assertEqual(self.backend.queue.call_args.args[2],operation)

    def test_newer_channel_head_does_not_replace_an_accepted_operation_artifact(self):
        import hashlib
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        self.backend.stage.side_effect=ConfigError('stage failed');agent.step()
        operation=agent.state()['operation']['id'];accepted=agent.state()['operation']['release']
        newer=self.base/'next-controller.zip';newer.write_bytes(b'new controller')
        sha=hashlib.sha256(newer.read_bytes()).hexdigest()
        spec=copy.deepcopy(self.specs[self.after_sha]);spec['version']='v7.0.4';spec['files']['payload/controller.py']='new'
        self.specs[sha]=spec
        self.publisher.publish(newer,sha,before_bundle=self.before,before_sha256=self.before_sha,execution='offline-fixture')
        checked=agent.step('check');self.assertEqual(checked['available_version'],'v7.0.4')
        self.assertEqual(checked['pending_version'],'v7.0.3')
        path,item=self.host.artifact(accepted);self.assertEqual(item,accepted)
        self.backend.stage.side_effect=None;self.backend.retryable.return_value=True
        self.assertEqual(agent.resolve(operation,'retry')['phase'],'waiting_empty')
        self.assertEqual(self.backend.queue.call_args.args[1],accepted)

    def test_install_consent_cannot_be_enabled_without_download_consent(self):
        agent=self.setup_agent()
        with self.assertRaises(ConfigError):configure(self.host,automatic_install=True,automatic_download=False)


if __name__=='__main__':unittest.main()
