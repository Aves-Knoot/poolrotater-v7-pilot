"""Real native maintenance workers on full catalogs; provider is invented.

Use POOLROTATER_MAINTENANCE_SOURCE for the final frozen source directory. No
production mounts, credentials, journal or external network enters the fixture.
"""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from tests import test_rolling as native
from tests import worker_responsiveness_full_catalog_fixture as catalogs
from poolrotater_host import operator_maintenance as operations
from poolrotater_host.layout import read_json

ROOT=Path(__file__).resolve().parents[1]


class FullCatalogMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='maintenance-full-catalog-')
        cls.root=Path(cls.temp.name)
        source=Path(os.environ.get('POOLROTATER_MAINTENANCE_SOURCE',str(ROOT)))
        release=cls.root/'v7.1.8'
        for name in native.source_files(source):
            out=release/name;out.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source/name,out)
        native.seal(release,'v7.1.8')
        cls.releases=[release]

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    cleanup_supervisor=native.ProcessIntegrationTests.cleanup_supervisor
    start=native.ProcessIntegrationTests.start

    def setUp(self):
        catalogs.FullCatalogWorkerTests.setUp(self)
        self.parent={'prefix':str(self.runtime/'protected-prefix'),'installation_id':str(uuid.uuid4()),
            'generation':str(uuid.uuid4()),'service_uid':os.geteuid(),'bundle_sha256':'a'*64}
        self.supervisor.protected_parent=self.parent
        # The external provider fixture must implement the score endpoint used
        # by a real completed match. The native scoring timeout is unchanged.
        self.supervisor.api.room_item_scores=lambda *args,**kwargs:{'scores':[]}
        self.job=None
        def control(path,*args,**kwargs):
            if Path(path).name==operations.NAME and Path(path).parent.name=='protected-prefix':
                return copy.deepcopy(self.job)
            return read_json(path) if Path(path).exists() else None
        self.enterContext(patch.object(operations,'_control',side_effect=control))

    def wait(self,predicate,label,seconds=30,*,poll=True):
        deadline=time.monotonic()+seconds
        while not predicate():
            if time.monotonic()>=deadline:
                self.fail(label+': '+json.dumps({a:w.status for a,w in self.supervisor.workers.items()}))
            if poll:operations.poll(self.supervisor)
            time.sleep(.1)

    def request(self,mode):
        now=time.time()
        self.job={'format':1,'id':str(uuid.uuid4()),'phase':'requested','mode':mode,'rooms':['room1'],
            'created_at':now,'expires_at':now+300,**{k:self.parent[k]for k in ('installation_id','generation')},
            'parent_sha256':self.parent['bundle_sha256']}

    def test_explicit_one_map_closes_only_after_real_completion_and_preserves_playing_sibling(self):
        a,b=self.start('room1'),self.start('room2')
        ids={w.alias:w.status['room_id']for w in (a,b)}
        for worker in (a,b):
            self.hub.shared.route('UserJoined',{'room_id':worker.status['room_id'],'user_id':7})
            self.hub.shared.route('MatchStarted',{'room_id':worker.status['room_id']})
        self.wait(lambda:all(w.status.get('state')=='PLAYING'for w in (a,b)),'both playing',poll=False)
        self.request('one-map')
        self.wait(lambda:a.status.get('operator_maintenance',{}).get('notice_confirmed'),'warning confirmed')
        self.assertIn(ids['room1'],self.hub.rooms)
        self.assertEqual(b.status['state'],'PLAYING')
        self.hub.shared.route('MatchCompleted',{'room_id':ids['room1']})
        self.wait(lambda:self.supervisor.registry.snapshot()['rooms']['room1'].get('phase')=='parked',
            'requested map completed and target closed',seconds=50)
        self.assertNotIn(ids['room1'],self.hub.rooms)
        self.assertIn(ids['room2'],self.hub.rooms)
        self.assertIsNone(b.process.poll());self.assertEqual(b.status['state'],'PLAYING')
        self.assertFalse(any(method=='KickPlayer'for method,args in self.hub.calls))
        receipt=read_json(self.runtime/operations.RECEIPT)
        self.assertTrue(receipt['closure_evidence']['room1']['notice_confirmed'])
        print(json.dumps({'ok':True,'full_catalog_rows':self.counts,'one_map_actual_native_completion':True,
            'occupied_playing_sibling_retained':True,'provider_invented':True}))

    def test_late_join_and_cancellation_keep_same_empty_drain_worker_and_room(self):
        a=self.start('room1');rid=a.status['room_id'];pid=a.process.pid
        self.request('empty')
        # Deliver through the controller, then stop polling before the closure
        # boundary so the independent referee roster can receive a late join.
        operations.poll(self.supervisor)
        self.wait(lambda:a.status.get('operator_maintenance',{}).get('phase')=='ready','empty main-thread pause',poll=False)
        self.hub.shared.route('UserJoined',{'room_id':rid,'user_id':7})
        self.wait(lambda:a.status.get('players')==1,'late join observed',poll=False)
        operations.poll(self.supervisor)
        self.assertIn(rid,self.hub.rooms);self.assertIsNone(a.process.poll())
        self.job['phase']='cancelled'
        self.wait(lambda:read_json(self.runtime/operations.RECEIPT).get('phase')=='cancelled','cancellation acknowledged')
        self.assertEqual(a.process.pid,pid);self.assertEqual(a.status['room_id'],rid)
        self.assertFalse(any(method=='CloseRoom'for method,args in self.hub.calls))
        print(json.dumps({'ok':True,'full_catalog_rows':self.counts,'native_late_join_cancelled_close':True,
            'worker_and_room_unchanged':True,'provider_invented':True}))

    def test_cancel_after_notice_does_not_close_or_abort_current_map(self):
        a=self.start('room1');rid=a.status['room_id']
        self.hub.shared.route('UserJoined',{'room_id':rid,'user_id':7})
        self.hub.shared.route('MatchStarted',{'room_id':rid})
        self.wait(lambda:a.status.get('state')=='PLAYING','match running',poll=False)
        self.request('one-map')
        self.wait(lambda:a.status.get('operator_maintenance',{}).get('notice_confirmed'),'warning confirmed')
        self.job['phase']='cancelled'
        self.wait(lambda:read_json(self.runtime/operations.RECEIPT).get('phase')=='cancelled','cancel applied')
        self.assertEqual(a.status['state'],'PLAYING');self.assertIn(rid,self.hub.rooms)
        self.assertFalse(any(method in ('CloseRoom','AbortMatch','KickPlayer')for method,args in self.hub.calls))

    def test_one_map_empty_room_closes_without_waiting_for_players_or_a_match(self):
        a=self.start('room1');rid=a.status['room_id']
        self.assertEqual(a.status['players'],0)
        self.request('one-map')
        receipt=self.runtime/operations.RECEIPT
        self.wait(lambda:receipt.exists() and read_json(receipt).get('phase')=='complete','empty one-map maintenance')
        self.assertNotIn(rid,self.hub.rooms)
        self.assertFalse(any(method in ('StartMatch','AbortMatch','KickPlayer')for method,args in self.hub.calls))


if __name__=='__main__':unittest.main()
