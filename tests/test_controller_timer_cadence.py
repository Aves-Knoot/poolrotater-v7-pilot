"""Background scheduling may be slow; fresh ownership and drain remain mandatory."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from contextlib import ExitStack, nullcontext
from pathlib import Path
import copy
import unittest
from unittest.mock import patch

from poolrotater_host import controller_update as update
from poolrotater_host.config import ConfigError


class ControllerCadenceTests(unittest.TestCase):
    def exercise(self, *, now=120, locked_since=60, rows=True, uncertain=False):
        parent={'service_uid':1000}
        target={'prefix':Path('/invented'), 'root':Path('/invented/root')}
        job={'phase':'locking','before':parent,'after':{},'process':{},'configuration':{},
             'rooms':['room1'],'lock_started':0,'locked_since':locked_since,
             'release_approval_required':True}
        observation={'room1':{'status':{'maintenance':{'uncertain':uncertain}}}}if rows else None
        with ExitStack()as stack:
            for name,value in {
                '_binding':(target,{},None,parent),'_control':job,'_process':{},
                '_config_hashes':{},'_rows':observation,'_same_rooms':True,
                '_gated':True,'journal_matches':True,'validate_runtime':{'manifest':{}},
                'file_lease':nullcontext(),
            }.items():stack.enter_context(patch.object(update,name,return_value=value))
            # This fixture has no real service UID or booking database. Keep
            # the production owner switch and booking guard intact by stubbing
            # only their fixture boundary inputs.
            stack.enter_context(patch('poolrotater_host.ratings.operations._identity',
                                     side_effect=lambda uid:nullcontext()))
            stack.enter_context(patch('poolrotater_host.bookings.policy.holds_updates',
                                     return_value=False))
            stack.enter_context(patch('poolrotater_host.pilot_operations._service_state',return_value={'MainPID':1}))
            stack.enter_context(patch('poolrotater_host.updates.approval.require_approved'))
            stack.enter_context(patch.object(update.time,'time',return_value=now))
            stack.enter_context(patch.object(update,'_write'))
            gate=stack.enter_context(patch.object(update,'_gate'))
            commit=stack.enter_context(patch.object(update,'_commit'))
            result=update.step('invented')
            return result,copy.deepcopy(job),gate,commit

    def test_minute_spaced_fresh_observations_can_complete_drained_admission(self):
        result,job,gate,commit=self.exercise()
        self.assertEqual(result['phase'],'committing')
        self.assertEqual(job['commit_started_at'],120)
        self.assertEqual(gate.call_args.args[-1],'committing')
        commit.assert_called_once()

    def test_admission_remains_bounded_inside_the_native_gate_lease(self):
        self.assertLess(update.ADMISSION_SECONDS,update.GATE_SECONDS)
        result,job,gate,commit=self.exercise(now=update.ADMISSION_SECONDS+1)
        self.assertEqual(result['phase'],'waiting_empty')
        self.assertIsNone(gate.call_args.args[-1]);commit.assert_not_called()

    def test_slow_schedule_never_substitutes_for_fresh_roster(self):
        result,job,gate,commit=self.exercise(rows=False)
        self.assertEqual(result['phase'],'waiting_empty');commit.assert_not_called()

    def test_uncertain_maintenance_is_held_despite_extra_time(self):
        result,job,gate,commit=self.exercise(uncertain=True)
        self.assertEqual(result['phase'],'held');commit.assert_not_called()

    def test_full_late_join_drain_is_still_required(self):
        result,job,gate,commit=self.exercise(locked_since=110)
        self.assertEqual(result['phase'],'locking');commit.assert_not_called()


if __name__=='__main__':unittest.main()
