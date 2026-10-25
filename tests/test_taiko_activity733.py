"""Taiko participation collection, mode totals, and mixed-replica guards."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import sqlite3
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from poolrotater_host.activity.collector import Collector
from poolrotater_host.activity.contracts import event
from poolrotater_host.activity.query import player
from poolrotater_host.activity.replica import Publisher, Replica, SCHEME
from poolrotater_host.activity.storage import connection
from poolrotater_host.layout import setup
from poolrotater_host.network.contracts import fingerprint


class TaikoActivity733Tests(unittest.TestCase):
    def test_taiko_activity_event_and_player_totals_are_ruleset_separated(self):
        item={'event_id':str(uuid.uuid4()),'installation_id':str(uuid.uuid4()),
              'generation':str(uuid.uuid4()),'consent_id':str(uuid.uuid4()),
              'kind':'achievement','payload':{
                  'room_id':9,'mode':'taiko','user_id':7,'username':'Taiko User',
                  'achievement_key':'room:9:item:5:user:7','beatmap_id':20,
                  'playlist_item_id':5,'created_at':100,'profile_gain_pp':12.5,
                  'score_pp':150.0,'accuracy':98.0,'top_rank':1,'historical':False}}
        self.assertIs(event(item), item)
        achievement={**item['payload'],'host_id':str(uuid.uuid4()),
                     'event_id':item['event_id']}
        standard={**achievement,'mode':'standard','achievement_key':'std-key',
                  'profile_gain_pp':99.0}
        snapshot={'hidden_user_ids':set(),'hosts':[{'host_id':achievement['host_id'],
                    'enabled':True,'updated_at':100}], 'presence':[],'joins':[],
                  'leaves':[],'matches':[],'baselines':[],
                  'achievements':[achievement,standard]}
        taiko=player(snapshot,7,ruleset_id=1)
        standard_stats=player(snapshot,7,ruleset_id=0)
        self.assertEqual(taiko['total_gain'],12.5)
        self.assertEqual(standard_stats['total_gain'],99.0)

    def test_collector_reads_taiko_stats_namespace_with_authoritative_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'state').mkdir()
            ledger=root/'ledger.db';marker={'installation_id':str(uuid.uuid4()),
                                             'generation':str(uuid.uuid4())}
            collector=Collector(root,ledger,clock=lambda:200,marker=marker)
            profile=collector.configure(share_statistics=True)
            with sqlite3.connect(ledger) as db:
                db.execute('CREATE TABLE activity_visibility(id INTEGER PRIMARY KEY,room_id INTEGER,public INTEGER,observed_at REAL,mode TEXT)')
                db.execute("INSERT INTO activity_visibility(room_id,public,observed_at,mode) VALUES(9,1,50,'taiko')")
                db.execute('CREATE TABLE autohost_pp_achievements_taiko(achievement_key TEXT PRIMARY KEY,user_id INTEGER,username TEXT,beatmap_id INTEGER,room_id INTEGER,playlist_item_id INTEGER,score_pp REAL,profile_gain_pp REAL,top_rank INTEGER,accuracy REAL,created_at REAL)')
                db.execute("INSERT INTO autohost_pp_achievements_taiko VALUES('room:9:item:5:user:7',7,'Taiko User',20,9,5,150,12.5,1,98,201)")
            with Collector._source(ledger) as source, connection(collector.path) as target:
                self.assertEqual(collector._achievements(source,target,profile,set()),1)
            events=collector.batch()['events']
            self.assertEqual(len(events),1)
            self.assertEqual(events[0]['payload']['mode'],'taiko')
            self.assertEqual(events[0]['payload']['profile_gain_pp'],12.5)

    def test_replica_generations_keep_old_rows_and_require_v3_for_taiko(self):
        publisher=Publisher.__new__(Publisher)
        publisher.projection=None;publisher.stamp=None
        publisher.include_catch=False;publisher.include_taiko=False
        user=7;host=str(uuid.uuid4());event_id=str(uuid.uuid4())
        taiko={'user_id':user,'username':'Taiko User','mode':'taiko','host_id':host,
               'event_id':event_id,'achievement_key':'room:9:item:5:user:7',
               'room_id':9,'playlist_item_id':5,'beatmap_id':20,'created_at':100,
               'profile_gain_pp':12.5,'score_pp':150.0,'accuracy':98.0,'top_rank':1,
               'historical':False}
        standard={**taiko,'mode':'standard','achievement_key':'std-key','profile_gain_pp':99.0}
        snapshot={'hidden_user_ids':set(),'hosts':[{'host_id':host,'enabled':True,'updated_at':100}],
                  'presence':[],'joins':[],'leaves':[],'matches':[],'baselines':[],
                  'achievements':[taiko,standard],'coverage':{},'generated_at':100}
        publisher.repository=SimpleNamespace(snapshot=lambda **_kwargs:snapshot)
        publisher._project(str(uuid.uuid4()),'a'*64,3,100)
        self.assertEqual({row['ruleset_id'] for row in publisher.projection},{0,3})
        self.assertEqual({row['player']['total_gain'] for row in publisher.projection}, {0.0,99.0})
        publisher._project(str(uuid.uuid4()),'a'*64,3,100,include_catch=True)
        self.assertEqual({row['ruleset_id'] for row in publisher.projection},{0,2,3})
        self.assertNotIn(1,{row['ruleset_id'] for row in publisher.projection})
        publisher._project(str(uuid.uuid4()),'a'*64,3,100,include_catch=True,include_taiko=True)
        self.assertEqual({row['ruleset_id'] for row in publisher.projection},{0,1,2,3})
        taiko_stats=next(row['player'] for row in publisher.projection if row['ruleset_id']==1)
        self.assertEqual(taiko_stats['total_gain'],12.5)

    def test_old_verified_snapshot_does_not_answer_taiko_as_zero(self):
        replica=Replica.__new__(Replica);replica.root=Path('.')
        @contextmanager
        def metadata(): yield object()
        with patch.object(replica,'metadata',metadata), \
             patch.object(replica,'observation',return_value={'phase':'ready','manifest':{'scheme':SCHEME}}), \
             patch.object(replica,'database',side_effect=AssertionError('old cache cannot answer Taiko')):
            self.assertEqual(replica.stats(7,1),(False,None))

    def test_managed_taiko_projection_negotiates_v3_from_a_standard_host_file(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'host';setup(root,['standard-fixed'])
            host=str(uuid.uuid4());epoch=str(uuid.uuid4());now=1000.0
            manifest={'ok':True,'format':1,'scheme':'player-totals-v3',
                      'server_epoch':epoch,'host_id':host,'policy_id':'a'*64,
                      'watermark':0,'generated_at':now,'rows':0,'pages':[]}
            manifest['content_id']=fingerprint(manifest)
            class Network:
                def __init__(self):self.routes=[]
                def profile(self):return {}
                def credential(self):return {'credential':'test','host_id':host,'server_epoch':epoch}
                def transport(self,_profile):return self
                def request(self,method,path,**_kwargs):
                    self.routes.append(path)
                    return manifest
            network=Network()
            projection={'rooms':{'owner':{'settings':{'preset':'taiko-fixed'}}}}
            with patch('poolrotater_host.managed_host.projection.read',return_value=projection):
                result=Replica(root,clock=lambda:now).sync(network)
            self.assertEqual(result['phase'],'activated')
            self.assertTrue(network.routes)
            self.assertTrue(all(path.startswith('/v3/activity/replica/') for path in network.routes))
            self.assertNotIn('/v1/activity/replica/current',network.routes)


if __name__=='__main__':
    unittest.main()
