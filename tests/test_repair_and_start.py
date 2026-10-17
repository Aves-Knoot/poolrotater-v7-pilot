"""Operator repair ordering, refusal boundaries and exactly-once startup."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

from pilot import repair_and_start as repair


class FakeOps:
    def __init__(self):
        self.calls=[];self.record=None;self.active=False;self.updater_busy=False
        self.clean=False;self.foreign=False;self.fail_start=False;self.granted=True
    def save(self,*args):pass
    def state(self,name):
        active=self.active if name=='bot.service' else self.updater_busy if name.endswith('updates-two.service') else False
        return {'LoadState':'loaded','ActiveState':'active' if active else 'failed' if name=='bot.service' else 'inactive',
                'MainPID':123 if active else 0,'UnitFileState':'disabled'}
    def systemctl(self,*args):self.calls.append(('systemctl',*args))
    def admin(self,action,*args):
        self.calls.append((action,*args))
        if action=='ownership-review':
            return {'journal_sha256':'c'*64,'journal_unchanged':True,'untracked_active_room_ids':[999] if self.foreign else [],
                'untracked_unconfirmed_room_ids':[], 'rooms':[{'alias':'room1','result':'local_clean'},
                {'alias':'room2','result':'local_clean' if self.clean else 'unknown_creation'}]}
        if action=='ownership-clear':
            self.clean=True;return {'phase':'committed','recovery_applied':True}
        if action=='network-status':return {'enrolled':self.granted,'pending_operation':None}
        if action=='start' and self.fail_start:raise RuntimeError('invented uncertain startup')
        return {'ok':True}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.old='a'*64;self.new='b'*64;self.ops=FakeOps()
        self.args=SimpleNamespace(instance='two',room_alias='room2',created_room_id=4716142,start=True,resume_agents=True)
        self.release={'runtime_sha256':self.new,'supported_predecessors':[{'sha256':self.old}]}
        self.record={'bundle_sha256':self.old,'release':'/invented/old'}
        self.target={'unit':'bot.service'}
        self.installation=SimpleNamespace(administrative_binding=lambda _: (self.target,{},self.record))
        def upgrade(*args,**kwargs):
            self.ops.calls.append(('upgrade',kwargs['from_sha256']))
            self.record.update(bundle_sha256=self.new,release='/invented/new')
            return {'status':'upgraded','service_started':False}
        self.upgrade=SimpleNamespace(upgrade=upgrade)
    def run_workflow(self):
        return repair.run(self.args,Path('/invented/kit'),self.release,self.ops,self.installation,self.upgrade)
    def test_upgrade_fresh_identity_recovery_then_exactly_one_start_and_existing_agents(self):
        result=self.run_workflow();names=[x[0] for x in self.ops.calls]
        self.assertEqual(result['phase'],'started');self.assertEqual(names.count('start'),1)
        self.assertLess(names.index('upgrade'),names.index('ownership-clear'))
        self.assertLess(names.index('ownership-clear'),names.index('start'))
        clear=next(x for x in self.ops.calls if x[0]=='ownership-clear')
        self.assertIn('c'*64,clear);self.assertIn('4716142',clear);self.assertIn('--confirm-lost-creation',clear)
        self.assertEqual(result['agents_started'],['network','update_polling'])
        self.assertFalse(any(x[0]=='systemctl' and x[1] in ('enable','disable') for x in self.ops.calls))
    def test_occupied_or_running_old_controller_is_not_stopped(self):
        self.ops.active=True
        with self.assertRaises(ValueError):self.run_workflow()
        self.assertFalse(self.ops.calls)
    def test_rerun_against_current_active_controller_is_read_only(self):
        self.ops.active=True;self.record['bundle_sha256']=self.new
        result=self.run_workflow();self.assertEqual(result['phase'],'already_running')
        self.assertFalse(self.ops.calls)
    def test_inflight_updater_is_not_killed_or_upgraded(self):
        self.ops.updater_busy=True
        with self.assertRaises(ValueError):self.run_workflow()
        self.assertEqual(self.ops.calls,[('systemctl','stop','poolrotater-v7-updates-two.timer')])
    def test_foreign_or_untracked_room_blocks_recovery_and_start(self):
        self.ops.foreign=True
        with self.assertRaises(ValueError):self.run_workflow()
        self.assertFalse(any(x[0] in ('ownership-clear','start') for x in self.ops.calls))
    def test_unknown_creation_needs_the_explicit_correct_alias(self):
        self.args.room_alias='room1'
        with self.assertRaises(ValueError):self.run_workflow()
        self.assertFalse(any(x[0] in ('ownership-clear','start') for x in self.ops.calls))
    def test_uncertain_start_is_attempted_once_without_starting_agents(self):
        self.ops.fail_start=True
        with self.assertRaises(RuntimeError):self.run_workflow()
        self.assertEqual(sum(x[0]=='start' for x in self.ops.calls),1)
        self.assertFalse(any(x[0]=='network-start' for x in self.ops.calls))
    def test_upgrade_without_start_leaves_the_room_service_stopped(self):
        self.args.start=False;self.args.resume_agents=False
        result=self.run_workflow();self.assertEqual(result['phase'],'ready_for_start')
        self.assertFalse(any(x[0] in ('start','network-start') for x in self.ops.calls))
    def test_clean_journal_does_not_repeat_historical_clear(self):
        self.ops.clean=True;self.run_workflow()
        self.assertFalse(any(x[0]=='ownership-clear' for x in self.ops.calls))
    def test_unrecognized_predecessor_is_not_replaced(self):
        self.record['bundle_sha256']='d'*64
        with self.assertRaises(ValueError):self.run_workflow()
        self.assertFalse(any(x[0] in ('upgrade','ownership-clear','start') for x in self.ops.calls))
