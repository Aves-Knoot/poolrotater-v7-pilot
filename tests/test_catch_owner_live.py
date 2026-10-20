"""Current owner website code publishes Catch and preserves sibling presentation."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import importlib.util,json,shutil,sqlite3,subprocess,sys,tempfile,time,types,unittest
from pathlib import Path
from unittest.mock import patch
from bot.ratings import RatingStore
from website.statistics_v71 import network_statistics

ROOT=Path(__file__).resolve().parents[1]

class RetainedCommunityTimingTests(unittest.TestCase):
    def test_standard_enrichment_uses_standard_sibling_when_lobby1_is_catch(self):
        owner=module('catch_owner_source_catalog',ROOT/'website/live/documentation/owner_source.py')
        with tempfile.TemporaryDirectory()as td:
            root=Path(td);(root/'config/rooms').mkdir(parents=True)
            (root/'config/host.json').write_text(json.dumps({'rooms':[
                {'alias':'room1','preset':'catch-fixed'},
                {'alias':'room2','preset':'standard-fixed'}]}))
            (root/'config/rooms/room1.json').write_text('{"ruleset_id":2}')
            (root/'config/rooms/room2.json').write_text('{"farm_db":"/root/private.db"}')
            source=owner.Source(True,root,'invented.service',root/'prefix')
            original=owner.bounded_json
            with patch.object(owner,'bounded_json',wraps=original)as read:
                with self.assertRaisesRegex(ValueError,'catalog outside protected namespace'):
                    source.catalog('farm.db')
                self.assertIn(unittest.mock.call(root/'config/rooms/room2.json'),read.call_args_list)
                self.assertNotIn(unittest.mock.call(root/'config/rooms/room1.json'),read.call_args_list)

    def test_owner_catch_is_labeled_and_filterable_on_home_and_dashboard(self):
        now=int(time.time());rooms=[]
        for i in range(1,5):
            rooms.append({'alias':'room'+str(i),'label':'Catch fixed'if i==1 else'Invented sibling',
              'type':'catch'if i==1 else'standard'if i==2 else'mania_7k'if i==3 else'mania_4k',
              'room_id':100+i,'players':0,'state':'IDLE','current_map':None,
              'queue_known':True,'queue':[],'settings':{},'guide':'lobbies:catch'if i==1 else'lobbies:start'})
        feed={'schema':1,'available':True,'generated_at':now,'expires_at':now+60,'rooms':[]}
        value={'status':{**feed,'rooms':rooms,'service':{'online':True}},'community':feed,'activity':{'samples':[]}}
        with tempfile.TemporaryDirectory()as td:
            root=Path(td);path=root/'community.json';path.write_text(json.dumps(feed))
            for name in('home.php','dashboard.php','nowplaying.php'):
                text=(ROOT/'website/live/template'/name).read_text().replace('/srv/poolrotator-wiki/public/community/rooms.json',str(path));(root/name).write_text(text)
            for source,name in [('website/live/presentation.php','presentation.php'),('website/community/community.php','community.php'),('website/live/fixtures/render.php','render.php')]:shutil.copyfile(ROOT/source,root/name)
            def render(surface):return subprocess.check_output(['/usr/bin/php8.3','-n',str(root/'render.php'),surface],input=json.dumps(value).encode()).decode()
            home=render('home');dashboard=render('dashboard')
        self.assertIn('aria-label="Join osu!catch CATCH in osu!"',home)
        self.assertIn('id=lobbies:catch',home)
        self.assertIn('id="room1" data-room-type="catch"',dashboard)
        self.assertIn('data-room-filter="catch"',dashboard)
        self.assertEqual(dashboard.count('data-room-type="standard"'),1)

    def test_sidebar_link_labels_follow_configured_catch_without_changing_siblings(self):
        from deployment import sqlite_readonly
        with tempfile.TemporaryDirectory()as td:
            root=Path(td);(root/'room1.json').write_text('{"ruleset_id":2}')
            source=types.SimpleNamespace(owner=True,config=lambda alias:root/(alias+'.json'),ledger=root/'invented.db')
            fake=types.ModuleType('owner_source');fake.selected=lambda:source
            with patch.dict(sys.modules,{'owner_source':fake,'sqlite_readonly':sqlite_readonly}):
                links=module('catch_sidebar_links',ROOT/'deployment/wiki/lobbies.py')
            descriptors=links.room_descriptors()
        self.assertEqual(descriptors[0],('room1','Catch fixed','lobbies:catch'))
        self.assertEqual(descriptors[1:],list(links.ROOMS[1:]))

    def test_freemod_nominal_timing_survives_metadata_and_php_projection(self):
        from website.community.display_model import playback
        current={'beatmap_id':101,'beatmapset_id':42,'title':'Invented','total_length':120}
        match={'playlist_item_id':99,'beatmap_id':101,'started_at':980,
               'clock_rate':None,'clock_skew_seconds':0}
        estimate=playback('playing',current,match,now=1000,observed_at=1000,fresh=True,
                          settings={'freestyle':True,'required_mods':[]})
        self.assertEqual((estimate['clock_rate'],estimate['nominal_speed']),(1.0,True))
        room={'key':'1'*24,'room_id':42,'title':'Invented Standard','operator':'Invented',
              'preset':'standard-auto','ownership':'community','state':'playing','players':1,
              'current_map':current,'playback':estimate}
        code='require $argv[1]; $room=json_decode(stream_get_contents(STDIN),true); echo prLiveCard($room,true,false,1000);'
        html=subprocess.check_output(['/usr/bin/php8.3','-n','-r',code,str(ROOT/'website/live/presentation.php')],input=json.dumps(room).encode()).decode()
        self.assertIn('Estimated at 1× 0:20 / 2:00',html)
        self.assertIn('data-nominal-speed="true"',html)

    def test_unknown_clock_without_freemod_does_not_invent_a_catch_estimate(self):
        from website.community.display_model import playback
        self.assertIsNone(playback('playing',{'beatmap_id':101,'total_length':120},
            {'playlist_item_id':99,'beatmap_id':101,'started_at':980,'clock_rate':None,'clock_skew_seconds':0},
            now=1000,observed_at=1000,fresh=True,settings={'freestyle':False,'required_mods':[]}))

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

class CatchOwnerLiveTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.now=time.time()
        (self.root/'state/rolling').mkdir(parents=True);(self.root/'config/rooms').mkdir(parents=True)
        source=types.SimpleNamespace(owner=True,root=self.root,ledger=self.root/'state/ratings.db',service='invented.service',
            config=lambda a:self.root/'config/rooms'/(a+'.json'),room_log=lambda a:self.root/'logs'/(a+'.log'),catalog=lambda n:self.root/n)
        fake=types.ModuleType('owner_source');fake.selected=lambda:source
        with patch.dict(sys.modules,{'owner_source':fake,'network_statistics':network_statistics}):
            self.analytics=module('catch_owner_analytics',ROOT/'website/statistics_v71/analytics_exporter.py')
            self.live=module('catch_owner_live',ROOT/'deployment/wiki/live.py')
        self.live.a=self.analytics;self.analytics.OPS=self.root/'ops'
        store=RatingStore(str(source.ledger));self.status={'generated_at':self.now,'rooms':{}}
        for n,mode in ((1,2),(2,0),(3,3),(4,3)):
            alias='room'+str(n);store.record_room_open(room_id=n,room_name='Invented (3.00-4.00*)',created_at=self.now-60)
            (source.config(alias)).write_text(json.dumps({'ruleset_id':mode,'sr_min':3,'sr_max':4,'countdown_seconds':20,'required_mods':[],'allowed_mods':[]}))
            self.status['rooms'][alias]={'room_id':n,'healthy':True,'observed_at':self.now,'state':'PLAYING'if n==1 else 'IDLE','players':2,
                'current_map':{'beatmap_id':51945 if n==1 else 100+n,'label':'Known chart','stars':3.5,'hit_length':80,'total_length':100},
                'queue':[],'queue_known':True,'match_started_at':self.now-20 if n==1 else None}
        (self.root/'state/rolling/public-status.json').write_text(json.dumps(self.status))
        self.enterContext(patch.object(self.analytics,'service_status',return_value={'online':True,'started_at':self.now-120}))
        self.enterContext(patch.object(self.live,'display_row',return_value={'beatmapset_id':13676,'title':'Catch chart','artist':'Artist','version':'Native Catch','sr':3.5,'total_length':100,'hit_length':80}))
    def run_export(self):return self.live.run(public=self.root/'public',cache_path=self.root/'cache.json',peak_path=self.root/'peak.json',now=self.now)
    def test_statistics_remain_available_before_optional_catch_tables_exist(self):
        from bot.ppstats import PPStatsStore
        PPStatsStore(str(self.analytics.DB))
        PPStatsStore(str(self.analytics.DB),ruleset_id=3)
        with sqlite3.connect(self.analytics.DB)as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='autohost_pp_achievements_catch'").fetchone())
        with patch.object(self.analytics,'read_network_statistics',return_value=None):
            stats,_,_=self.analytics.run(public=self.root/'public',state=self.root/'analytics',now=self.now)
        self.assertTrue(stats['available'])
        self.assertIn('catch',stats['scopes'])
    def test_populated_pp_history_exports_all_three_rulesets_without_scope_leaks(self):
        from bot.ppstats import PPStatsStore
        from bot.catch_ppstats import CatchPPStatsStore
        for ruleset,room,gain in ((0,2,1.),(3,3,2.),(2,1,3.)):
            store=(CatchPPStatsStore if ruleset==2 else PPStatsStore)(str(self.analytics.DB),ruleset_id=ruleset)
            store.record_room_mode(room)
            store.record_achievements(room_id=room,playlist_item_id=room,beatmap_id=51945,
                achievements=[{'user_id':9001,'name':'Invented player','rank':10,'gain':gain,'pp':100,'accuracy':98.}])
        with sqlite3.connect(self.analytics.DB)as db:
            db.execute("UPDATE autohost_rooms SET initial_name='Invented Mania 7K' WHERE room_id=3")
        with patch.object(self.analytics,'read_network_statistics',return_value=None):
            stats,_,_=self.analytics.run(public=self.root/'public',state=self.root/'analytics',now=self.now)
        self.assertTrue(stats['available'])
        for scope,gain in (('all',6.),('standard',1.),('mania_7k',2.),('catch',3.)):
            group=stats['scopes'][scope]['all']
            self.assertEqual(group['totals']['pp'],gain,scope)
            self.assertEqual(group['leaders'][0]['username'],'Invented player')
    def test_catch_card_has_correct_type_art_settings_and_approximate_progress(self):
        data=self.run_export();self.assertTrue(data['available']);self.assertEqual(len(data['rooms']),4)
        row=data['rooms'][0];self.assertEqual((row['type'],row['label'],row['guide']),('catch','Catch fixed','lobbies:catch'))
        self.assertEqual(row['settings']['countdown_seconds'],20)
        self.assertEqual(row['current_map']['beatmapset_id'],13676)
        self.assertEqual(row['playback']['started_at'],self.now-20)
        self.assertEqual([r['type']for r in data['rooms'][1:]],['standard','mania_7k','mania_4k'])
    def test_unknown_provider_total_never_uses_catch_hit_span_for_progress(self):
        self.status['rooms']['room1']['current_map'].pop('total_length')
        (self.root/'state/rolling/public-status.json').write_text(json.dumps(self.status))
        self.live.display_row.return_value={**self.live.display_row.return_value,'total_length':None}
        row=self.run_export()['rooms'][0];self.assertEqual(row['type'],'catch');self.assertIsNone(row['playback'])

    def test_failed_ledger_read_keeps_known_catch_label_without_claiming_it_online(self):
        with patch.object(self.analytics,'readonly',side_effect=sqlite3.OperationalError('invented unavailable ledger')):
            data=self.run_export()
        self.assertFalse(data['available']);row=data['rooms'][0]
        self.assertEqual((row['type'],row['state']),('catch','STALE'));self.assertIsNone(row['players'])
