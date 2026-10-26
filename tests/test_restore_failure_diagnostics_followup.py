"""Guard refusals stay refusals while their bounded cause becomes observable."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from poolrotater_host import controller_update, update_drain
from poolrotater_host.config import ConfigError
from poolrotater_host.network.contracts import NetworkError
import test_update_drain733 as _fixtures

assert Path(update_drain.__file__).resolve().parents[1]==ROOT


class RestorationFailureCodeTests(unittest.TestCase):
    def test_only_closed_single_codes_are_emitted(self):
        known='update_drain_restore_running_identity_changed'
        cases=[(NetworkError(known),known),(ConfigError(known),known),
            (NetworkError('private-token=DO_NOT_EMIT'),'update_guard_requires_review'),
            (ConfigError('private-token=DO_NOT_EMIT'),'update_guard_requires_review'),
            (ConfigError(known,'DO_NOT_EMIT'),'update_guard_requires_review'),
            (OSError('DO_NOT_EMIT /private/path'),'update_guard_requires_review')]
        for error,wanted in cases:
            with self.subTest(kind=type(error).__name__,wanted=wanted):
                code=update_drain.restoration_failure_code(error)
                self.assertEqual(code,wanted)
                self.assertNotIn('DO_NOT_EMIT',code)

    def test_restore_preserves_old_result_shapes_and_masks_unknown_errors(self):
        job={update_drain.RECORD:{'request_id':'invented'}}
        target={'root':Path('/unused'),'prefix':Path('/unused')}
        for error,wanted in [
            (NetworkError('update_drain_request_binding_changed'),
             'update_drain_request_binding_changed'),
            (ConfigError('update_recovery_configuration_changed'),
             'update_recovery_configuration_changed'),
            (ConfigError('DO_NOT_EMIT password'),'update_guard_requires_review')]:
            observed=[]
            with patch.object(update_drain,'_validate_drain',side_effect=error):
                original=update_drain.restore(target,{},job,now=1.)
                diagnosed=update_drain.restore(target,{},job,now=1.,diagnostic=observed.append)
            self.assertEqual(original,diagnosed)
            self.assertEqual(diagnosed,{'phase':'requires_review','request_id':'invented','rooms':{}})
            self.assertEqual(observed,[wanted])

    def test_stale_waits_do_not_become_failure_or_retry_authority(self):
        for code in ('update_drain_status_stale_or_incomplete',
                     'update_drain_pause_status_stale','update_drain_receipt_stale'):
            observed=[]
            with patch.object(update_drain,'_validate_drain',side_effect=NetworkError(code)):
                result=update_drain.restore({}, {},
                    {update_drain.RECORD:{'request_id':'invented'}},now=1.,diagnostic=observed.append)
            self.assertEqual(result,{'phase':'waiting','request_id':'invented','rooms':{}})
            self.assertEqual(observed,[])

    def test_broken_diagnostic_sink_cannot_change_refusal(self):
        with patch.object(update_drain,'_validate_drain',
                          side_effect=NetworkError('update_drain_request_binding_changed')):
            result=update_drain.restore({}, {},
                {update_drain.RECORD:{'request_id':'invented'}},now=1.,
                diagnostic=lambda _code: (_ for _ in ()).throw(RuntimeError('private sink failure')))
        self.assertEqual(result,{'phase':'requires_review','request_id':'invented','rooms':{}})


class ChangedRunningIdentityDiagnosticTests(unittest.TestCase):
    setUp=_fixtures.UpdateDrainRestoreTests.setUp

    def test_recreated_sibling_still_refuses_without_dispatching_restore_open(self):
        from poolrotater_host.managed_host import policy
        from poolrotater_host.diagnostic_logs.common import transaction
        from poolrotater_host.layout import read_json,atomic_json
        store=policy.state(self.root)
        old=self.drain['initial']['room2']['identity']
        new_room=old['room_id']+100
        # Model a normal native recreation: same worker, clean closure followed
        # by a positively owned replacement. This does NOT relax the guard.
        store.confirmed_closed('room2',old['generation'],old['room_id'])
        store.reserve('room2',old['generation'])
        store.mark_dispatched('room2',old['generation'])
        store.observe_owned('room2',old['generation'],new_room)
        for name in ('status.json','ownership.json'):
            path=self.root/'state/rolling'/name
            value=read_json(path);value['rooms']['room2']['room_id']=new_room
            if name=='status.json':value['rooms']['room2']['status']['room_id']=new_room
            atomic_json(path,value)
        observed=[];native_restore=update_drain.restore
        def diagnosed(*args,**kwargs):
            return native_restore(*args,**kwargs,diagnostic=observed.append)
        with patch.object(update_drain,'restore',side_effect=diagnosed):
            result=_fixtures.UpdateDrainRestoreTests.restore(self)
        self.assertEqual(result['phase'],'requires_review')
        self.assertEqual(observed,['update_drain_restore_running_identity_changed'])
        with transaction(store.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM operations').fetchone()[0],0)


class RootJobRestorationDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.target={'root':Path(self.temp.name),'prefix':Path(self.temp.name)/'prefix'}
        (self.target['root']/'config').mkdir()
        self.parent={'service_uid':1000}
        self.job={'phase':'cancelled','before':self.parent,'configuration':{},
            update_drain.RECORD:{'request_id':'invented'},
            'failure_code':'existing_commit_code','failure_stage':'configuration'}

    def run_restore(self,result,code=None):
        def restore(*args,diagnostic=None,**kwargs):
            if code is not None:diagnostic(code)
            return result
        def save(**values):self.job.update(values)
        with patch.object(update_drain,'cancel',return_value={'phase':'complete'}), \
             patch.object(update_drain,'restore',side_effect=restore), \
             patch.object(controller_update,'_configuration_unprepared',return_value=True), \
             patch.object(controller_update,'_commit') as commit:
            outcome=controller_update._restore_canceled_drain('invented',self.target,
                self.parent,self.job,save)
        commit.assert_not_called()
        return outcome

    def test_only_root_diagnostic_is_added_and_authority_fields_stay_unchanged(self):
        code='update_drain_restore_running_identity_changed'
        original={k:self.job[k] for k in ('phase','failure_code','failure_stage')}
        self.assertTrue(self.run_restore({'phase':'requires_review'},code))
        self.assertEqual(self.job['drain_restoration_phase'],'requires_review')
        self.assertEqual(self.job['drain_restoration_failure_code'],code)
        self.assertEqual({k:self.job[k] for k in original},original)
        self.assertFalse(controller_update._drain_restoration_needed(self.job))

    def test_old_result_without_reason_is_generic_and_cannot_enable_retry(self):
        self.assertTrue(self.run_restore({'phase':'requires_review'}))
        self.assertEqual(self.job['drain_restoration_failure_code'],'update_guard_requires_review')
        self.assertFalse(controller_update._drain_restoration_needed(self.job))

    def test_unrecognized_sink_value_cannot_enter_root_job(self):
        self.assertTrue(self.run_restore({'phase':'requires_review'},'DO_NOT_EMIT private token'))
        self.assertEqual(self.job['drain_restoration_failure_code'],'update_guard_requires_review')

    def test_waiting_and_success_clear_only_stale_diagnostic(self):
        for phase in ('waiting','complete'):
            self.job.pop('drain_restoration_phase',None)
            self.job['drain_restoration_failure_code']='update_guard_requires_review'
            self.assertTrue(self.run_restore({'phase':phase}))
            self.assertIsNone(self.job['drain_restoration_failure_code'])
            self.assertEqual(self.job['failure_code'],'existing_commit_code')

    def test_foreign_registration_remains_refused_before_restore(self):
        def save(**values):self.job.update(values)
        with patch.object(update_drain,'restore') as restore:
            self.assertTrue(controller_update._restore_canceled_drain('invented',self.target,
                {'different':True},self.job,save))
        restore.assert_not_called()
        self.assertEqual(self.job['drain_restoration_failure_code'],
                         'update_recovery_registration_changed')


if __name__=='__main__':unittest.main()
