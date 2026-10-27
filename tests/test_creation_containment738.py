"""Unknown creation is fenced, never replayed or declared absent."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from poolrotater_host.creation_containment import isolate, CONTAINED, REFUSED, DEFERRED
from poolrotater_host.layout import atomic_json, read_json
from poolrotater_host.provider_service import run_provider_service
from poolrotater_host.config import ConfigError
from poolrotater_host.testing.fixtures import create_catalogs
from rolling.state import Registry
import test_v7_provider_rooms as rooms
from test_v7_runtime import fixture_release


class FakeAuthority:
    def __init__(self, root): self.root = root


class FakeClient:
    def __init__(self, auth):
        self.auth = auth
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._session = object()
        self._manager = SimpleNamespace(is_alive=lambda: True)
        self._cleanup_failed = False
        self._connect_generation = 1
        self._boundary = lambda: None


class ContainmentPolicyTests(unittest.TestCase):
    """Predicate negatives use invented parent objects; native tests follow."""
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch('poolrotater_host.provider_authorization.ProviderOAuth', FakeAuthority))
        self.enterContext(patch('poolrotater_host.provider_referee.ProviderRefereeClient', FakeClient))
        self.authority = self.enterContext(patch('poolrotater_host.provider_rooms._authority'))
        self.auth = FakeAuthority(self.root)
        self.hub = SimpleNamespace(client=FakeClient(self.auth), is_connected=lambda: True)
        self.registry = Registry(self.root/'ownership.json')
        self.registry.record('room1', phase='recovery_required', operation='make_pending',
                             ownership_unknown=True, room_id=None, channel_id=None,
                             generation='a'*32, pid=101, creation_candidate=None)
        self.registry.record('room2', phase='running', operation='owned', ownership_unknown=False,
                             room_id=202, generation='b'*32, pid=102)
        self.worker = SimpleNamespace(alias='room1', process=SimpleNamespace(pid=101,poll=lambda:2),
            broker=SimpleNamespace(generation='a'*32, uncertain=True, active=True, fenced=False,
                                   hub=SimpleNamespace(owned=set(), creating=False)))
        self.supervisor = SimpleNamespace(runtime=self.root, registry=self.registry,
            workers={'room1':self.worker}, shared=SimpleNamespace(hub=self.hub,lock=threading.RLock(),
                stopping=threading.Event()), rollout_alias=None, stopping=threading.Event())
        self.disposals = 0
        def dispose(worker):
            self.disposals += 1
            worker.broker.fenced = True
            worker.broker.active = False
        self.supervisor.dispose = dispose
        self.supervisor.record = self.registry.record

    def call(self): return isolate(self.supervisor,self.worker,self.auth,self.hub)

    def test_retains_candidate_unknown_and_occupied_sibling_then_is_idempotent(self):
        candidate={'format':1,'room_id':303,'connection_generation':1,'observed_at':time.time()}
        self.registry.record('room1',creation_candidate=candidate)
        sibling=copy.deepcopy(self.registry.snapshot()['rooms']['room2'])
        self.assertEqual(self.call(),CONTAINED)
        row=self.registry.snapshot()['rooms']['room1']
        self.assertTrue(row['ownership_unknown']);self.assertEqual(row['operation'],'make_pending')
        self.assertEqual(row['creation_candidate'],candidate);self.assertIsNone(row['pid'])
        self.assertEqual(self.registry.snapshot()['rooms']['room2'],sibling)
        self.assertEqual(self.call(),CONTAINED);self.assertEqual(self.disposals,1)
        self.hub.client._connect_generation=2;self.hub.client._session=object()
        self.assertEqual(self.call(),CONTAINED);self.assertEqual(self.disposals,1)
        with self.assertRaises(RuntimeError):self.registry.require_clean_start()

    def test_does_not_isolate_alive_foreign_or_adopted_room(self):
        for patch_row in ({'phase':'running'},{'operation':'close_pending'},
                          {'ownership_unknown':False},{'room_id':101},{'channel_id':101},
                          {'generation':'c'*32},{'pid':999}):
            with self.subTest(patch_row=patch_row):
                old=self.registry.snapshot()['rooms']['room1']
                self.registry.record('room1',**patch_row)
                self.assertEqual(self.call(),REFUSED);self.assertEqual(self.disposals,0)
                self.registry.record('room1',**{k:v for k,v in old.items()if k!='updated_at'})
        self.worker.process.poll=lambda:None
        self.assertEqual(self.call(),REFUSED)
        self.worker.process.poll=lambda:2;self.worker.broker.hub.owned.add(101)
        self.assertEqual(self.call(),REFUSED)
        self.worker.broker.hub.owned.clear();self.worker.broker.hub.creating=True
        self.assertEqual(self.call(),REFUSED)

    def test_current_authority_and_identity_are_required(self):
        self.authority.side_effect=RuntimeError('invented authority refusal')
        self.assertEqual(self.call(),REFUSED);self.assertEqual(self.disposals,0)
        self.authority.side_effect=None;self.hub.client.auth=FakeAuthority(self.root)
        self.assertEqual(self.call(),REFUSED)
        self.hub.client.auth=self.auth;self.supervisor.rollout_alias='room1'
        self.assertEqual(self.call(),REFUSED)

    def test_shared_failure_is_not_relabelled_as_contained(self):
        self.hub.client._cleanup_failed=True
        self.assertEqual(self.call(),REFUSED)
        self.hub.client._cleanup_failed=False;self.hub.client._stop.set()
        self.assertEqual(self.call(),REFUSED)
        self.hub.client._stop.clear();self.hub.client._manager=SimpleNamespace(is_alive=lambda:False)
        self.assertEqual(self.call(),REFUSED)
        self.hub.client._manager=SimpleNamespace(is_alive=lambda:True);self.hub.is_connected=lambda:False
        self.assertEqual(self.call(),DEFERRED);self.assertEqual(self.disposals,0)

    def test_busy_route_does_not_block_or_change_the_journal(self):
        held=threading.Event();release=threading.Event()
        def hold():
            with self.supervisor.shared.lock:held.set();release.wait(3)
        thread=threading.Thread(target=hold);thread.start();self.assertTrue(held.wait(1))
        before=self.registry.path.read_bytes();start=time.monotonic()
        try:self.assertEqual(self.call(),DEFERRED)
        finally:release.set();thread.join(2)
        self.assertLess(time.monotonic()-start,1)
        self.assertEqual(before,self.registry.path.read_bytes());self.assertEqual(self.disposals,0)

    def test_changed_receipt_or_failed_retirement_does_not_claim_success(self):
        self.worker._creation_containment=('foreign',101)
        self.assertEqual(self.call(),REFUSED)
        del self.worker._creation_containment
        def refused(worker):raise OSError('invented local cleanup failure')
        self.supervisor.dispose=refused
        with self.assertRaises(OSError):self.call()
        self.assertTrue(self.registry.snapshot()['rooms']['room1']['ownership_unknown'])
        self.assertFalse(hasattr(self.worker,'_creation_containment'))


class NativeCreationContainmentTests(rooms.RoomCase):
    def setUp(self):
        super().setUp()
        host=read_json(self.root/'config/host.json')
        for room in host['rooms']:
            if room['preset']=='standard-fixed':room.update(sr_min=4.5,sr_max=5.5)
        atomic_json(self.root/'config/host.json',host)
        create_catalogs(self.root)
        self.release=fixture_release(self.root,'v7.0.0')
        self.auth=self.authorized();self.backend,self.api,self.hub=self.adapters(self.auth)
        self.enterContext(patch('rolling.supervisor.require_immutable'))

    def exercise(self, *, after_create=False, shared_failure=False):
        result={'injected':False,'isolated_ticks':0,'reply_sent':False,'proved':False}
        phases=[];captured={};original_messages=self.api.chat_messages
        def messages(channel,*args,**kwargs):
            value=original_messages(channel,*args,**kwargs)
            if result['isolated_ticks'] and channel==captured.get('channel') and not result['reply_sent']:
                result['reply_sent']=True
                return [*(value or []),{'message_id':99001,'sender_id':7,'content':'.help'}]
            return value
        self.api.chat_messages=messages
        def observer(phase,supervisor,workers):
            phases.append(phase)
            if phase!='running':return
            affected,*siblings=list(workers);occupied=siblings[0];rid=occupied.status['room_id']
            self.backend.rooms[rid]['players'].append({'user_id':7,'status':'idle'})
            self.backend.wire.route('UserJoined',{'room_id':rid,'user_id':7})
            self.backend.rooms[rid]['state']='playing'
            self.backend.wire.route('MatchStarted',{'room_id':rid,
                'playlist_item_id':self.backend.rooms[rid]['playlist'][0]['id']})
            captured.update(channel=occupied.broker.channel,affected=affected,siblings=siblings,rid=rid)
            original=affected.peer.handler
            def reject_replacement(method,*args,**kwargs):
                if (method=='api.chat_messages' and not result['injected']
                        and occupied.status.get('state')=='PLAYING'):
                    result['injected']=True
                    old,=affected.broker.hub.owned
                    affected.broker.close_room(old,require_empty=True)
                    with supervisor.shared.lock:
                        field='creation_error_after_commit' if after_create else 'completion_error'
                        setattr(self.backend.wire,field,'Error 123: invented private rejection')
                        try:affected.broker.create('invented replacement',1)
                        finally:setattr(self.backend.wire,field,None)
                return original(method,*args,**kwargs)
            affected.peer.handler=reject_replacement
            native_tick=supervisor.tick;started=time.monotonic()
            def tick():
                self.assertLess(time.monotonic()-started,90,'creation containment did not settle')
                native_tick()
                if not getattr(affected,'_creation_containment',None):return
                result['isolated_ticks']+=1
                row=supervisor.registry.snapshot()['rooms'][affected.alias]
                self.assertEqual(row['phase'],'recovery_required');self.assertTrue(row['ownership_unknown'])
                self.assertEqual(row['operation'],'make_pending');self.assertIsNone(row['room_id'])
                self.assertTrue(affected.broker.fenced);self.assertIsNotNone(affected.process.poll())
                self.assertTrue(all(supervisor.workers[w.alias]is w and w.process.poll()is None
                                    and w.status['room_id']in self.backend.rooms for w in siblings))
                self.assertEqual(occupied.status['state'],'PLAYING')
                self.assertIn(7,{p['user_id']for p in self.backend.rooms[rid]['players']})
                makes=[v for v in self.backend.wire.commands if v['target']=='MakeRoom']
                self.assertEqual(len(makes),5)  # 4 startup + exactly one rejected replacement
                self.assertEqual(self.backend.authenticated_calls.count('CloseRoom'),1)
                if shared_failure:
                    self.hub.client._stop.set()
                    result['proved']=True
                    return
                replied=any(channel==captured['channel'] and '.queue'in str(text)
                            for channel,text in self.backend.api.messages)
                if result['isolated_ticks']>=4 and replied:
                    result['proved']=True;supervisor.request_shutdown()
            supervisor.tick=tick
        if shared_failure:
            with self.assertRaises(ConfigError):
                run_provider_service(self.root,self.release,authentication=self.auth,api=self.api,
                                     hub=self.hub,observer=observer)
            # Lost account transport can additionally refuse final closure.
            # Its cleanup exception must not hide the original captured cause
            # or permit us to declare those sibling rooms safely closed.
            detail=read_json(self.root/'state/controller-failure.json')
            self.assertEqual(detail['failure_code'],'provider_worker_ownership_recovery_required')
            self.assertEqual(phases[-1],'failed')
            journal=read_json(self.root/'state/rolling/ownership.json')['rooms']
            self.assertTrue(any(v.get('room_id') or v.get('ownership_unknown') for v in journal.values()))
        else:
            run_provider_service(self.root,self.release,authentication=self.auth,api=self.api,
                                 hub=self.hub,observer=observer)
        self.assertTrue(result['proved']);self.assertTrue(result['injected'])
        row=read_json(self.root/'state/rolling/ownership.json')['rooms'][captured['affected'].alias]
        self.assertTrue(row['ownership_unknown']);self.assertEqual(row['operation'],'make_pending')
        self.assertIsNone(row.get('room_id'));self.assertIsNone(row.get('creation_candidate'))
        self.assertEqual(len([v for v in self.backend.wire.commands if v['target']=='MakeRoom']),5)
        for sibling in captured['siblings']:
            self.assertIsNotNone(sibling.process.poll())
        if not shared_failure:
            self.assertEqual(phases,['running','stopped'])
            self.assertEqual(len(self.backend.rooms),1 if after_create else 0)

    def test_rejection_before_creation_keeps_playing_siblings_and_commands_alive(self):
        self.exercise()

    def test_rejection_after_server_creation_keeps_unknown_room_and_never_replays(self):
        self.exercise(after_create=True)

    def test_shared_authority_stop_after_containment_remains_fatal(self):
        self.exercise(shared_failure=True)
