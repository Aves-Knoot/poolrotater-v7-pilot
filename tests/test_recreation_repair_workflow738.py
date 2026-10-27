"""Workflow integration with real locks/receipt and qualified native boundaries mocked.

Installed artifact/custody/provider behavior still needs full donor qualification.
"""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,tempfile,unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from poolrotater_host.config import ConfigError
from poolrotater_host.layout import read_json
from poolrotater_host.recreation_repair_workflow import run
import test_recreation_repair_helpers738 as helper_tests

class Workflow(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.prefix=Path(temporary.name)
        self.target={'prefix':self.prefix,'root':self.prefix/'runtime','unit':'poolrotater-v7-pilot-two.service'}
        self.record={k:'invented-'+k for k in ('installation_id','generation','host_root','execution')}
        self.record.update(prefix=str(self.prefix),service_uid=1000,execution='pilot-live',bundle_sha256='a'*64)
        self.ops=helper_tests.Ops();self.saved=[];self.ops.save=lambda label,row:self.saved.append((label,copy.deepcopy(row)))
        self.review={'journal_unchanged':True,'journal_sha256':'c'*64,
            'rooms':[{'alias':'room1','result':'local_clean'}],
            'untracked_active_room_ids':[],'untracked_unconfirmed_room_ids':[]}
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        def patched(name,**kw):return self.stack.enter_context(patch(name,**kw))
        self.binding=patched('poolrotater_host.pilot_installation.administrative_binding',side_effect=lambda instance:(self.target,{'installation_id':self.record['installation_id']},dict(self.record)))
        self.quiet=patched('poolrotater_host.pilot_installation.require_no_service_processes')
        self.pending=patched('poolrotater_host.runtime_lifecycle.require_no_pending')
        self.job=patched('poolrotater_host.provisioning._control',return_value=None)
        self.upgrade=patched('poolrotater_host.pilot_upgrade.upgrade',side_effect=self.upgrade_once)
        self.ownership=patched('poolrotater_host.pilot_operations._ownership_stopped',side_effect=lambda *args,**kw:copy.deepcopy(self.review))
        self.start=patched('poolrotater_host.pilot_operations.lifecycle',return_value={'ok':True})
        patched('poolrotater_host.rescue.pin_refresh.refresh_pin',return_value={'status':'current'})
    def upgrade_once(self,*args,**kw):
        self.record['bundle_sha256']='b'*64
        # Exercise the stopped installer's reconciliation rearm, not just no-op upgrade.
        self.ops.states['poolrotater-v7-managed-two.timer'].update(ActiveState='active',UnitFileState='enabled')
        return {'status':'upgraded','operator_state_preserved':True,'service_started':False}
    def execute(self,*,start=True):
        return run('two',self.prefix/'runtime.zip','b'*64,{'a'*64},self.ops,start=start)
    def test_preserved_upgrade_rechecks_ownership_and_starts_once_with_original_intent(self):
        result=self.execute()
        self.assertEqual(result['phase'],'started');self.assertEqual(result['start_attempts'],1)
        self.start.assert_called_once_with('two','start',preserve_managed_intent=True,admission_held=True)
        receipt=read_json(self.prefix/'recreation-repair.json')
        self.assertTrue(receipt['startup_attempted']);self.assertEqual(receipt['phase'],'complete')
        self.assertGreaterEqual(self.quiet.call_count,2)
    def test_unknown_without_id_keeps_original_journal_and_never_starts(self):
        self.review['rooms'][0]['result']='unknown_creation'
        def review(instance,action,*args,**kw):
            if action=='inventory':return {'history_complete':True,'history_stable':True,'absence_recovery_qualified':False,'rooms':[]}
            return copy.deepcopy(self.review)
        self.ownership.side_effect=review
        result=self.execute()
        self.assertEqual(result['phase'],'requires_upstream_evidence');self.start.assert_not_called()
        self.assertEqual([call.args[1]for call in self.ownership.call_args_list],['review','inventory'])
        self.assertFalse(read_json(self.prefix/'recreation-repair.json')['startup_attempted'])
    def test_failed_start_marker_blocks_rerun_without_second_attempt(self):
        self.start.side_effect=ConfigError('injected startup failure')
        with self.assertRaises(ConfigError):self.execute()
        with self.assertRaisesRegex(ConfigError,'already_attempted'):self.execute()
        self.assertEqual(self.start.call_count,1)
    def test_existing_committing_job_is_not_interrupted(self):
        self.job.return_value={'phase':'committing'}
        with self.assertRaisesRegex(ConfigError,'native_update'):self.execute()
        self.assertEqual(self.ops.calls,[]);self.upgrade.assert_not_called();self.start.assert_not_called()
    def test_ready_without_start_keeps_managed_timer_paused_and_boot_preference(self):
        result=self.execute(start=False)
        self.assertEqual(result['phase'],'ready_for_start');self.start.assert_not_called()
        self.assertEqual(self.ops.states['poolrotater-v7-managed-two.timer'],helper_tests.state('inactive','enabled'))
    def test_unknown_inventory_failure_is_not_reported_as_complete_absence(self):
        self.review['rooms'][0]['result']='unknown_creation'
        with self.assertRaisesRegex(ConfigError,'inventory_requires_review'):self.execute()
        self.start.assert_not_called()
    def test_native_prepared_transition_refuses_before_pause(self):
        self.pending.side_effect=ConfigError('native transition prepared')
        with self.assertRaises(ConfigError):self.execute()
        self.assertEqual(self.ops.calls,[]);self.upgrade.assert_not_called()

    def test_upgrade_error_after_timer_rearm_cannot_release_an_active_reconciler(self):
        def fail(*args,**kwargs):
            self.upgrade_once(*args,**kwargs)
            raise ConfigError('injected after commit')
        self.upgrade.side_effect=fail
        with self.assertRaises(ConfigError):self.execute()
        self.assertEqual(self.ops.states['poolrotater-v7-managed-two.timer']['ActiveState'],'inactive')
        self.start.assert_not_called()
