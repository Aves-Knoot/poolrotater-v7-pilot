"""Finite stopped helper coordination, original state and interruption evidence."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy,unittest
from poolrotater_host.config import ConfigError
from poolrotater_host.recreation_repair_helpers import Helpers,TIMERS


def state(active='inactive',enabled='disabled',pid=0,loaded='loaded'):
    return {'LoadState':loaded,'ActiveState':active,'UnitFileState':enabled,'MainPID':pid}

class Ops:
    def __init__(self):
        self.calls=[];self.states={'poolrotater-v7-pilot-two.service':state('failed'),
                                  'poolrotater-v7-network-two.service':state('active','enabled',42)}
        for key in TIMERS:
            base='poolrotater-v7-'+key+'-two'
            self.states[base+'.timer']=state('active','enabled')
            self.states[base+'.service']=state(enabled='static')
    def state(self,name):return copy.deepcopy(self.states[name])
    def systemctl(self,action,name):
        self.calls.append((action,name));row=self.states[name]
        if action=='stop':row.update(ActiveState='inactive',MainPID=0)
        elif action=='start':row['ActiveState']='active'
        elif action=='enable':row['UnitFileState']='enabled'
        elif action=='disable':row['UnitFileState']='disabled'

class Coordination(unittest.TestCase):
    def setUp(self):
        self.ops=Ops();self.now=0
        self.helper=Helpers('two',self.ops,clock=lambda:self.now,sleep=lambda t:setattr(self,'now',self.now+t))
    def test_all_timers_pause_before_network_and_original_states_restore(self):
        snapshot=self.helper.snapshot();original=copy.deepcopy(snapshot)
        self.assertTrue(self.helper.pause(snapshot)['helpers_paused'])
        self.assertEqual(self.ops.calls[-1],('stop','poolrotater-v7-network-two.service'))
        self.assertEqual(len(self.ops.calls),5)
        result=self.helper.restore(snapshot)
        self.assertEqual(set(result['restored']),set(TIMERS)|{'network'})
        self.assertEqual(snapshot,original)
        self.assertFalse(any('pilot-two.service'in unit for _,unit in self.ops.calls))
    def test_executing_helper_finishes_naturally_without_kill(self):
        name='poolrotater-v7-updates-two.service';self.ops.states[name]=state('activating','static',17)
        def finish(delay):
            self.now+=delay;self.ops.states[name]=state(enabled='static')
        self.helper.sleep=finish
        self.helper.pause(self.helper.snapshot())
        self.assertEqual(self.now,1);self.assertNotIn(('stop',name),self.ops.calls)
    def test_persistent_helper_refuses_before_stopping_network(self):
        name='poolrotater-v7-recovery-two.service';self.ops.states[name]=state('active','static',17)
        with self.assertRaisesRegex(ConfigError,'executing_helpers'):
            self.helper.pause(self.helper.snapshot(),timeout=2)
        self.assertEqual(self.now,2)
        self.assertFalse(any('network' in unit for _,unit in self.ops.calls))
        self.assertFalse(any(unit.endswith('.service')for _,unit in self.ops.calls))
    def test_missing_old_timer_installed_during_upgrade_is_left_disabled_stopped(self):
        key='poolrotater-v7-controller-update-two.timer'
        self.ops.states[key]=state(enabled='',loaded='not-found');snapshot=self.helper.snapshot()
        self.helper.pause(snapshot)
        self.ops.states[key]=state('active','enabled')
        self.helper.restore(snapshot)
        self.assertEqual(self.ops.states[key],state())
    def test_deliberately_inactive_helpers_and_network_stay_inactive(self):
        self.ops.states['poolrotater-v7-network-two.service']=state()
        self.ops.states['poolrotater-v7-updates-two.timer']=state('inactive','enabled')
        snapshot=self.helper.snapshot();self.helper.pause(snapshot);self.helper.restore(snapshot)
        self.assertNotIn(('start','poolrotater-v7-network-two.service'),self.ops.calls)
        self.assertNotIn(('start','poolrotater-v7-updates-two.timer'),self.ops.calls)
        self.assertEqual(self.ops.states['poolrotater-v7-updates-two.timer']['UnitFileState'],'enabled')
    def test_running_controller_is_never_stopped(self):
        self.ops.states['poolrotater-v7-pilot-two.service']=state('active','disabled',88)
        with self.assertRaisesRegex(ConfigError,'stopped_bot'):self.helper.snapshot()
        self.assertEqual(self.ops.calls,[])
    def test_snapshot_cannot_be_reused_for_different_instance(self):
        snapshot=self.helper.snapshot();snapshot['instance']='first'
        with self.assertRaisesRegex(ConfigError,'snapshot'):self.helper.pause(snapshot)
        self.assertEqual(self.ops.calls,[])
    def test_failed_start_can_defer_reconciliation_without_disabling_boot_preference(self):
        snapshot=self.helper.snapshot();self.helper.pause(snapshot)
        result=self.helper.restore(snapshot,allow_reconciliation=False)
        self.assertEqual(result['restoration_deferred'],['managed'])
        self.assertEqual(self.ops.states['poolrotater-v7-managed-two.timer'],state('inactive','enabled'))
    def test_interrupted_pause_can_restore_from_retained_snapshot(self):
        snapshot=self.helper.snapshot();self.helper.pause(snapshot)
        fresh=Helpers('two',self.ops)
        fresh.restore(copy.deepcopy(snapshot))
        self.assertEqual(self.ops.states['poolrotater-v7-network-two.service']['ActiveState'],'active')
        for key in TIMERS:self.assertEqual(self.ops.states['poolrotater-v7-'+key+'-two.timer']['ActiveState'],'active')

    def test_network_boot_enablement_is_restored_after_installer_changes_it(self):
        snapshot=self.helper.snapshot();self.helper.pause(snapshot)
        self.ops.states['poolrotater-v7-network-two.service']['UnitFileState']='disabled'
        self.helper.restore(snapshot)
        self.assertIn(('enable','poolrotater-v7-network-two.service'),self.ops.calls)
    def test_unexpected_static_unit_change_is_not_treated_as_preserved_preference(self):
        snapshot=self.helper.snapshot();self.helper.pause(snapshot)
        self.ops.states['poolrotater-v7-managed-two.timer']['UnitFileState']='static'
        with self.assertRaisesRegex(ConfigError,'enablement_requires_review'):self.helper.restore(snapshot)
    def test_silent_enablement_failure_is_not_reported_as_success(self):
        snapshot=self.helper.snapshot();self.helper.pause(snapshot)
        self.ops.states['poolrotater-v7-updates-two.timer']['UnitFileState']='disabled'
        original=self.ops.systemctl
        def fail(action,name):
            if action=='enable':self.ops.calls.append((action,name))
            else:original(action,name)
        self.ops.systemctl=fail
        with self.assertRaisesRegex(ConfigError,'enablement_requires_review'):self.helper.restore(snapshot)
