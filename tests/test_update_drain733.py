"""Strict automatic drain admission over the existing maintenance mailbox."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from contextlib import contextmanager
import copy
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from poolrotater_host.layout import atomic_json, read_json, setup, load
from poolrotater_host.network.contracts import fingerprint
from poolrotater_host.operator_maintenance import NAME, RECEIPT, validate as validate_maintenance
from poolrotater_host import update_drain


def binding(root,prefix):
    marker={'format':1,'installation_id':str(uuid.uuid4()),'generation':str(uuid.uuid4())}
    parent={**marker,'bundle_sha256':'a'*64,'prefix':str(prefix),'host_root':str(root),
            'service_uid':1000,'release':str(prefix/'releases'/('v7.3.2-'+'b'*16)),
            'execution':'pilot-live'}
    job={'format':1,'id':str(uuid.uuid4()),'phase':'waiting_empty','before':parent,
         'after':{'bundle_sha256':'c'*64},'rooms':['room1','room2'],'process':
         {'pid':1001,'start':'start','boot':'boot'},'configuration':{'config/host.json':'d'*64}}
    return marker,parent,job


def initial_alias():
    return {'identity':{'pid':101,'generation':'worker-generation','room_id':501,
        'release':'/prefix/releases/'+('v7.3.2-'+'b'*16)},
        'managed':{'desired':'running','revision':4,'phase':'running','room_id':501,
                   'room_generation':'managed-generation','pending_operation':None,
                   'preset':'standard-fixed','visibility':'public'},
        'reservation':{'generation':'worker-generation','room_id':501,'phase':'owned','operation':None},
        'closure_started_at':None}


class UpdateDrainRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'host';setup(self.root)
        (self.root/'state/rolling').mkdir(mode=0o700,parents=True,exist_ok=True)
        self.prefix=Path(self.tmp.name)/'prefix';self.prefix.mkdir(mode=0o700)
        self.marker,self.parent,self.job=binding(self.root,self.prefix)
        self.initial={'room1':initial_alias()}
        self.projection={'format':1,'installation_id':self.marker['installation_id'],
            'generation':self.marker['generation'],'rooms':{
                'room1':{'revision':4,'desired':'running','settings':{'preset':'standard-fixed','visibility':'public'}},
                'room2':{'revision':0,'desired':'stopped','settings':{'preset':'standard-fixed','visibility':'public'}}}}
        self.record={'format':1,'job_id':self.job['id'],
            'request_id':str(uuid.uuid5(uuid.UUID(self.job['id']),'poolrotater-empty-room-drain-v1')),
            'installation_id':self.parent['installation_id'],'generation':self.parent['generation'],
            'parent_sha256':self.parent['bundle_sha256'],'mode':'empty','rooms':['room1'],
            'created_at':1000.0,'expires_at':1000.0+86400,
            'configuration_sha256':fingerprint(self.job['configuration']),
            'projection_sha256':fingerprint(self.projection),'eligibility_sha256':'f'*64,
            'projection':self.projection,'initial':self.initial}
        self.saved=[]
        def save(**values):self.job.update(values);self.saved.append(values)
        self.save=save

    @staticmethod
    def _fixture_control(path,owner=0,*,private=False,optional=False,**kwargs):
        path=Path(path)
        if not path.exists():
            if optional:return None
            raise FileNotFoundError(path)
        return json.loads(path.read_text())

    def request(self,*,gate=True,collect=None):
        def control(path,*args,**kwargs):
            path=Path(path)
            if not path.exists():
                if kwargs.get('optional'):return None
                raise FileNotFoundError(path)
            return json.loads(path.read_text())
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_control',side_effect=control), \
             patch.object(update_drain,'_write',side_effect=lambda path,value:atomic_json(path,value)), \
             patch.object(update_drain,'_configuration_sha',return_value=self.record['configuration_sha256']), \
             patch.object(update_drain,'_collect',return_value=(['room1'],self.initial,self.record['projection_sha256'],self.record['eligibility_sha256'],self.projection)) as selected, \
             patch('poolrotater_host.controller_update._gate',return_value=gate) as gate_call:
            value=update_drain.request({'root':self.root,'prefix':self.prefix},self.parent,self.job,
                                       self.save,now=lambda:1000.0)
        return value,selected,gate_call

    def test_request_is_deterministic_empty_only_and_idempotent(self):
        first,selected,gate=self.request()
        self.assertEqual(first,{'phase':'requested','request_id':self.record['request_id'],'rooms':['room1']})
        mailbox=read_json(self.prefix/NAME)
        self.assertEqual(mailbox['mode'],'empty')
        self.assertEqual(mailbox['rooms'],['room1'])
        validate_maintenance(mailbox,self.parent)
        saved=self.prefix.joinpath(NAME).read_bytes()
        second,_,_=self.request()
        self.assertEqual(second['request_id'],first['request_id'])
        self.assertEqual((self.prefix/NAME).read_bytes(),saved)
        self.assertEqual(gate.call_count,2)
        self.assertEqual(selected.call_count,2)

    def _round_zero_terminal_pair(self,*,old_parent=False,active=False,
                                  mailbox_phase=None,receipt_phase=None):
        old_id=str(uuid.uuid4())
        (self.root/'state/rolling').mkdir(mode=0o700,parents=True,exist_ok=True)
        old_parent_sha='e'*64 if old_parent else self.parent['bundle_sha256']
        mailbox_phase=mailbox_phase or ('requested' if active else 'cancelled')
        receipt_phase=receipt_phase or ('draining' if active else 'cancelled')
        mailbox={'format':1,'id':old_id,'installation_id':self.parent['installation_id'],
            'generation':self.parent['generation'],'parent_sha256':old_parent_sha,
            'phase':mailbox_phase,'mode':'empty','rooms':['room1'],
            'created_at':800.0,'expires_at':900.0}
        receipt={'format':1,'id':old_id,'phase':receipt_phase,'mode':'empty',
            'controller_pid':self.job['process']['pid'],'updated_at':950.0,
            'rooms':{'room1':'request_delivered' if receipt_phase=='draining'
                else 'cancelled' if receipt_phase=='cancelled' else 'closed'}}
        atomic_json(self.prefix/NAME,mailbox);atomic_json(self.root/RECEIPT,receipt)
        return mailbox,receipt

    def _round_zero_unpublished_gate(self,*,closure_evidence=False):
        from poolrotater_host.controller_update import GATE
        mailbox,receipt=self._round_zero_terminal_pair()
        if closure_evidence:
            receipt['closure_evidence']={'room1':{'room_id':501,'mode':'empty',
                'notice_confirmed':False,'active_map_observed':False,'require_empty':True,
                'closure_started_at':940.0}}
            atomic_json(self.root/RECEIPT,receipt)
        self.job['nonce']='a'*32
        with patch.object(update_drain,'_control',side_effect=self._fixture_control):
            baseline=update_drain._pre_gate_baseline(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job,self.job['nonce'])
        self.job[update_drain.GATE_BASELINE]=baseline
        gate_path=self.root/GATE;gate_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        atomic_json(gate_path,{'format':1,'phase':'locking','nonce':self.job['nonce'],
            'expires_at':time.time()+300,'bridge_release':self.parent['release']})
        return mailbox,receipt,baseline,gate_path

    def _round_one_archived_terminal_pair(self):
        previous=copy.deepcopy(self.record);previous['round']=0
        previous_id=previous['request_id']
        previous_mailbox={'format':1,'id':previous_id,
            'installation_id':self.parent['installation_id'],'generation':self.parent['generation'],
            'parent_sha256':self.parent['bundle_sha256'],'phase':'cancelled','mode':'empty',
            'rooms':['room1'],'created_at':previous['created_at'],'expires_at':previous['expires_at']}
        terminal={'format':1,'id':previous_id,'phase':'cancelled','mode':'empty',
            'controller_pid':self.job['process']['pid'],'updated_at':previous['expires_at']+50,
            'rooms':{'room1':'closed'},'closure_evidence':{'room1':{
                'room_id':501,'mode':'empty','notice_confirmed':False,
                'active_map_observed':False,'require_empty':True,
                'closure_started_at':previous['created_at']+1}}}
        archive={'format':1,'job_id':self.job['id'],'round':0,'request_id':previous_id,
            'drain':previous,'drain_sha256':fingerprint(previous),
            'terminal_receipt':terminal,'terminal_receipt_sha256':fingerprint(terminal)}
        history=self.prefix/'round0-history.json';self.round_history_path=history
        atomic_json(history,archive)
        (self.root/'state/rolling').mkdir(mode=0o700,parents=True,exist_ok=True)
        current={**previous,'round':1,
            'request_id':update_drain._request_id(self.job['id'],1)}
        self.job['controller_update_drain_round']=1
        self.job[update_drain.RECORD]=current
        atomic_json(self.prefix/NAME,previous_mailbox)
        atomic_json(self.root/RECEIPT,terminal)
        return previous,previous_mailbox,terminal,archive,history,current

    def test_round_one_history_accepts_only_monotonic_timestamp_refresh(self):
        previous,_mailbox,terminal,archive,history,_current=self._round_one_archived_terminal_pair()
        progress={**terminal,'updated_at':terminal['updated_at']+600}
        atomic_json(self.root/RECEIPT,progress)
        with patch.object(update_drain,'_history_file',return_value=history), \
             patch.object(update_drain,'_control',side_effect=self._fixture_control):
            self.assertTrue(update_drain._history_matches(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job,terminal['id'],progress))
            self.assertEqual(update_drain._history_receipt_pin(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job,terminal['id'],progress),
                archive['terminal_receipt_sha256'])
        self.assertGreater(progress['updated_at'],previous['expires_at'])

        attacks=[]
        attacks.append({**progress,'id':str(uuid.uuid4())})
        attacks.append({**progress,'phase':'complete'})
        attacks.append({**progress,'controller_pid':progress['controller_pid']+1})
        attacks.append({**progress,'rooms':{'room1':'cancelled'}})
        attacks.append({**progress,'closure_evidence':{'room1':{
            **progress['closure_evidence']['room1'],'room_id':502}}})
        attacks.append({**progress,'failures':{'room1':'closed_with_error'}})
        attacks.append({**progress,'updated_at':terminal['updated_at']-1})
        attacks.append({**progress,'updated_at':time.time()+600})
        attacks.append({**progress,'updated_at':float('nan')})
        attacks.append({**progress,'unexpected':'changed'})
        for attack in attacks:
            with self.subTest(attack=attack):
                with patch.object(update_drain,'_history_file',return_value=history), \
                     patch.object(update_drain,'_control',side_effect=self._fixture_control):
                    self.assertFalse(update_drain._history_matches(
                        {'root':self.root,'prefix':self.prefix},self.parent,self.job,terminal['id'],attack))

        changed_archive=copy.deepcopy(archive)
        changed_archive['terminal_receipt']['rooms']['room1']='cancelled'
        atomic_json(history,changed_archive)
        with patch.object(update_drain,'_history_file',return_value=history), \
             patch.object(update_drain,'_control',side_effect=self._fixture_control):
            self.assertFalse(update_drain._history_matches(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job,terminal['id'],progress))

    def test_round_one_timestamp_refresh_keeps_gate_baseline_and_retry_safe(self):
        previous,mailbox,terminal,_archive,_history,current=self._round_one_archived_terminal_pair()
        nonce='f'*32;self.job['nonce']=nonce
        with patch.object(update_drain,'_history_file',return_value=self.round_history_path), \
             patch.object(update_drain,'_control',side_effect=self._fixture_control):
            baseline=update_drain._pre_gate_baseline(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job,nonce)
        self.assertIsNotNone(baseline)
        archived=read_json(self.round_history_path)
        self.assertEqual(baseline['receipt_sha256'],archived['terminal_receipt_sha256'])

        refreshed={**terminal,'updated_at':terminal['updated_at']+600}
        self.assertNotEqual(baseline['receipt_sha256'],fingerprint(refreshed))
        atomic_json(self.root/RECEIPT,refreshed)
        with patch.object(update_drain,'_history_file',return_value=self.round_history_path), \
             patch.object(update_drain,'_control',side_effect=self._fixture_control):
            again=update_drain._pre_gate_baseline(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job,nonce)
        self.assertEqual(again,baseline)

        from poolrotater_host.controller_update import GATE
        gate_path=self.root/GATE;gate_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        atomic_json(gate_path,{'format':1,'phase':'locking','nonce':nonce,
            'expires_at':time.time()+300,'bridge_release':self.parent['release']})
        self.job[update_drain.GATE_BASELINE]=baseline
        self.job.pop(update_drain.RECORD,None)  # interrupted after locking, before mailbox write
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch('poolrotater_host.controller_update._configuration_unprepared',return_value=True), \
             patch('poolrotater_host.controller_update._gate',return_value=True) as gate, \
             patch.object(update_drain,'_history_file',return_value=self.round_history_path), \
             patch.object(update_drain,'_control',side_effect=self._fixture_control):
            self.assertTrue(update_drain.release_unpublished_gate(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job))
        gate.assert_called_once_with({'root':self.root,'prefix':self.prefix},self.parent,self.job,None)
        with patch.object(update_drain,'_history_file',return_value=self.round_history_path), \
             patch.object(update_drain,'_control',side_effect=self._fixture_control):
            self.assertEqual(update_drain._pre_gate_baseline(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job,nonce),baseline)

        self.job[update_drain.RECORD]=current
        new_mailbox={'format':1,'id':current['request_id'],
            'installation_id':self.parent['installation_id'],'generation':self.parent['generation'],
            'parent_sha256':self.parent['bundle_sha256'],'phase':'requested','mode':'empty',
            'rooms':current['rooms'],'created_at':current['created_at'],'expires_at':current['expires_at']}
        self.job[update_drain.GATE_BASELINE]=baseline
        with patch.object(update_drain,'_history_file',return_value=self.round_history_path), \
             patch.object(update_drain,'_control',side_effect=self._fixture_control):
            self.assertTrue(update_drain._pre_gate_terminal_matches(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job,new_mailbox,refreshed))
        changed={**refreshed,'rooms':{'room1':'cancelled'}}
        with patch.object(update_drain,'_history_file',return_value=self.round_history_path), \
             patch.object(update_drain,'_control',side_effect=self._fixture_control):
            self.assertFalse(update_drain._pre_gate_terminal_matches(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job,new_mailbox,changed))

    def test_round_zero_replaces_pinned_terminal_pair_and_replays_before_new_receipt(self):
        cases=(('cancelled','cancelled'),('requested','complete'),('requested','expired'))
        for index,(mailbox_phase,receipt_phase) in enumerate(cases):
            if index:self.setUp()
            with self.subTest(mailbox_phase=mailbox_phase,receipt_phase=receipt_phase):
                old_mailbox,old_receipt=self._round_zero_terminal_pair(
                    mailbox_phase=mailbox_phase,receipt_phase=receipt_phase)
                first,_,_=self.request()
                self.assertEqual(first['phase'],'requested')
                current=read_json(self.prefix/NAME)
                self.assertEqual(current['id'],self.record['request_id'])
                self.assertNotEqual(current['id'],old_mailbox['id'])
                self.assertEqual(read_json(self.root/RECEIPT),old_receipt)
                baseline=self.job[update_drain.GATE_BASELINE]
                self.assertEqual(baseline['mailbox_sha256'],fingerprint(old_mailbox))
                self.assertEqual(baseline['receipt_sha256'],fingerprint(old_receipt))

                # The native controller has not polled the replacement yet.
                # Replay accepts only the old receipt pinned before the gate.
                second,_,_=self.request()
                self.assertEqual(second['request_id'],first['request_id'])
                self.assertEqual(read_json(self.root/RECEIPT),old_receipt)

                new_receipt={'format':1,'id':current['id'],'phase':'draining','mode':'empty',
                    'controller_pid':self.job['process']['pid'],'updated_at':1001.0,
                    'rooms':{'room1':'request_delivered'}}
                atomic_json(self.root/RECEIPT,new_receipt)
                third,_,_=self.request()
                self.assertEqual(third['request_id'],first['request_id'])
                self.assertEqual(third['phase'],'draining')

    def test_round_zero_replay_allows_timestamp_refresh_but_not_semantic_change(self):
        _,old_receipt=self._round_zero_terminal_pair()
        first,_,_=self.request()
        self.assertEqual(first['phase'],'requested')
        changed={**old_receipt,'updated_at':old_receipt['updated_at']+1}
        atomic_json(self.root/RECEIPT,changed)
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_control',side_effect=self._fixture_control), \
             patch.object(update_drain,'_configuration_sha',return_value=fingerprint(self.job['configuration'])), \
             patch('poolrotater_host.controller_update._gate',return_value=True):
            replay=update_drain.request({'root':self.root,'prefix':self.prefix},self.parent,self.job,
                                        self.save,now=lambda:1000.0)
        self.assertEqual(replay['request_id'],first['request_id'])
        semantic_change={**changed,'rooms':{'room1':'closed'}}
        atomic_json(self.root/RECEIPT,semantic_change)
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_control',side_effect=self._fixture_control), \
             patch.object(update_drain,'_configuration_sha',return_value=fingerprint(self.job['configuration'])), \
             patch('poolrotater_host.controller_update._gate') as gate, \
             patch.object(update_drain,'_write') as write:
            with self.assertRaisesRegex(Exception,'update_drain_unrelated_maintenance_pending'):
                update_drain.request({'root':self.root,'prefix':self.prefix},self.parent,self.job,
                                     self.save,now=lambda:1000.0)
        gate.assert_not_called();write.assert_not_called()

    def test_round_zero_unpublished_gate_releases_after_timestamp_refresh(self):
        _,receipt,baseline,_gate_path=self._round_zero_unpublished_gate(closure_evidence=True)
        refreshed={**receipt,'updated_at':receipt['updated_at']+1}
        atomic_json(self.root/RECEIPT,refreshed)
        with patch.object(update_drain,'_control',side_effect=self._fixture_control), \
             patch.object(update_drain,'_process_matches',return_value=True), \
             patch('poolrotater_host.controller_update._configuration_unprepared',return_value=True), \
             patch('poolrotater_host.controller_update._gate',return_value=True) as gate_release:
            self.assertTrue(update_drain.release_unpublished_gate(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job))
        gate_release.assert_called_once()
        self.assertEqual(self.job[update_drain.GATE_BASELINE],baseline)

    def test_round_zero_unpublished_gate_refuses_changed_receipt_or_untrusted_baseline(self):
        def changed_room(value):value['rooms']={'room1':'closed'}
        def changed_closure(value):value['closure_evidence']['room1']['room_id']=502
        def changed_extra(value):value['unexpected']='value'
        attacks=(
            lambda value:value.update(updated_at=value['updated_at']-1),
            lambda value:value.update(updated_at=time.time()+600),
            lambda value:value.update(updated_at=float('nan')),
            lambda value:value.update(controller_pid=value['controller_pid']+1),
            lambda value:value.update(rooms={'room2':'cancelled'}),
            changed_room,changed_closure,changed_extra,
        )
        for index,attack in enumerate(attacks):
            if index:self.setUp()
            with self.subTest(index=index):
                _mailbox,receipt,_baseline,_gate_path=self._round_zero_unpublished_gate(
                    closure_evidence=True)
                mutated=copy.deepcopy(receipt);attack(mutated)
                if index==2:
                    (self.root/RECEIPT).write_text(json.dumps(mutated,allow_nan=True))
                else:atomic_json(self.root/RECEIPT,mutated)
                with patch.object(update_drain,'_control',side_effect=self._fixture_control), \
                     patch.object(update_drain,'_process_matches',return_value=True), \
                     patch('poolrotater_host.controller_update._configuration_unprepared',return_value=True), \
                     patch('poolrotater_host.controller_update._gate') as gate_release:
                    self.assertFalse(update_drain.release_unpublished_gate(
                        {'root':self.root,'prefix':self.prefix},self.parent,self.job))
                gate_release.assert_not_called()
        for index in range(2):
            if index:self.setUp()
            with self.subTest(baseline=index):
                self._round_zero_unpublished_gate(closure_evidence=True)
                if index==0:self.job.pop(update_drain.GATE_BASELINE)
                else:self.job[update_drain.GATE_BASELINE]['receipt_semantic_sha256']='f'*64
                with patch.object(update_drain,'_control',side_effect=self._fixture_control), \
                     patch.object(update_drain,'_process_matches',return_value=True), \
                     patch('poolrotater_host.controller_update._configuration_unprepared',return_value=True), \
                     patch('poolrotater_host.controller_update._gate') as gate_release:
                    self.assertFalse(update_drain.release_unpublished_gate(
                        {'root':self.root,'prefix':self.prefix},self.parent,self.job))
                gate_release.assert_not_called()

    def test_round_zero_accepts_only_same_installation_old_parent_terminal_pair(self):
        old_mailbox,old_receipt=self._round_zero_terminal_pair(old_parent=True)
        first,_,_=self.request()
        self.assertEqual(first['phase'],'requested')
        baseline=self.job[update_drain.GATE_BASELINE]
        self.assertEqual(baseline['mailbox_id'],old_mailbox['id'])
        self.assertEqual(baseline['receipt_sha256'],fingerprint(old_receipt))
        second,_,_=self.request()
        self.assertEqual(second['request_id'],first['request_id'])

    def test_round_zero_rejects_active_old_parent_mailbox_before_gate(self):
        self._round_zero_terminal_pair(old_parent=True,active=True)
        def read_control(path,*args,**kwargs):
            path=Path(path)
            if not path.exists():
                if kwargs.get('optional'):return None
                raise FileNotFoundError(path)
            return json.loads(path.read_text())
        collected=(['room1'],self.initial,self.record['projection_sha256'],
                   self.record['eligibility_sha256'],self.projection)
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_control',side_effect=read_control), \
             patch.object(update_drain,'_configuration_sha',return_value=self.record['configuration_sha256']), \
             patch.object(update_drain,'_collect',return_value=collected), \
             patch('poolrotater_host.controller_update._gate') as gate, \
             patch.object(update_drain,'_write') as write:
            with self.assertRaisesRegex(Exception,'update_drain_unrelated_maintenance_pending'):
                update_drain.request({'root':self.root,'prefix':self.prefix},self.parent,self.job,
                                     self.save,now=lambda:1000.0)
        gate.assert_not_called();write.assert_not_called()

    def test_round_zero_gate_baseline_is_durable_before_crash_window(self):
        from poolrotater_host.controller_update import GATE
        from poolrotater_host.provisioning import _control as native_control
        gate_path=self.root/GATE;gate_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        collected=(['room1'],self.initial,self.record['projection_sha256'],
                   self.record['eligibility_sha256'],self.projection)
        def collect(*args,**kwargs):
            if collect.calls==0:
                collect.calls+=1
                return collected
            raise KeyboardInterrupt
        collect.calls=0
        def gate(target,parent,job,phase):
            if phase=='locking':
                atomic_json(gate_path,{'format':1,'phase':'locking','nonce':job['nonce'],
                    'expires_at':2000.,'bridge_release':parent['release']})
            elif phase is None:gate_path.unlink(missing_ok=True)
            return True
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_control',side_effect=lambda path,*args,**kwargs:
                 native_control(path,*args,**kwargs)), \
             patch.object(update_drain,'_configuration_sha',return_value=self.record['configuration_sha256']), \
             patch.object(update_drain,'_collect',side_effect=collect), \
             patch.object(update_drain,'_write',side_effect=lambda path,value:atomic_json(path,value)), \
             patch('poolrotater_host.controller_update._gate',side_effect=gate):
            with self.assertRaises(KeyboardInterrupt):
                update_drain.request({'root':self.root,'prefix':self.prefix},self.parent,self.job,
                                     self.save,now=lambda:1000.)
        self.assertTrue(gate_path.exists())
        self.assertIsNone(self.job.get(update_drain.RECORD))
        baseline=self.job[update_drain.GATE_BASELINE]
        self.assertEqual(baseline['nonce'],self.job['nonce'])
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch('poolrotater_host.controller_update._gate',side_effect=gate):
            self.assertTrue(update_drain.release_unpublished_gate(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job))
        self.assertFalse(gate_path.exists())

    def test_booking_or_no_eligible_slot_creates_no_mailbox(self):
        result,_,_=self.request(gate=False)
        self.assertEqual(result['phase'],'waiting_for_bookings')
        self.assertFalse((self.prefix/NAME).exists())
        self.job.pop(update_drain.RECORD,None)
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_control',side_effect=lambda path,*args,**kwargs:json.loads(Path(path).read_text()) if Path(path).exists() else None), \
             patch.object(update_drain,'_configuration_sha',return_value=self.record['configuration_sha256']), \
             patch.object(update_drain,'_collect',return_value=([],{},self.record['projection_sha256'],self.record['eligibility_sha256'],self.projection)), \
             patch('poolrotater_host.controller_update._gate') as gate:
            result=update_drain.request({'root':self.root,'prefix':self.prefix},self.parent,self.job,
                                        self.save,now=lambda:1000.0)
        self.assertEqual(result['phase'],'no_eligible_rooms')
        gate.assert_not_called()
        self.assertFalse((self.prefix/NAME).exists())

    def test_unrelated_pending_maintenance_is_never_overwritten(self):
        other={'format':1,'id':str(uuid.uuid4()),'installation_id':self.parent['installation_id'],
            'generation':self.parent['generation'],'parent_sha256':self.parent['bundle_sha256'],
            'phase':'requested','mode':'empty','rooms':['room2'],'created_at':1000.0,'expires_at':2000.0}
        atomic_json(self.prefix/NAME,other)
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_control',side_effect=lambda path,*args,**kwargs:json.loads(Path(path).read_text()) if Path(path).exists() else None), \
             patch.object(update_drain,'_configuration_sha',return_value=self.record['configuration_sha256']), \
             patch.object(update_drain,'_collect',return_value=(['room1'],self.initial,self.record['projection_sha256'],self.record['eligibility_sha256'],self.projection)), \
             patch('poolrotater_host.controller_update._gate',return_value=True):
            with self.assertRaisesRegex(Exception,'update_drain_unrelated_maintenance_pending'):
                update_drain.request({'root':self.root,'prefix':self.prefix},self.parent,self.job,
                                     self.save,now=lambda:1000.0)
        self.assertEqual(read_json(self.prefix/NAME),other)
        self.assertNotIn(update_drain.RECORD,self.job)

    def test_round_one_refuses_foreign_active_mailbox_even_with_archived_prior_receipt(self):
        current=copy.deepcopy(self.record);current['round']=1
        current['request_id']=update_drain._request_id(self.job['id'],1)
        self.job['controller_update_drain_round']=1
        self.job[update_drain.RECORD]=current
        other={'format':1,'id':str(uuid.uuid4()),'installation_id':self.parent['installation_id'],
            'generation':self.parent['generation'],'parent_sha256':self.parent['bundle_sha256'],
            'phase':'requested','mode':'empty','rooms':['room2'],'created_at':1000.0,'expires_at':2000.0}
        old_progress={'format':1,'id':self.record['request_id'],'phase':'cancelled','mode':'empty',
            'controller_pid':self.job['process']['pid'],'updated_at':1100.0,'rooms':{'room1':'cancelled'}}
        def control(path,*args,**kwargs):
            path=Path(path)
            if path==self.prefix/NAME:return other
            if path==self.root/RECEIPT:return old_progress
            if kwargs.get('optional'):return None
            raise FileNotFoundError(path)
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_control',side_effect=control), \
             patch.object(update_drain,'_configuration_sha',return_value=current['configuration_sha256']), \
             patch.object(update_drain,'_history_matches',return_value=True), \
             patch.object(update_drain,'_collect') as collect, \
             patch('poolrotater_host.controller_update._gate') as gate, \
             patch.object(update_drain,'_write') as write:
            with self.assertRaisesRegex(Exception,'update_drain_unrelated_maintenance_pending'):
                update_drain.request({'root':self.root,'prefix':self.prefix},self.parent,self.job,
                                     self.save,now=lambda:1200.)
        collect.assert_not_called();gate.assert_not_called();write.assert_not_called()

    def test_cancelled_exact_request_is_reported_as_terminal(self):
        cancelled={'format':1,'id':self.record['request_id'],
            'installation_id':self.parent['installation_id'],'generation':self.parent['generation'],
            'parent_sha256':self.parent['bundle_sha256'],'phase':'cancelled','mode':'empty',
            'rooms':['room1'],'created_at':self.record['created_at'],'expires_at':self.record['expires_at']}
        self.job[update_drain.RECORD]=copy.deepcopy(self.record)
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_configuration_sha',return_value=self.record['configuration_sha256']), \
             patch.object(update_drain,'_maintenance_paths',return_value=(self.prefix/NAME,
                 self.root/RECEIPT,cancelled,None)), \
             patch('poolrotater_host.controller_update._gate') as gate:
            result=update_drain.request({'root':self.root,'prefix':self.prefix},self.parent,self.job,
                                        self.save,now=lambda:1000.0)
        self.assertEqual(result['phase'],'cancelled')
        gate.assert_not_called()

    def test_cancel_writes_only_exact_mailbox_phase_and_is_idempotent(self):
        mailbox={'format':1,'id':self.record['request_id'],
            'installation_id':self.parent['installation_id'],'generation':self.parent['generation'],
            'parent_sha256':self.parent['bundle_sha256'],'phase':'requested','mode':'empty',
            'rooms':['room1'],'created_at':self.record['created_at'],'expires_at':self.record['expires_at']}
        self.job[update_drain.RECORD]=copy.deepcopy(self.record);writes=[]
        def write(path,value):writes.append(copy.deepcopy(value))
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_maintenance_paths',return_value=(self.prefix/NAME,
                 self.root/RECEIPT,mailbox,None)), \
             patch.object(update_drain,'_write',side_effect=write):
            result=update_drain.cancel({'root':self.root,'prefix':self.prefix},self.parent,self.job)
        self.assertEqual(result['phase'],'waiting')
        self.assertEqual(writes,[{**mailbox,'phase':'cancelled'}])
        self.assertEqual(self.job[update_drain.RECORD],self.record)

    def test_cancel_cleans_only_its_gate_when_request_was_never_published(self):
        self.job[update_drain.RECORD]=copy.deepcopy(self.record)
        self.job['nonce']='f'*32
        gate_record={'format':1,'nonce':self.job['nonce'],'phase':'locking','expires_at':2000.0}
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_maintenance_paths',return_value=(self.prefix/NAME,
                 self.root/RECEIPT,None,None)), \
             patch.object(update_drain,'_control',return_value=gate_record), \
             patch('poolrotater_host.controller_update._gate',return_value=True) as gate:
            result=update_drain.cancel({'root':self.root,'prefix':self.prefix},self.parent,self.job)
        self.assertEqual(result['phase'],'complete')
        gate.assert_called_once_with({'root':self.root,'prefix':self.prefix},self.parent,self.job,None)

    def test_rearm_requires_current_process_and_refreshes_only_exact_gate(self):
        self.job[update_drain.RECORD]=copy.deepcopy(self.record);self.job['phase']='recovering'
        mailbox={'format':1,'id':self.record['request_id'],
            'installation_id':self.parent['installation_id'],'generation':self.parent['generation'],
            'parent_sha256':self.parent['bundle_sha256'],'phase':'requested','mode':'empty',
            'rooms':['room1'],'created_at':self.record['created_at'],'expires_at':self.record['expires_at']}
        progress={'format':1,'id':self.record['request_id'],'phase':'complete','mode':'empty',
            'controller_pid':self.job['process']['pid'],'updated_at':1500.0,'rooms':{'room1':'closed'}}
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_maintenance_paths',return_value=(self.prefix/NAME,
                 self.root/RECEIPT,mailbox,progress)), \
             patch.object(update_drain,'_rebase_configuration',return_value=self.record) as rebase, \
             patch('poolrotater_host.updates.recovery._approval') as approval, \
             patch('poolrotater_host.controller_update._gate',return_value=True) as gate:
            result=update_drain.rearm({'root':self.root,'prefix':self.prefix},self.parent,self.job,self.save)
        self.assertEqual(result['phase'],'waiting')
        rebase.assert_called_once();approval.assert_called_once_with({'root':self.root,'prefix':self.prefix},self.job,recovering=True)
        gate.assert_called_once_with({'root':self.root,'prefix':self.prefix},self.parent,self.job,'locking')
        with patch.object(update_drain,'_process_matches',return_value=False), \
             patch('poolrotater_host.updates.recovery._approval') as approval:
            with self.assertRaises(Exception):
                update_drain.rearm({'root':self.root,'prefix':self.prefix},self.parent,self.job,self.save)
        approval.assert_not_called()

    def test_private_or_booking_designated_running_slot_blocks_partial_drain(self):
        projection={'format':1,'rooms':{
            'room1':{'desired':'running','settings':{'visibility':'public'}},
            'room2':{'desired':'running','settings':{'visibility':'private'}}}}
        managed={
            'room1':{'alias':'room1','desired':'running','revision':1,'phase':'running','room_id':501,
                'room_generation':'managed1','pending_operation':None,'preset':'standard-fixed',
                'visibility':'public','settings':{'visibility':'public'}},
            'room2':{'alias':'room2','desired':'running','revision':2,'phase':'running','room_id':502,
                'room_generation':'managed2','pending_operation':None,'preset':'standard-fixed',
                'visibility':'private','settings':{'visibility':'private'}}}
        host={'rooms':[{'alias':'room1','settings':{'visibility':'public'}},
                       {'alias':'room2','settings':{'visibility':'private'}}]}
        observations={};journal={};reservations={}
        for index,alias in enumerate(('room1','room2'),1):
            room_id=500+index;generation='worker'+str(index);pid=100+index
            identity={'pid':pid,'generation':generation,'room_id':room_id,
                      'release':self.parent['release']}
            observations[alias]={**identity,'phase':'running','operation':'owned',
                'ownership_unknown':False,'error':None,'target':None,'creation_candidate':None,
                'admission_locked':False,'healthy':True,'roster_known':True,'heartbeat_age':1.0,
                'status':{'ready':True,'running':True,'room_id':room_id,'state':'IDLE','players':0}}
            journal[alias]=dict(observations[alias])
            managed[alias].update(room_id=room_id,room_generation='managed'+str(index))
            reservations[alias]={'alias':alias,'generation':generation,'room_id':room_id,
                                 'phase':'owned','operation':None}
        snapshot={'generated_at':1000,'configuration_error':None,'rooms':observations}
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_configuration_sha',return_value=fingerprint(self.job['configuration'])), \
             patch.object(update_drain,'_managed_snapshot',return_value=(self.marker,host,None,managed,reservations,projection)), \
             patch.object(update_drain,'_control',side_effect=lambda path,*args,**kwargs:snapshot if str(path).endswith('status.json') else {'format':1,'rooms':journal}), \
             patch('poolrotater_host.reconciliation._journal',return_value={'format':1,'rooms':journal}), \
             patch('poolrotater_host.ratings.operations._identity'), \
             patch('poolrotater_host.bookings.policy.holds_updates',return_value=False), \
             patch('poolrotater_host.bookings.policy.enabled_aliases',return_value=frozenset()), \
             patch('poolrotater_host.bookings.update_guard.holds_alias',return_value=False):
            result=update_drain._collect({'root':self.root},self.parent,self.job,now=1000)
        self.assertEqual(result[0],[])
        self.assertEqual(len(result),5)
        self.assertEqual(result[4],projection)

        # A real booking hold also blocks a partial drain and must retain the
        # same five-value _collect contract used by request()/replay callers.
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_configuration_sha',return_value=fingerprint(self.job['configuration'])), \
             patch.object(update_drain,'_managed_snapshot',return_value=(self.marker,host,None,managed,reservations,projection)), \
             patch.object(update_drain,'_control',side_effect=lambda path,*args,**kwargs:snapshot if str(path).endswith('status.json') else {'format':1,'rooms':journal}), \
             patch('poolrotater_host.reconciliation._journal',return_value={'format':1,'rooms':journal}), \
             patch('poolrotater_host.ratings.operations._identity'), \
             patch('poolrotater_host.bookings.policy.holds_updates',return_value=True), \
             patch('poolrotater_host.bookings.policy.enabled_aliases',return_value=frozenset()), \
             patch('poolrotater_host.bookings.update_guard.holds_alias',return_value=False):
            held=update_drain._collect({'root':self.root},self.parent,self.job,now=1000)
        self.assertEqual(held[0],[])
        self.assertEqual(len(held),5)
        self.assertEqual(held[4],projection)

    def test_collect_reads_real_managed_state_and_projection_fixture(self):
        from poolrotater_host.managed_host import policy,projection
        from poolrotater_host.controller_update import _config_hashes
        marker,host=load(self.root)
        managed=policy.state(self.root)
        from poolrotater_host.runtime_config import render
        from poolrotater_host.managed_host.settings import FIELDS
        rendered=render(self.root)
        managed.seed([{'alias':row['alias'],'desired':'running' if row['alias']=='room1' else 'stopped',
            'settings':{k:v for k,v in row['settings'].items() if k in FIELDS}
                |{'preset':row['preset'],'visibility':row['settings'].get('managed_visibility','public')}}
            for row in rendered['rooms']])
        managed.policy_applied(1);projection.export(managed)
        generation='a'*32;room_id=7654321
        managed.reserve('room1',generation);managed.mark_dispatched('room1',generation)
        managed.observe_owned('room1',generation,room_id)
        from poolrotater_host.diagnostic_logs.common import transaction
        with transaction(managed.path) as db:
            reservations={row['alias']:dict(row) for row in db.execute('SELECT * FROM reservations')}
        marker,host=load(self.root);aliases={row['alias'] for row in host['rooms']}
        runtime=self.root/'state/rolling';runtime.mkdir(parents=True,exist_ok=True)
        observed={alias:{'phase':'parked','operation':'closed','room_id':None,'pid':None,
            'generation':None,'release':None,'ownership_unknown':False,'error':None,'target':None,
            'creation_candidate':None,'admission_locked':False} for alias in aliases}
        running={'phase':'running','operation':'owned','room_id':room_id,'pid':4321,
            'generation':generation,'release':self.parent['release'],'ownership_unknown':False,
            'error':None,'target':None,'creation_candidate':None,'admission_locked':False,
            'healthy':True,'roster_known':True,'heartbeat_age':1.0,
            'status':{'ready':True,'running':True,'room_id':room_id,'state':'IDLE','players':0}}
        observed['room1']=running
        atomic_json(runtime/'status.json',{'generated_at':1000.0,'configuration_error':None,'rooms':observed})
        atomic_json(runtime/'ownership.json',{'format':1,'rooms':observed})
        journal={'format':1,'rooms':observed}
        parent={**marker,'bundle_sha256':'a'*64,'prefix':str(self.prefix),'host_root':str(self.root),
            'service_uid':self.root.stat().st_uid,'release':self.parent['release'],'execution':'pilot-live'}
        job={'id':str(uuid.uuid4()),'phase':'waiting_empty','before':parent,
            'rooms':sorted(aliases),'process':{'pid':4321,'start':'start','boot':'boot'},
            'configuration':_config_hashes(self.root)}
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch('poolrotater_host.ratings.operations._identity'):
            selected,initial,_projection_sha,_eligibility_sha,_projection=update_drain._collect(
                {'root':self.root},parent,job,now=1000.0)
        self.assertEqual(selected,['room1'])
        self.assertEqual(initial['room1']['identity']['room_id'],room_id)
        self.assertEqual(initial['room1']['reservation']['generation'],generation)
        drain={'rooms':['room1'],'projection':projection.read(self.root,marker),
               'initial':{'room1':initial['room1']}}
        stale_status=read_json(runtime/'status.json');stale_status['generated_at']=879.0
        atomic_json(runtime/'status.json',stale_status)
        with patch('poolrotater_host.ratings.operations._identity'):
            self.assertEqual(update_drain._pending_configuration_work(self.root,
                self.parent['service_uid'],drain,now=1000),[])
        stale_status['generated_at']=1000.0;atomic_json(runtime/'status.json',stale_status)
        # A valid stopped-room edit must keep admission open before any
        # maintenance gate is published. The request itself remains native and
        # durably pending until the ordinary engine applies it.
        offslot=managed.room('room4',private=True)
        operation_id=str(uuid.uuid4())
        request={'protocol_version':1,'policy_version':1,'id':operation_id,**managed.binding,
            'alias':'room4','actor':'fixture-config-restore','role':'FLEET_ADMIN',
            'action':'configure','expected_revision':offslot['revision'],
            'expected_room_generation':None,'timing':'immediate',
            'patch':{'preset':'standard-loved','visibility':'private',
                'password':'fixture-one-time-password'},'expires_at':time.time()+600,
            'cancel_operation_id':None}
        managed.enqueue(request)
        from poolrotater_host.network.contracts import canonical,NetworkError
        from poolrotater_host.managed_host.contracts import digest
        foreign={**request,'host_id':str(uuid.uuid4())}
        with transaction(managed.path)as db:
            db.execute('UPDATE operations SET payload=?,digest=? WHERE id=?',
                (canonical(foreign).decode(),digest(foreign),operation_id))
        with patch('poolrotater_host.ratings.operations._identity'), \
             self.assertRaisesRegex(NetworkError,'update_drain_managed_operation_requires_review'):
            update_drain._pending_configuration_work(self.root,self.parent['service_uid'],drain,now=1000)
        with transaction(managed.path)as db:
            db.execute('UPDATE operations SET payload=?,digest=? WHERE id=?',
                (canonical(request).decode(),digest(request),operation_id))
        stale_status['generated_at']=879.0;atomic_json(runtime/'status.json',stale_status)
        with patch('poolrotater_host.ratings.operations._identity'), \
             self.assertRaisesRegex(NetworkError,'update_drain_pause_status_stale'):
            update_drain._pending_configuration_work(self.root,self.parent['service_uid'],
                drain,now=1000)
        stale_status['generated_at']=1000.0;atomic_json(runtime/'status.json',stale_status)
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_configuration_sha',return_value=fingerprint(job['configuration'])), \
             patch.object(update_drain,'_managed_snapshot',return_value=(marker,host,managed,
                {row['alias']:row for row in managed.snapshot()['rooms']},reservations,
                projection.read(self.root,marker))), \
             patch.object(update_drain,'_control',side_effect=lambda path,*args,**kwargs:snapshot if str(path).endswith('status.json') else {'format':1,'rooms':journal}), \
             patch('poolrotater_host.reconciliation._journal',return_value={'format':1,'rooms':journal}), \
             patch('poolrotater_host.ratings.operations._identity'), \
             patch('poolrotater_host.bookings.policy.holds_updates',return_value=False), \
             patch('poolrotater_host.bookings.policy.enabled_aliases',return_value=frozenset()), \
             patch('poolrotater_host.bookings.update_guard.holds_alias',return_value=False):
            blocked=update_drain._collect({'root':self.root},parent,job,now=1000)
        self.assertEqual(blocked[0],[])
        current_projection=projection.read(self.root,marker)
        drain={'rooms':['room1'],'projection':current_projection,
               'initial':{'room1':initial['room1']}}
        with patch('poolrotater_host.ratings.operations._identity'):
            pending=update_drain._pending_configuration_work(self.root,self.parent['service_uid'],drain,now=1000)
        self.assertEqual([item['id'] for item in pending],[operation_id])
        self.assertEqual(managed.begin(operation_id,ready=False)['phase'],'waiting_boundary')
        altered={**pending[0],'payload_sha256':digest(foreign)}
        with transaction(managed.path)as db:
            db.execute('UPDATE operations SET payload=?,digest=? WHERE id=?',
                (canonical(foreign).decode(),digest(foreign),operation_id))
        with patch('poolrotater_host.ratings.operations._identity'), \
             self.assertRaisesRegex(NetworkError,'update_drain_pause_operation_changed'):
            update_drain._paused_configuration_projection(self.root,self.parent['service_uid'],drain,
                {'operations':[altered]},current_projection,
                {row['alias']:row for row in managed.snapshot()['rooms']},reservations,
                now=1000)
        with transaction(managed.path)as db:
            db.execute('UPDATE operations SET payload=?,digest=? WHERE id=?',
                (canonical(request).decode(),digest(request),operation_id))
        with patch('poolrotater_host.ratings.operations._identity'):
            self.assertEqual(update_drain._pending_configuration_work(self.root,self.parent['service_uid'],drain,now=1000),pending)
        self.assertEqual(managed.begin(operation_id,ready=True)['phase'],'applying')
        managed.finish(operation_id,'applied',result={'application':'recreate','revision':offslot['revision']+1})
        projection.export(managed)
        updated=managed.snapshot();updated_rooms={row['alias']:row for row in updated['rooms']}
        from poolrotater_host.diagnostic_logs.common import transaction
        with transaction(managed.path) as db:
            updated_reservations={row['alias']:dict(row) for row in db.execute('SELECT * FROM reservations')}
        current_projection=projection.read(self.root,marker)
        pause={'operations':pending}
        with patch('poolrotater_host.ratings.operations._identity'):
            allowed=update_drain._paused_configuration_projection(self.root,self.parent['service_uid'],
                drain,pause,current_projection,updated_rooms,updated_reservations,now=1000)
        self.assertEqual(allowed['rooms']['room4']['settings']['visibility'],'private')
        self.assertEqual(allowed['rooms']['room4']['settings']['preset'],'standard-loved')
        drift=copy.deepcopy(current_projection);drift['rooms']['room4']['revision']+=1
        with patch('poolrotater_host.ratings.operations._identity'), self.assertRaises(Exception):
            update_drain._paused_configuration_projection(self.root,self.parent['service_uid'],
                drain,pause,drift,updated_rooms,updated_reservations)

        # A still-owned public sibling can accept a live edit while a drain is
        # pending. Its existing room identity and reservation must remain exact.
        current_projection=projection.read(self.root,marker)
        job['configuration']=_config_hashes(self.root)
        live_room=managed.room('room1',private=True);live_id=str(uuid.uuid4())
        live_request={'protocol_version':1,'policy_version':1,'id':live_id,**managed.binding,
            'alias':'room1','actor':'fixture-live-config-during-drain','role':'FLEET_ADMIN',
            'action':'configure','expected_revision':live_room['revision'],
            'expected_room_generation':live_room['room_generation'],'timing':'immediate',
            'patch':{'countdown_seconds':12},'expires_at':time.time()+600,
            'cancel_operation_id':None}
        managed.enqueue(live_request);managed.begin(live_id,ready=False)
        live_drain={'rooms':['room1'],'projection':current_projection,
            'initial':{'room1':initial['room1']}}
        with patch('poolrotater_host.ratings.operations._identity'):
            live_pending=update_drain._pending_configuration_work(self.root,self.parent['service_uid'],
                live_drain,now=1000)
        self.assertEqual(live_pending[0]['application'],'live')
        self.assertEqual(live_pending[0]['identity']['room_id'],room_id)
        self.assertEqual(managed.begin(live_id,ready=True)['phase'],'applying')
        managed.finish(live_id,'applied',result={'application':'live',
            'revision':live_room['revision']+1})
        projection.export(managed)
        updated_rooms={row['alias']:row for row in managed.snapshot()['rooms']}
        with transaction(managed.path) as db:
            updated_reservations={row['alias']:dict(row) for row in db.execute('SELECT * FROM reservations')}
        live_projection=projection.read(self.root,marker)
        with patch('poolrotater_host.ratings.operations._identity'):
            live_allowed=update_drain._paused_configuration_projection(self.root,
                self.parent['service_uid'],live_drain,{'operations':live_pending},
                live_projection,updated_rooms,updated_reservations,now=1000)
        self.assertEqual(live_allowed['rooms']['room1']['revision'],live_room['revision']+1)
        self.assertEqual(updated_rooms['room1']['room_id'],room_id)
        observed['room4']['pid']=9876
        atomic_json(runtime/'status.json',{'generated_at':1000.0,'configuration_error':None,'rooms':observed})
        atomic_json(runtime/'ownership.json',{'format':1,'rooms':observed})
        from poolrotater_host.network.contracts import NetworkError
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch('poolrotater_host.ratings.operations._identity'), \
             self.assertRaises(NetworkError):
            update_drain._collect({'root':self.root},parent,job,now=1000.0)


class UpdateDrainProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)/'host';setup(root)
        prefix=Path(self.tmp.name)/'prefix';prefix.mkdir(mode=0o700)
        marker,parent,job=binding(root,prefix);now=2000.0
        alias=initial_alias();drain_id=str(uuid.uuid5(uuid.UUID(job['id']),'poolrotater-empty-room-drain-v1'))
        projection={'format':1,'installation_id':marker['installation_id'],
                    'generation':marker['generation'],'rooms':{
                        'room1':{'revision':4,'desired':'running','settings':{'preset':'standard-fixed'}},
                        'room2':{'revision':0,'desired':'stopped','settings':{'preset':'standard-fixed'}}}}
        self.projection=projection
        initial={'room1':alias}
        drain={'format':1,'job_id':job['id'],'request_id':drain_id,
            'installation_id':marker['installation_id'],'generation':marker['generation'],
            'parent_sha256':parent['bundle_sha256'],'mode':'empty','rooms':['room1'],
            'created_at':now-30,'expires_at':now+3600,
            'configuration_sha256':fingerprint(job['configuration']),
            'projection_sha256':fingerprint(projection),'eligibility_sha256':'f'*64,
            'projection':projection,'initial':initial}
        job[update_drain.RECORD]=drain;job['nonce']='f'*32
        self.target={'root':root,'prefix':prefix};self.parent=parent;self.job=job;self.now=now
        self.request_value={'format':1,'id':drain_id,'installation_id':parent['installation_id'],
            'generation':parent['generation'],'parent_sha256':parent['bundle_sha256'],'phase':'requested',
            'mode':'empty','rooms':['room1'],'created_at':now-30,'expires_at':now+3600}
        status_row={'phase':'parked','operation':'closed','room_id':None,'pid':None,
            'generation':'worker-generation','release':alias['identity']['release'],
            'ownership_unknown':False,'error':None,'target':None,'creation_candidate':None,
            'admission_locked':False}
        self.status={'generated_at':now-1,'supervisor_version':'v7.3.2','configuration_error':None,
                     'rooms':{'room1':status_row,'room2':{'phase':'parked','operation':'closed',
                         'room_id':None,'pid':None,'generation':'off-generation','release':None,
                         'ownership_unknown':False,'error':None,'target':None,'creation_candidate':None}}}
        self.journal={'format':1,'rooms':copy.deepcopy(self.status['rooms'])}
        self.reservation={'alias':'room1','generation':'worker-generation','room_id':501,'phase':'owned','operation':None}
        self.extra_reservations=[]
        self.managed={'rooms':[{'alias':'room1','desired':'running','revision':4,'phase':'running',
            'room_id':501,'room_generation':'managed-generation','pending_operation':None,
            'failure_code':None,
            'preset':'standard-fixed','visibility':'public'},
            {'alias':'room2','desired':'stopped','revision':0,'phase':'stopped','room_id':None,
             'room_generation':None,'pending_operation':None,'failure_code':None,
             'preset':'standard-fixed','visibility':'public'}]}
        self.rows={'room1':copy.deepcopy(self.managed['rooms'][0]),'room2':copy.deepcopy(self.managed['rooms'][1])}
        self.rows['room1']['settings']={'preset':'standard-fixed','visibility':'public'}
        self.rows['room2']['settings']={'preset':'standard-fixed','visibility':'public'}
        self.gate={'format':1,'nonce':job['nonce'],'phase':'locking','expires_at':now+1000}
        self.progress={'format':1,'id':drain_id,'phase':'complete','mode':'empty',
            'controller_pid':job['process']['pid'],'updated_at':now-1,
            'rooms':{'room1':'closed'},'closure_evidence':{'room1':{
                'room_id':501,'mode':'empty','notice_confirmed':False,'active_map_observed':False,
                'require_empty':True,'closure_started_at':now-10}}}

    def proof(self,*,strict_identity=False,actual_eligibility=False):
        from contextlib import contextmanager,nullcontext
        import os
        import poolrotater_host.update_drain as module
        outer=self
        class Store:
            path=Path(outer.tmp.name)/'fake-managed.db'
            @staticmethod
            def snapshot():return copy.deepcopy(outer.managed)
        class Cursor:
            def execute(cursor,_sql):
                rows=list(outer.extra_reservations)
                if outer.reservation is not None:rows.insert(0,outer.reservation)
                return rows
        @contextmanager
        def transaction(_path):yield Cursor()
        def control(path,*args,**kwargs):
            path=Path(path)
            if path==self.target['root']/'state/rolling/supervisor-maintenance.json':return copy.deepcopy(self.gate)
            if path==self.target['prefix']/NAME:return copy.deepcopy(self.request_value)
            if path==self.target['root']/RECEIPT:return copy.deepcopy(self.progress)
            if path==self.target['root']/'state/rolling/status.json':return copy.deepcopy(self.status)
            if path==self.target['root']/'state/rolling/ownership.json':return copy.deepcopy(self.journal)
            raise AssertionError(path)
        marker={'format':1,'installation_id':self.parent['installation_id'],'generation':self.parent['generation']}
        identity_patch=patch('poolrotater_host.ratings.operations._identity')
        if strict_identity:
            root_uid=os.geteuid();active_uid={'value':root_uid}
            @contextmanager
            def checked_identity(uid):
                if active_uid['value']!=root_uid:
                    raise AssertionError('nested service UID identity scope')
                active_uid['value']=uid
                try:yield
                finally:active_uid['value']=root_uid
            identity_patch=patch('poolrotater_host.ratings.operations._identity',checked_identity)
        eligibility_patch=(nullcontext() if actual_eligibility else
                           patch.object(module,'_eligibility_sha',return_value=self.job[update_drain.RECORD]['eligibility_sha256']))
        with patch.object(module,'_control',side_effect=control), \
             patch.object(module,'_process_matches',return_value=True), \
             patch.object(module,'_configuration_sha',return_value=self.job[update_drain.RECORD]['configuration_sha256']), \
             eligibility_patch, identity_patch, \
             patch.object(module,'load',return_value=(marker,{'rooms':[{'alias':'room1'},{'alias':'room2'}]})), \
             patch('poolrotater_host.managed_host.policy.state',return_value=Store()), \
             patch('poolrotater_host.managed_host.projection.read',return_value=self.projection), \
             patch('poolrotater_host.reconciliation._journal',return_value=self.journal), \
             patch('poolrotater_host.diagnostic_logs.common.transaction',side_effect=transaction), \
             patch('poolrotater_host.bookings.policy.holds_updates',return_value=False), \
             patch('poolrotater_host.bookings.update_guard.holds_alias',return_value=False):
            return update_drain.proof(self.target,self.parent,self.job,now=self.now)

    def test_proof_returns_only_exact_clean_closed_alias_and_retains_managed_intent(self):
        self.projection={'format':1,'installation_id':self.parent['installation_id'],
                         'generation':self.parent['generation'],'rooms':{
                            'room1':{'revision':4,'desired':'running','settings':{'preset':'standard-fixed'}},
                            'room2':{'revision':0,'desired':'stopped','settings':{'preset':'standard-fixed'}}}}
        result=self.proof()
        self.assertEqual(set(result),{'room1'})
        self.assertIs(result['room1']['managed_closed'],True)
        self.assertIs(result['room1']['managed_reservation_pending_reconcile'],True)
        self.assertEqual(self.managed['rooms'][0]['desired'],'running')

    def test_proof_eligibility_read_uses_non_nested_identity_and_keeps_ownership_guards(self):
        self.projection={'format':1,'installation_id':self.parent['installation_id'],
                         'generation':self.parent['generation'],'rooms':{
                            'room1':{'revision':4,'desired':'running','settings':{'preset':'standard-fixed','visibility':'public'}},
                            'room2':{'revision':0,'desired':'stopped','settings':{'preset':'standard-fixed','visibility':'public'}}}}
        self.job[update_drain.RECORD]['projection']=copy.deepcopy(self.projection)
        self.job[update_drain.RECORD]['projection_sha256']=fingerprint(self.projection)
        self.job[update_drain.RECORD]['eligibility_sha256']=fingerprint({
            'rooms':{'room1':{'visibility':'public'},'room2':{'visibility':'public'}},
            'booking_aliases':[]})
        result=self.proof(strict_identity=True,actual_eligibility=True)
        self.assertEqual(set(result),{'room1'})
        self.journal['rooms']['room1']['ownership_unknown']=True
        self.assertIsNone(self.proof(strict_identity=True,actual_eligibility=True))

    def test_collect_booking_hold_eligibility_uses_non_nested_identity(self):
        from contextlib import contextmanager
        import os
        host={'rooms':[{'alias':'room1'},{'alias':'room2'}]}
        marker={'format':1,'installation_id':self.parent['installation_id'],
                'generation':self.parent['generation']}
        managed={row['alias']:copy.deepcopy(row) for row in self.managed['rooms']}
        managed['room1']['pending_operation']='pending-native-operation'
        root_uid=os.geteuid();active_uid={'value':root_uid}
        @contextmanager
        def checked_identity(uid):
            if active_uid['value']!=root_uid:
                raise AssertionError('nested service UID identity scope')
            active_uid['value']=uid
            try:yield
            finally:active_uid['value']=root_uid
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_configuration_sha',return_value=fingerprint(self.job['configuration'])), \
             patch.object(update_drain,'_managed_snapshot',return_value=(marker,host,None,managed,{},self.projection)), \
             patch('poolrotater_host.ratings.operations._identity',checked_identity), \
             patch('poolrotater_host.bookings.policy.holds_updates',return_value=True):
            result=update_drain._collect({'root':self.target['root']},self.parent,self.job,now=self.now)
        self.assertEqual(result[0],[])

    def test_proof_accepts_already_settled_native_closure(self):
        self.projection={'format':1,'installation_id':self.parent['installation_id'],
                         'generation':self.parent['generation'],'rooms':{
                            'room1':{'revision':4,'desired':'running','settings':{'preset':'standard-fixed'}},
                            'room2':{'revision':0,'desired':'stopped','settings':{'preset':'standard-fixed'}}}}
        self.managed['rooms'][0].update(phase='stopped',room_id=None,room_generation=None)
        self.reservation=None
        result=self.proof()
        self.assertEqual(set(result),{'room1'})
        self.assertIs(result['room1']['managed_closed'],True)
        self.assertIs(result['room1']['managed_reservation_pending_reconcile'],False)

    def _add_second_drained_alias(self):
        """Expand the exact fixture to exercise mixed per-alias settle states."""
        drain=self.job[update_drain.RECORD]
        second=initial_alias()
        second['identity'].update(pid=102,generation='worker-generation-2',room_id=502)
        second['managed'].update(revision=2,room_id=502,room_generation='managed-generation-2')
        second['reservation'].update(generation='worker-generation-2',room_id=502)
        drain['rooms']=['room1','room2']
        drain['initial']['room2']=second
        self.request_value['rooms']=['room1','room2']
        drain['projection']['rooms']['room2']={'revision':2,'desired':'running',
            'settings':{'preset':'standard-fixed'}}
        drain['projection_sha256']=fingerprint(drain['projection'])
        self.projection=copy.deepcopy(drain['projection'])
        room=self.managed['rooms'][1]
        room.update(desired='running',revision=2,phase='running',room_id=502,
                    room_generation='managed-generation-2')
        self.extra_reservations=[{'alias':'room2',**second['reservation']}]
        row={'phase':'parked','operation':'closed','room_id':None,'pid':None,
            'generation':'worker-generation-2','release':second['identity']['release'],
            'ownership_unknown':False,'error':None,'target':None,'creation_candidate':None,
            'admission_locked':False}
        self.status['rooms']['room2']=copy.deepcopy(row)
        self.journal['rooms']['room2']=copy.deepcopy(row)
        self.progress['rooms']['room2']='closed'
        self.progress['closure_evidence']['room2']={
            'room_id':502,'mode':'empty','notice_confirmed':False,'active_map_observed':False,
            'require_empty':True,'closure_started_at':self.now-10}

    def test_proof_accepts_mixed_settled_and_still_reserved_aliases(self):
        self._add_second_drained_alias()
        self.managed['rooms'][0].update(phase='stopped',room_id=None,room_generation=None)
        self.reservation=None
        result=self.proof()
        self.assertEqual(set(result),{'room1','room2'})
        self.assertIs(result['room1']['managed_reservation_pending_reconcile'],False)
        self.assertIs(result['room2']['managed_reservation_pending_reconcile'],True)

    def test_proof_accepts_both_aliases_settled_with_distinct_worker_generations(self):
        self._add_second_drained_alias()
        self.managed['rooms'][0].update(phase='stopped',room_id=None,room_generation=None)
        self.managed['rooms'][1].update(phase='stopped',room_id=None,room_generation=None)
        self.reservation=None
        self.extra_reservations=[]
        result=self.proof()
        self.assertEqual(set(result),{'room1','room2'})
        self.assertTrue(all(row['managed_closed'] for row in result.values()))
        self.assertTrue(all(not row['managed_reservation_pending_reconcile'] for row in result.values()))
        self.assertEqual(self.journal['rooms']['room1']['generation'],'worker-generation')
        self.assertEqual(self.journal['rooms']['room2']['generation'],'worker-generation-2')

    def test_proof_refuses_unrelated_reservation_or_changed_settled_generation(self):
        self.projection={'format':1,'installation_id':self.parent['installation_id'],
                         'generation':self.parent['generation'],'rooms':{
                            'room1':{'revision':4,'desired':'running','settings':{'preset':'standard-fixed'}},
                            'room2':{'revision':0,'desired':'stopped','settings':{'preset':'standard-fixed'}}}}
        self.managed['rooms'][0].update(phase='stopped',room_id=None,room_generation=None)
        self.reservation=None
        self.assertIsNotNone(self.proof())
        self.managed['rooms'][0]['room_generation']='foreign-managed-generation'
        self.assertIsNone(self.proof())
        self.managed['rooms'][0]['room_generation']=None
        self.managed['rooms'][0]['revision']=5
        self.assertIsNone(self.proof())
        self.managed['rooms'][0]['revision']=4
        self.reservation={'alias':'room1','generation':'foreign-generation','room_id':501,
                          'phase':'owned','operation':None}
        self.assertIsNone(self.proof())

    def test_proof_refuses_failure_or_pending_operation_for_both_intent_states(self):
        self.projection={'format':1,'installation_id':self.parent['installation_id'],
                         'generation':self.parent['generation'],'rooms':{
                            'room1':{'revision':4,'desired':'running','settings':{'preset':'standard-fixed'}},
                            'room2':{'revision':0,'desired':'stopped','settings':{'preset':'standard-fixed'}}}}
        intent=self.managed['rooms'][0]
        self.assertIsNotNone(self.proof())
        intent['failure_code']='worker_failed'
        self.assertIsNone(self.proof())
        intent['failure_code']=None
        intent['pending_operation']='foreign-operation'
        self.assertIsNone(self.proof())
        intent['pending_operation']=None

        intent.update(phase='stopped',room_id=None,room_generation=None)
        self.reservation=None
        self.assertIsNotNone(self.proof())
        intent['failure_code']='worker_failed'
        self.assertIsNone(self.proof())
        intent['failure_code']=None
        intent['pending_operation']='foreign-operation'
        self.assertIsNone(self.proof())

    def test_proof_refuses_foreign_request_stale_status_unknown_room_and_changed_intent(self):
        self.projection={'format':1,'installation_id':self.parent['installation_id'],
                         'generation':self.parent['generation'],'rooms':{
                            'room1':{'revision':4,'desired':'running','settings':{'preset':'standard-fixed'}},
                            'room2':{'revision':0,'desired':'stopped','settings':{'preset':'standard-fixed'}}}}
        for mutate in (
            lambda:self.progress.update(id=str(uuid.uuid4())),
            lambda:self.status.update(generated_at=self.now-100),
            lambda:self.journal['rooms']['room1'].update(ownership_unknown=True),
            lambda:self.managed['rooms'][0].update(revision=5),
            lambda:self.reservation.update(generation='foreign-generation'),
        ):
            with self.subTest(mutate=mutate):
                saved=(copy.deepcopy(self.progress),copy.deepcopy(self.status),copy.deepcopy(self.journal),
                       copy.deepcopy(self.managed),copy.deepcopy(self.reservation))
                mutate();self.assertIsNone(self.proof())
                self.progress,self.status,self.journal,self.managed,self.reservation=saved

    def test_proof_rejects_expired_gate_and_unconfirmed_closure(self):
        self.projection={'format':1,'installation_id':self.parent['installation_id'],
                         'generation':self.parent['generation'],'rooms':{
                            'room1':{'revision':4,'desired':'running','settings':{'preset':'standard-fixed'}},
                            'room2':{'revision':0,'desired':'stopped','settings':{'preset':'standard-fixed'}}}}
        self.gate['expires_at']=self.now-1
        self.assertIsNone(self.proof())
        self.gate['expires_at']=self.now+1000
        self.progress['closure_evidence']['room1']['require_empty']=False
        self.assertIsNone(self.proof())

    def test_refreshed_valid_config_rebases_hash_without_rewriting_closure_or_intent(self):
        self.job['configuration']={'config/host.json':'new-valid-hash'}
        drain=self.job[update_drain.RECORD]
        old_initial=copy.deepcopy(drain['initial'])
        class Store:
            path=Path(self.tmp.name)/'fake-managed.db'
        marker={'format':1,'installation_id':self.parent['installation_id'],'generation':self.parent['generation']}
        host={'rooms':[{'alias':'room1','settings':{'visibility':'public'}},
                       {'alias':'room2','settings':{'visibility':'public'}}]}
        managed={r['alias']:dict(r) for r in self.managed['rooms']}
        reservations={'room1':dict(self.reservation)}
        projection=self.projection
        with patch.object(update_drain,'_configuration_sha',return_value=fingerprint(self.job['configuration'])), \
             patch.object(update_drain,'_managed_snapshot',return_value=(marker,host,Store(),managed,reservations,projection)), \
             patch.object(update_drain,'_eligibility_sha',return_value=drain['eligibility_sha256']), \
             patch.object(update_drain,'_control',side_effect=lambda path,*args,**kwargs:
                self.status if str(path).endswith('status.json') else {'format':1,'rooms':self.journal['rooms']}), \
             patch('poolrotater_host.reconciliation._journal',return_value=self.journal), \
             patch('poolrotater_host.ratings.operations._identity'), \
             patch('poolrotater_host.bookings.policy.holds_updates',return_value=False), \
             patch('poolrotater_host.bookings.update_guard.holds_alias',return_value=False):
            result=update_drain._rebase_configuration(self.target,self.parent,self.job,drain,
                                                       self.progress,now=self.now)
        self.assertEqual(result['configuration_sha256'],fingerprint(self.job['configuration']))
        self.assertEqual(result['initial'],old_initial)
        self.assertEqual(self.managed['rooms'][0]['desired'],'running')


class UpdateDrainRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'host';setup(self.root)
        self.prefix=Path(self.tmp.name)/'prefix';self.prefix.mkdir(mode=0o700)
        from poolrotater_host.managed_host import policy,projection
        from poolrotater_host.managed_host.settings import FIELDS
        from poolrotater_host.runtime_config import render
        from poolrotater_host.controller_update import _config_hashes
        from poolrotater_host.diagnostic_logs.common import transaction
        self.marker,host=load(self.root)
        store=policy.state(self.root);rendered=render(self.root)
        store.seed([{'alias':row['alias'],'desired':'running' if row['alias'] in ('room1','room2') else 'stopped',
            'settings':{k:v for k,v in row['settings'].items() if k in FIELDS}
                |{'preset':row['preset'],'visibility':row['settings'].get('managed_visibility','public')}}
            for row in rendered['rooms']])
        store.policy_applied(1)
        for alias,generation,room_id in (('room1','a'*32,7654321),('room2','b'*32,7654322)):
            store.reserve(alias,generation);store.mark_dispatched(alias,generation);store.observe_owned(alias,generation,room_id)
        projection.export(store)
        self.marker,host=load(self.root);self.aliases={row['alias'] for row in host['rooms']}
        self.projection=projection.read(self.root,self.marker);self.now=time.time()
        managed={row['alias']:row for row in store.snapshot()['rooms']}
        release=str(self.prefix/'releases'/('v7.3.2-'+'b'*16))
        identities={'room1':{'pid':4321,'generation':'a'*32,'room_id':7654321,'release':release},
                    'room2':{'pid':4322,'generation':'b'*32,'room_id':7654322,'release':release}}
        self.parent={**self.marker,'bundle_sha256':'a'*64,'prefix':str(self.prefix),
            'host_root':str(self.root),'service_uid':self.root.stat().st_uid,
            'release':release,'execution':'pilot-live'}
        self.job={'format':1,'id':str(uuid.uuid4()),'phase':'cancelled','before':self.parent,
            'after':{'bundle_sha256':'c'*64},'rooms':sorted(self.aliases),
            'process':{'pid':4321,'start':'start','boot':'boot'},'nonce':'f'*32,
            'configuration':_config_hashes(self.root)}
        from poolrotater_host.diagnostic_logs.common import transaction
        with transaction(store.path) as db:
            reservations={row['alias']:dict(row) for row in db.execute('SELECT * FROM reservations')}
        initial={}
        for alias in ('room1','room2'):
            with transaction(store.path) as db:
                reservation=dict(db.execute('SELECT * FROM reservations WHERE alias=?',(alias,)).fetchone())
            initial[alias]={'identity':identities[alias],
                'managed':{k:managed[alias][k] for k in
                    ('desired','revision','phase','room_id','room_generation','pending_operation','preset','visibility')},
                'reservation':{k:reservation[k] for k in ('generation','room_id','phase','operation')},
                'closure_started_at':None}
        request_id=str(uuid.uuid5(uuid.UUID(self.job['id']),'poolrotater-empty-room-drain-v1'))
        self.drain={'format':1,'job_id':self.job['id'],'request_id':request_id,
            'installation_id':self.parent['installation_id'],'generation':self.parent['generation'],
            'parent_sha256':self.parent['bundle_sha256'],'mode':'empty','rooms':['room1','room2'],
            'created_at':self.now-30,'expires_at':self.now+3600,
            'configuration_sha256':fingerprint(self.job['configuration']),
            'projection_sha256':fingerprint(self.projection),
            'eligibility_sha256':fingerprint({'rooms':{a:{'visibility':self.projection['rooms'][a]['settings'].get('visibility','public')} for a in self.aliases},'booking_aliases':[]}),
            'projection':self.projection,'initial':initial}
        self.job[update_drain.RECORD]=self.drain
        self.mailbox={'format':1,'id':request_id,'installation_id':self.parent['installation_id'],
            'generation':self.parent['generation'],'parent_sha256':self.parent['bundle_sha256'],
            'phase':'cancelled','mode':'empty','rooms':['room1','room2'],
            'created_at':self.drain['created_at'],'expires_at':self.drain['expires_at']}
        row={'phase':'parked','operation':'closed','room_id':None,'pid':None,'generation':'a'*32,
            'release':release,'ownership_unknown':False,'error':None,'target':None,
            'creation_candidate':None,'admission_locked':False}
        rows={alias:{'phase':'parked','operation':'closed','room_id':None,'pid':None,
            'generation':None,'release':None,'ownership_unknown':False,'error':None,'target':None,
            'creation_candidate':None,'admission_locked':False} for alias in self.aliases}
        rows['room1']=row
        rows['room2']={**identities['room2'],'phase':'running','operation':'owned',
            'ownership_unknown':False,'error':None,'target':None,'creation_candidate':None,
            'admission_locked':False,'healthy':True,'roster_known':True,'heartbeat_age':1.0,
            'status':{'ready':True,'running':True,'room_id':7654322,'state':'IDLE','players':0}}
        runtime=self.root/'state/rolling';runtime.mkdir(parents=True,exist_ok=True)
        atomic_json(runtime/'status.json',{'generated_at':self.now-1,'configuration_error':None,'rooms':rows})
        atomic_json(runtime/'ownership.json',{'format':1,'rooms':copy.deepcopy(rows)})
        atomic_json(runtime/'operator-maintenance.json',{'format':1,'id':request_id,
            'phase':'cancelled','rooms':{'room1':'closed','room2':'cancelled'},'mode':'empty','controller_pid':4321,
            'updated_at':self.now-1,'closure_evidence':{'room1':{'room_id':7654321,
                'mode':'empty','notice_confirmed':False,'active_map_observed':False,
                'require_empty':True,'closure_started_at':self.now-10}}})

    def restore(self,now=None,*,real_identity=False,process_matches=True):
        from poolrotater_host.provisioning import _control as native_control
        from contextlib import nullcontext
        def control(path,*args,**kwargs):
            if Path(path)==self.prefix/NAME:return copy.deepcopy(self.mailbox)
            return native_control(path,*args,**kwargs)
        identity=nullcontext() if real_identity else patch('poolrotater_host.ratings.operations._identity')
        with patch.object(update_drain,'_control',side_effect=control), \
             patch.object(update_drain,'_process_matches',return_value=process_matches), \
             identity:
            return update_drain.restore({'root':self.root,'prefix':self.prefix},self.parent,self.job,
                                        now=self.now if now is None else now)

    def _restore_with_identity_scope_capture(self):
        uid=1000
        self.parent['service_uid']=uid
        # Track the identity boundary while leaving the native State store and
        # diagnostic transaction implementation real. The ordinary isolated
        # runner blocks seteuid, so this checks custody scopes rather than OS
        # credentials; transaction calls still open and verify the real DB.
        active={'uid':None};database_scopes=[]
        @contextmanager
        def test_service_identity(service_uid):
            self.assertIsNone(active['uid'])
            active['uid']=service_uid
            try:yield
            finally:active['uid']=None
        from poolrotater_host.diagnostic_logs import common
        transaction=common.transaction
        @contextmanager
        def checked_transaction(path):
            if Path(path)==self.root/'state/managed-host.sqlite3':
                self.assertEqual(active['uid'],uid)
                database_scopes.append(active['uid'])
            with transaction(path) as db:yield db
        with patch('poolrotater_host.ratings.operations._identity',test_service_identity), \
             patch('poolrotater_host.diagnostic_logs.common.transaction',checked_transaction):
            result=self.restore(real_identity=True)
        return result,uid,database_scopes,test_service_identity

    def test_restore_managed_database_calls_run_under_native_service_uid(self):
        result,uid,database_scopes,test_service_identity=self._restore_with_identity_scope_capture()
        self.assertEqual(result['phase'],'waiting')
        self.assertTrue(database_scopes)
        self.assertTrue(all(value==uid for value in database_scopes))
        operation_id=str(uuid.uuid5(uuid.UUID(self.drain['request_id']),'restore-open:room1'))
        with test_service_identity(uid):
            store=__import__('poolrotater_host.managed_host.policy',fromlist=['state']).state(self.root)
            self.assertEqual(store.operation(operation_id)['phase'],'received')

    def test_all_closed_journal_reconcile_runs_under_native_service_uid(self):
        from poolrotater_host.provisioning import _control
        status_path=self.root/'state/rolling/status.json'
        status=_control(status_path,self.parent['service_uid'])
        closed={'phase':'parked','operation':'closed','room_id':None,'pid':None,
            'generation':'b'*32,'release':self.parent['release'],'ownership_unknown':False,
            'error':None,'target':None,'creation_candidate':None,'admission_locked':False}
        status['rooms']['room2']=copy.deepcopy(closed)
        atomic_json(status_path,status)
        journal_path=self.root/'state/rolling/ownership.json'
        journal=read_json(journal_path);journal['rooms']['room2']=copy.deepcopy(closed)
        atomic_json(journal_path,journal)
        receipt_path=self.root/'state/rolling/operator-maintenance.json'
        receipt=read_json(receipt_path);receipt['rooms']['room2']='closed'
        receipt['closure_evidence']['room2']={'room_id':7654322,'mode':'empty',
            'notice_confirmed':False,'active_map_observed':False,'require_empty':True,
            'closure_started_at':self.now-10}
        atomic_json(receipt_path,receipt)
        result,uid,database_scopes,_identity_context=self._restore_with_identity_scope_capture()
        self.assertEqual(result['phase'],'waiting')
        self.assertTrue(database_scopes)
        self.assertTrue(all(value==uid for value in database_scopes))

    def test_config_pause_restores_exact_closures_and_rebases_one_offslot_edit(self):
        from poolrotater_host.managed_host import policy,projection
        from poolrotater_host.controller_update import _config_hashes
        store=policy.state(self.root)
        room=store.room('room4',private=True)
        operation_id=str(uuid.uuid4())
        request={'protocol_version':1,'policy_version':1,'id':operation_id,**store.binding,
            'alias':'room4','actor':'fixture-config-after-drain-gate','role':'FLEET_ADMIN',
            'action':'configure','expected_revision':room['revision'],
            'expected_room_generation':None,'timing':'immediate',
            'patch':{'visibility':'private','password':'fixture-one-time-password'},'expires_at':self.now+600,
            'cancel_operation_id':None}
        store.enqueue(request)
        store.begin(operation_id,ready=False)
        live_room=store.room('room2',private=True);live_id=str(uuid.uuid4())
        live_request={'protocol_version':1,'policy_version':1,'id':live_id,**store.binding,
            'alias':'room2','actor':'fixture-live-config-after-drain-gate','role':'FLEET_ADMIN',
            'action':'configure','expected_revision':live_room['revision'],
            'expected_room_generation':live_room['room_generation'],'timing':'immediate',
            'patch':{'countdown_seconds':12},'expires_at':self.now+600,
            'cancel_operation_id':None}
        store.enqueue(live_request);store.begin(live_id,ready=False)
        with patch('poolrotater_host.ratings.operations._identity'):
            pause_ops=update_drain._pending_configuration_work(self.root,self.parent['service_uid'],self.drain)
        self.assertEqual({row['id']for row in pause_ops},{operation_id,live_id})
        self.job['phase']='waiting_empty'
        self.job[update_drain.PAUSE]={'format':1,'phase':'restoring',
            'request_id':self.drain['request_id'],'operations':pause_ops}
        from poolrotater_host.controller_update import GATE
        atomic_json(self.root/GATE,{'format':1,'phase':'locking','nonce':self.job['nonce'],
            'expires_at':self.now+300,'bridge_release':self.parent['release']})
        first=self.restore()
        self.assertEqual(first['phase'],'waiting')
        self.assertFalse((self.root/GATE).exists())
        open_id=str(uuid.uuid5(uuid.UUID(self.drain['request_id']),'restore-open:room1'))
        self.assertEqual(store.operation(open_id)['phase'],'received')
        self.assertEqual(store.room('room4')['desired'],'stopped')

        # Once restore releases the exact gate, the predecessor applies the
        # already accepted offslot setting normally. Resume must recognize only
        # this pinned operation's N→N+1 projection while retaining the old
        # closure receipts and deterministic OPEN operation.
        self.assertEqual(store.begin(operation_id,ready=True)['phase'],'applying')
        store.finish(operation_id,'applied',result={'application':'recreate',
            'revision':room['revision']+1})
        self.assertEqual(store.begin(live_id,ready=True)['phase'],'applying')
        store.finish(live_id,'applied',result={'application':'live',
            'revision':live_room['revision']+1})
        projection.export(store)
        self.job['configuration']=_config_hashes(self.root)
        self.job[update_drain.PAUSE]['phase']='waiting_configuration'
        self.assertEqual(store.begin(open_id,ready=True)['phase'],'applying')
        open_generation='c'*32;open_room_id=7654329
        store.reserve('room1',open_generation,operation=open_id)
        store.mark_dispatched('room1',open_generation)
        store.observe_owned('room1',open_generation,open_room_id)
        store.finish(open_id,'applied',result={'application':'lifecycle','room_id':open_room_id,
            'revision':store.room('room1')['revision']})
        projection.export(store)
        self.job['configuration']=_config_hashes(self.root)
        runtime=self.root/'state/rolling'
        status=read_json(runtime/'status.json');rows=status['rooms']
        new_owned={'phase':'running','operation':'owned','room_id':open_room_id,'pid':8765,
            'generation':open_generation,'release':self.parent['release'],'ownership_unknown':False,
            'error':None,'target':None,'creation_candidate':None,'admission_locked':False,
            'healthy':True,'roster_known':True,'heartbeat_age':1.0,
            'status':{'ready':True,'running':True,'room_id':open_room_id,'state':'IDLE','players':0}}
        rows['room1']=copy.deepcopy(new_owned);status.update(generated_at=self.now)
        atomic_json(runtime/'status.json',status)
        journal=read_json(runtime/'ownership.json');journal['rooms']['room1']=copy.deepcopy(new_owned)
        atomic_json(runtime/'ownership.json',journal)

        # Recovery may resume the same configuration pause after the outer
        # native cancellation has marked the job cancelled/held. Only its
        # exact pause binding and operation receipts authorize this projection.
        self.job['phase']='cancelled'
        pause=self.job[update_drain.PAUSE]
        original_pause_request=pause['request_id']
        pause['request_id']='0'*36
        self.assertEqual(self.restore()['phase'],'requires_review')
        pause['request_id']=original_pause_request
        pause['phase']='cancelling'
        self.assertEqual(self.restore()['phase'],'requires_review')
        pause['phase']='waiting_configuration'
        self.assertEqual(self.restore(process_matches=False)['phase'],'requires_review')
        original_revision=pause['operations'][0]['expected_revision']
        pause['operations'][0]['expected_revision']=original_revision+1
        self.assertEqual(self.restore()['phase'],'requires_review')
        pause['operations'][0]['expected_revision']=original_revision
        uncertain_id=pause['operations'][0]['id']
        from poolrotater_host.diagnostic_logs.common import transaction
        with transaction(store.path) as db:
            db.execute("UPDATE operations SET phase='uncertain' WHERE id=?",(uncertain_id,))
        self.assertEqual(self.restore()['phase'],'requires_review')
        with transaction(store.path) as db:
            db.execute("UPDATE operations SET phase='applied' WHERE id=?",(uncertain_id,))
        second=self.restore()
        self.assertEqual(second['phase'],'complete')
        self.job['phase']='held'
        self.assertEqual(self.restore()['phase'],'complete')
        self.job['phase']='waiting_empty'
        self.assertEqual(store.operation(open_id)['phase'],'applied')
        self.assertEqual(store.room('room4')['desired'],'stopped')
        self.assertEqual(store.room('room4',private=True)['config']['visibility'],'private')
        from poolrotater_host.diagnostic_logs.common import transaction
        with transaction(store.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM operations WHERE alias='room1'").fetchone()[0],1)

        def save(**values):self.job.update(values)
        archive_path=self.prefix/'drain-history-test.json'
        def control(path,owner=0,*,private=False,optional=False,**kwargs):
            if Path(path)==self.prefix/NAME:return copy.deepcopy(self.mailbox)
            path=Path(path)
            if not path.exists():
                if optional:return None
                raise FileNotFoundError(path)
            return json.loads(path.read_text())
        wall=time.time()
        tick_at=wall-0.005;refreshed_at=wall-0.001
        clock_values=iter((tick_at,wall-0.004,wall-0.003,wall,wall+0.1))
        original_restore=update_drain.restore
        def republish_between_restore_and_archive(*args,**kwargs):
            result=original_restore(*args,**kwargs)
            receipt_path=self.root/'state/rolling/operator-maintenance.json'
            receipt=read_json(receipt_path);receipt['updated_at']=refreshed_at
            atomic_json(receipt_path,receipt)
            return result
        with patch.object(update_drain,'restore',side_effect=republish_between_restore_and_archive), \
             patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_history_file',return_value=archive_path), \
             patch.object(update_drain,'_write',side_effect=lambda path,value,**kwargs:atomic_json(path,value)), \
             patch.object(update_drain,'_control',side_effect=control), \
             patch('poolrotater_host.ratings.operations._identity'), \
             patch('poolrotater_host.bookings.policy.holds_updates',return_value=False), \
             patch('poolrotater_host.bookings.update_guard.holds_alias',return_value=False):
            resumed=update_drain.advance_configuration_pause(
                {'root':self.root,'prefix':self.prefix},self.parent,self.job,save,
                now=lambda:next(clock_values))
        self.assertEqual(resumed['phase'],'requalified')
        self.assertIsNone(self.job.get(update_drain.RECORD))
        self.assertEqual(self.job['controller_update_drain_round'],1)
        self.assertEqual(self.job[update_drain.HISTORY][-1]['request_id'],self.drain['request_id'])
        self.assertEqual(self.job[update_drain.HISTORY][-1]['terminal_receipt']['updated_at'],refreshed_at)
        with patch.object(update_drain,'_history_file',return_value=archive_path), \
             patch.object(update_drain,'_control',side_effect=control):
            self.assertTrue(update_drain._history_matches(
                {'prefix':self.prefix},self.parent,self.job,self.drain['request_id'],
                read_json(self.root/RECEIPT)))
        # A fresh round uses a distinct deterministic mailbox ID. The prior
        # cancelled mailbox/receipt may be replaced only because its exact
        # terminal proof was archived above.
        next_projection=projection.read(self.root,load(self.root)[0])
        next_initial={'room1':initial_alias()}
        next_initial['room1']['managed']={**next_initial['room1']['managed'],
            'revision':store.room('room1')['revision'],'room_id':open_room_id,
            'room_generation':store.room('room1')['room_generation']}
        next_initial['room1']['identity']={**new_owned,
            'pid':new_owned['pid'],'generation':open_generation,'room_id':open_room_id,
            'release':self.parent['release']}
        value={'rooms':['room1'],'initial':next_initial,
            'projection_sha256':update_drain.fingerprint(next_projection),
            'eligibility_sha256':'f'*64,'projection':next_projection}
        def read_control(path,owner=0,*,private=False,optional=False,**kwargs):
            path=Path(path)
            if path==self.prefix/NAME and path.exists():return json.loads(path.read_text())
            if path==self.prefix/NAME:return copy.deepcopy(self.mailbox)
            if not path.exists():
                if optional:return None
                raise FileNotFoundError(path)
            return json.loads(path.read_text())
        with patch.object(update_drain,'_process_matches',return_value=True), \
             patch.object(update_drain,'_configuration_sha',return_value=fingerprint(self.job['configuration'])), \
             patch.object(update_drain,'_control',side_effect=read_control), \
             patch.object(update_drain,'_history_file',return_value=archive_path), \
             patch.object(update_drain,'_write',side_effect=lambda path,value,**kwargs:atomic_json(path,value)), \
             patch.object(update_drain,'_collect',return_value=(['room1'],next_initial,
                value['projection_sha256'],value['eligibility_sha256'],next_projection)), \
             patch.object(update_drain,'_eligibility_sha',return_value=value['eligibility_sha256']), \
             patch('poolrotater_host.controller_update._gate',return_value=True):
            next_request=update_drain.request({'root':self.root,'prefix':self.prefix},
                self.parent,self.job,save,now=lambda:self.now+1)
        self.assertEqual(next_request['phase'],'requested')
        self.assertNotEqual(next_request['request_id'],self.drain['request_id'])
        self.assertEqual(self.job[update_drain.RECORD]['round'],1)

    def test_pending_config_for_already_closed_generation_is_refused_then_room_reopens(self):
        from poolrotater_host.managed_host import policy
        store=policy.state(self.root);room=store.room('room1',private=True)
        operation_id=str(uuid.uuid4())
        request={'protocol_version':1,'policy_version':1,'id':operation_id,**store.binding,
            'alias':'room1','actor':'fixture-stale-generation-config','role':'FLEET_ADMIN',
            'action':'configure','expected_revision':room['revision'],
            'expected_room_generation':room['room_generation'],'timing':'immediate',
            'patch':{'countdown_seconds':12},'expires_at':self.now+600,
            'cancel_operation_id':None}
        store.enqueue(request);store.begin(operation_id,ready=False)
        with patch('poolrotater_host.ratings.operations._identity'):
            pinned=update_drain._pending_configuration_work(self.root,
                self.parent['service_uid'],self.drain,now=self.now)
        self.assertEqual([row['id'] for row in pinned],[operation_id])
        self.job['phase']='waiting_empty'
        self.job[update_drain.PAUSE]={'format':1,'phase':'restoring',
            'request_id':self.drain['request_id'],'operations':pinned}
        first,uid,database_scopes,_identity_context=self._restore_with_identity_scope_capture()
        self.assertEqual(first['phase'],'waiting')
        self.assertTrue(database_scopes)
        self.assertTrue(all(value==uid for value in database_scopes))
        self.assertIsNone(store.room('room1')['room_generation'])
        self.assertIsNone(store.room('room1')['room_id'])
        # Native generation CAS rejects the stale request after exact closure
        # settlement; the helper does not rewrite or replay its payload.
        rejected=store.begin(operation_id,ready=True)
        self.assertEqual(rejected['phase'],'refused')
        self.assertEqual(rejected['failure_code'],'room_generation_changed')
        second,uid,database_scopes,_identity_context=self._restore_with_identity_scope_capture()
        self.assertEqual(second['phase'],'waiting')
        self.assertTrue(database_scopes)
        self.assertTrue(all(value==uid for value in database_scopes))
        open_id=str(uuid.uuid5(uuid.UUID(self.drain['request_id']),'restore-open:room1'))
        self.assertEqual(store.operation(open_id)['phase'],'received')
        self.assertEqual(store.operation(operation_id)['phase'],'refused')
        self.assertEqual(store.room('room1')['desired'],'running')

    def test_terminal_partial_drain_creates_one_durable_native_open_and_replays_it(self):
        first=self.restore()
        self.assertEqual(first['phase'],'waiting')
        from poolrotater_host.managed_host import policy
        store=policy.state(self.root);op_id=str(uuid.uuid5(uuid.UUID(self.drain['request_id']),'restore-open:room1'))
        op=store.operation(op_id)
        self.assertEqual(op['phase'],'received')
        self.assertEqual(store.room('room1')['desired'],'running')
        self.assertIsNone(store.room('room1')['room_generation'])
        self.assertEqual(store.room('room2')['room_generation'],self.drain['initial']['room2']['managed']['room_generation'])
        second=self.restore()
        self.assertEqual(second['phase'],'waiting')
        self.assertEqual(store.operation(op_id)['phase'],'received')
        from poolrotater_host.diagnostic_logs.common import transaction
        with transaction(store.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operations WHERE alias='room1'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM operations WHERE alias='room2'").fetchone()[0],0)

    def _apply_restore_open_with_owned_journal(self):
        from poolrotater_host.managed_host import policy,projection
        first=self.restore()
        self.assertEqual(first['phase'],'waiting')
        store=policy.state(self.root)
        open_id=str(uuid.uuid5(uuid.UUID(self.drain['request_id']),'restore-open:room1'))
        self.assertEqual(store.operation(open_id)['phase'],'received')
        self.assertEqual(store.begin(open_id,ready=True)['phase'],'applying')
        generation='c'*32;room_id=7654331
        store.reserve('room1',generation,operation=open_id)
        store.mark_dispatched('room1',generation)
        store.observe_owned('room1',generation,room_id)
        store.finish(open_id,'applied',result={'application':'lifecycle','room_id':room_id,
            'revision':store.room('room1')['revision']})
        projection.export(store)
        from poolrotater_host.controller_update import _config_hashes
        self.job['configuration']=_config_hashes(self.root)
        runtime=self.root/'state/rolling'
        journal=read_json(runtime/'ownership.json')
        owned={'phase':'running','operation':'owned','room_id':room_id,'pid':8766,
            'generation':generation,'release':self.parent['release'],'ownership_unknown':False,
            'error':None,'target':None,'creation_candidate':None,'admission_locked':False}
        journal['rooms']['room1']=copy.deepcopy(owned)
        atomic_json(runtime/'ownership.json',journal)
        return store,open_id,generation,room_id,owned,runtime

    def test_applied_open_waits_for_health_snapshot_then_completes_without_reopening(self):
        from poolrotater_host.diagnostic_logs.common import transaction
        store,open_id,generation,room_id,owned,runtime=self._apply_restore_open_with_owned_journal()
        # This is a fresh status document that still contains the exact clean
        # parked identity captured before the deterministic OPEN finished.
        result=self.restore()
        self.assertEqual(result['phase'],'waiting')
        self.assertEqual(result['error'],'update_drain_restore_open_startup_pending')
        self.assertEqual(result['rooms']['room1'],'waiting_for_startup_observation')
        self.assertEqual(store.operation(open_id)['phase'],'applied')
        with transaction(store.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operations WHERE alias='room1'").fetchone()[0],1)

        status=read_json(runtime/'status.json');ready={**owned,'healthy':True,'roster_known':True,
            'heartbeat_age':0.5,'status':{'ready':True,'running':True,'room_id':room_id,
                'state':'IDLE','players':0}}
        status['generated_at']=self.now;status['rooms']['room1']=ready
        atomic_json(runtime/'status.json',status)
        result=self.restore()
        self.assertEqual(result['phase'],'complete')
        self.assertIsNone(result['error'])
        self.assertEqual(store.operation(open_id)['phase'],'applied')
        with transaction(store.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operations WHERE alias='room1'").fetchone()[0],1)

    def test_applied_open_still_refuses_foreign_or_unknown_status_ownership(self):
        store,open_id,generation,room_id,owned,runtime=self._apply_restore_open_with_owned_journal()
        status=read_json(runtime/'status.json')
        status['generated_at']=self.now
        foreign={**owned,'generation':'d'*32,'room_id':7654332,'pid':8767}
        status['rooms']['room1']=foreign
        atomic_json(runtime/'status.json',status)
        self.assertEqual(self.restore()['phase'],'requires_review')

        status['rooms']['room1']={**owned,'ownership_unknown':True}
        atomic_json(runtime/'status.json',status)
        self.assertEqual(self.restore()['phase'],'requires_review')
        self.assertEqual(store.operation(open_id)['phase'],'applied')

    def test_expiry_receipt_written_after_request_deadline_can_restore_with_fresh_status(self):
        late=self.drain['expires_at']+1
        runtime=self.root/'state/rolling'
        status=read_json(runtime/'status.json');status['generated_at']=late-1
        atomic_json(runtime/'status.json',status)
        receipt=read_json(runtime/'operator-maintenance.json')
        receipt.update(phase='expired',updated_at=late)
        atomic_json(runtime/'operator-maintenance.json',receipt)
        result=self.restore(now=late)
        self.assertEqual(result['phase'],'waiting')

    def test_restore_samples_clock_after_receipt_read_but_rejects_real_future_or_bad_receipts(self):
        receipt_path=self.root/'state/rolling/operator-maintenance.json'
        receipt=read_json(receipt_path);receipt['updated_at']=self.now+0.1
        atomic_json(receipt_path,receipt)
        moments=iter((self.now,self.now+0.2,self.now+0.2))
        self.assertEqual(self.restore(now=lambda:next(moments))['phase'],'waiting')

        for mutate in (
            lambda value:value.update(updated_at=self.now+0.1),
            lambda value:value.update(updated_at=self.drain['created_at']-1),
            lambda value:value.update(controller_pid=self.job['process']['pid']+1),
            lambda value:value.update(rooms={'room1':'closed','room2':'cancelled'}),
        ):
            receipt=read_json(receipt_path);mutate(receipt);atomic_json(receipt_path,receipt)
            with self.subTest(receipt=receipt):
                self.assertEqual(self.restore(now=self.now)['phase'],'requires_review')

    def test_replays_open_after_crash_between_exact_reservation_settlement_and_enqueue(self):
        from poolrotater_host.managed_host import policy
        store=policy.state(self.root)
        store.confirmed_closed('room1','a'*32,7654321)
        result=self.restore()
        self.assertEqual(result['phase'],'waiting')
        op_id=str(uuid.uuid5(uuid.UUID(self.drain['request_id']),'restore-open:room1'))
        self.assertEqual(store.operation(op_id)['phase'],'received')
        self.assertEqual(store.room('room1')['revision'],self.drain['initial']['room1']['managed']['revision'])

    def test_all_closed_slots_settle_through_existing_closed_journal_reconciler(self):
        from poolrotater_host.managed_host import policy
        from poolrotater_host.diagnostic_logs.common import transaction
        runtime=self.root/'state/rolling'
        rows=read_json(runtime/'status.json')['rooms']
        rows['room2']={'phase':'parked','operation':'closed','room_id':None,'pid':None,
            'generation':'b'*32,'release':self.parent['release'],'ownership_unknown':False,
            'error':None,'target':None,'creation_candidate':None,'admission_locked':False}
        atomic_json(runtime/'status.json',{'generated_at':self.now-1,'configuration_error':None,'rooms':rows})
        atomic_json(runtime/'ownership.json',{'format':1,'rooms':copy.deepcopy(rows)})
        progress=read_json(runtime/'operator-maintenance.json')
        progress['rooms']['room2']='closed'
        progress['closure_evidence']['room2']={'room_id':7654322,'mode':'empty',
            'notice_confirmed':False,'active_map_observed':False,'require_empty':True,
            'closure_started_at':self.now-10}
        atomic_json(runtime/'operator-maintenance.json',progress)
        result=self.restore()
        self.assertEqual(result['phase'],'waiting')
        store=policy.state(self.root)
        with transaction(store.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reservations').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM operations').fetchone()[0],2)
