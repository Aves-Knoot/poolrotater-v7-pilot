"""Taiko website aggregation and public live-display regression coverage."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def _load_exporter():
    # The deployed analytics job binds a fixed owner source. Unit fixtures use
    # a inert path object and never open a host ledger or catalog.
    source=types.ModuleType('owner_source')
    source.selected=lambda:types.SimpleNamespace(root=Path('/invented/owner'),ledger=Path('/invented/ledger'))
    sys.modules.setdefault('owner_source',source)
    exporter_dir=str(ROOT/'website/statistics_v71')
    if exporter_dir not in sys.path:sys.path.insert(0,exporter_dir)
    return importlib.import_module('website.statistics_v71.analytics_exporter')


def _owner_db():
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.execute('CREATE TABLE autohost_stats_privacy(user_id INTEGER,hidden INTEGER)')
    lobby='(user_id INTEGER,username TEXT,updated_at REAL,first_join_at REAL,total_seconds REAL,visit_count INTEGER,last_join_at REAL,last_leave_at REAL)'
    for table in ('autohost_lobby_time','autohost_lobby_time_mania','autohost_lobby_time_catch','autohost_lobby_time_taiko'):
        db.execute('CREATE TABLE '+table+lobby)
    db.execute('CREATE TABLE autohost_room_players(room_id INTEGER,user_id INTEGER,username TEXT,total_seconds REAL,last_join_at REAL,last_leave_at REAL)')
    db.execute('CREATE TABLE autohost_match_players(room_id INTEGER,user_id INTEGER,username TEXT,created_at REAL,score_submitted INTEGER,beatmap_id INTEGER,accuracy REAL)')
    achievement='(achievement_key TEXT,user_id INTEGER,username TEXT,beatmap_id INTEGER,room_id INTEGER,playlist_item_id INTEGER,score_pp REAL,profile_gain_pp REAL,top_rank INTEGER,accuracy REAL,created_at REAL)'
    for table in ('autohost_pp_achievements','autohost_pp_achievements_mania','autohost_pp_achievements_catch','autohost_pp_achievements_taiko'):
        db.execute('CREATE TABLE '+table+achievement)
    db.execute('CREATE TABLE autohost_matches(beatmap_id INTEGER,map_label TEXT,star_rating REAL,started_at REAL)')
    return db


class TaikoWebsite733Tests(unittest.TestCase):
    def test_owner_statistics_use_taiko_tables_and_keep_privacy_global(self):
        exporter=_load_exporter();db=_owner_db()
        db.executemany('INSERT INTO autohost_stats_privacy VALUES(?,?)',[(7,1),(8,0)])
        db.execute('INSERT INTO autohost_lobby_time_taiko VALUES(8,\'Visible Taiko\',900,300,120,2,700,900)')
        db.execute("INSERT INTO autohost_lobby_time_taiko VALUES(7,'Hidden Taiko',900,300,240,4,700,900)")
        achievement="INSERT INTO autohost_pp_achievements_taiko VALUES(?,?,?,?,?,?,?,?,?,?,?)"
        for uid,name in ((7,'Hidden Taiko'),(8,'Visible Taiko')):
            db.execute(achievement,(f'room:9:item:{uid}:user:{uid}',uid,name,77,9,uid,150,12.5,1,98.0,850))
        db.executemany('INSERT INTO autohost_match_players VALUES(?,?,?,?,?,?,?)',[
            (9,7,'Hidden Taiko',850,1,77,98.0),(9,8,'Visible Taiko',850,1,77,98.0)])
        scopes,_,_,_=exporter.metrics(db,{9:{'type':'taiko'}},1000,set())
        self.assertIn('taiko',scopes)
        group=scopes['taiko']['all']
        self.assertEqual(group['totals']['players'],2)
        self.assertEqual(group['totals']['maps'],2)
        self.assertEqual(group['totals']['pp'],25.0)
        self.assertEqual([p['user_id'] for p in group['leaders']],[8])
        player=group['leaders'][0]
        self.assertEqual(player['time_by_type']['taiko'],120.0)
        self.assertEqual(player['best_gain']['gain'],12.5)
        self.assertEqual(exporter.room_type({'ruleset_id':1,'initial_name':'4K title','last_name':'Standard'}),'taiko')
        db.close()

    def test_network_taiko_participation_has_its_own_scope_and_all_scope(self):
        from website.statistics_v71 import network_statistics as network
        now=2_000_000_000;host='alpha';uid=42
        snapshot={'format':1,'generated_at':now,
          'hosts':[{'host_id':host,'label':'Alpha','consent_at':now-600,'updated_at':now,'enabled':True}],
          'presence':[{'host_id':host,'user_id':uid,'room_id':80,'mode':'taiko','username':'Taiko 42',
                       'started_at':now-120,'ended_at':now-20}],
          'joins':[],'matches':[],'hidden_user_ids':[]}
        raw=network.groups();raw['taiko']['all'][uid]=network.empty_player(uid)
        raw['all']['all'][uid]=network.empty_player(uid)
        combined,views=network.compose(raw,{},(),snapshot,set(),now)
        self.assertIn('taiko',combined)
        self.assertEqual(combined['taiko']['all']['totals']['verified_seconds'],100)
        self.assertEqual(combined['all']['all']['totals']['verified_seconds'],100)
        self.assertEqual(views['hosts'][host]['scopes']['taiko']['all']['leaders'][0]['username'],'Taiko 42')
        self.assertEqual(network.MODES['taiko'],'taiko')

    def test_community_taiko_map_art_and_timing_use_verified_nm_metadata(self):
        from website.community.community_feed import (project, last_seen, fetch_compatible_directory,
            DirectoryV2Unavailable, DirectoryV3Unavailable, FETCH_SECONDS, FRESH_SECONDS, HIDE_SECONDS)
        from website.community.display_model import DisplayIndex
        now=1000;host=str(uuid.uuid4())
        settings={'ruleset_id':1,'keys':None,'auto_difficulty':False,
                  'selection_style':'variety','allowed_mods':['NM'],'required_mods':['NM'],
                  'freestyle':False,'countdown_seconds':15}
        row={'alias':'community-taiko','osu_room_id':801,'preset':'taiko-fixed','state':'playing',
             'occupants':4,'visibility':'public','title':'Community Taiko','min_stars':3.0,'max_stars':5.0,
             'observed_at':990,'code_version':'v7.3.3','catalog_version':'a'*64,
             'current_map':{'beatmap_id':77,'title':'Qualified Taiko NM','stars':4.2},
             'host_id':host,'operator':'Host','age_seconds':10,'freshness':'fresh',
             'join_url':None,'distribution':'self-reported',
             'display':{'settings':settings,'playing_players':2,
                        'match':{'playlist_item_id':21,'beatmap_id':77,'started_at':980,
                                 'clock_rate':1.0,'participants':2,'clock_skew_seconds':0.0}}}
        directory={'ok':True,'protocol_version':2,'server_epoch':str(uuid.uuid4()),
                   'generated_at':now,'rooms':[row],'next_offset':None}
        with tempfile.TemporaryDirectory() as td:
            index_path=Path(td)/'display.db'
            with sqlite3.connect(index_path) as db:
                db.execute('CREATE TABLE display_maps(mode TEXT,beatmap_id INTEGER,beatmapset_id INTEGER,title TEXT,artist TEXT,version TEXT,total_length INTEGER,hit_length INTEGER,sr REAL)')
                # A converted Taiko chart shares the provider ID with its
                # Standard source. The source row's stars must never replace
                # the live Taiko referee's reported difficulty.
                db.execute("INSERT INTO display_maps VALUES('standard',77,9001,'Standard source title','Standard artist','Standard source',180,150,9.8)")
                db.execute("INSERT INTO display_maps VALUES('taiko',77,9001,'Taiko catalog title','Taiko catalog artist','Taiko NM',120,90,7.7)")
            index_path.chmod(0o400)
            with DisplayIndex(index_path,owner_uid=os.geteuid()) as index:
                result=project(directory,now=now,metadata_lookup=index.lookup)
        room=result['rooms'][0]
        self.assertEqual(room['preset'],'taiko-fixed')
        self.assertEqual(room['settings']['ruleset_id'],1)
        self.assertEqual(room['current_map']['stars'],4.2)
        self.assertEqual(room['current_map']['title'],'Qualified Taiko NM')
        self.assertEqual(room['current_map']['artist'],'Taiko catalog artist')
        self.assertEqual(room['current_map']['beatmapset_id'],9001)
        self.assertEqual(room['current_map']['total_length'],120)
        self.assertEqual(room['current_map']['hit_length'],90)
        self.assertEqual(room['current_map']['cover_url'],'https://assets.ppy.sh/beatmaps/9001/covers/cover.jpg')
        self.assertEqual(room['playback']['duration_seconds'],120)
        self.assertEqual(room['playback']['started_at'],980)
        self.assertEqual(room['playback']['clock_rate'],1.0)
        self.assertFalse(room['playback']['nominal_speed'])
        self.assertEqual(room['match_participants'],2)

        # The real public live-card renderer consumes the same scoped output:
        # reported Taiko stars and ruleset survive into SSR while the cover,
        # title, artist, and durations use the Taiko display-index entry.
        import time
        from tests.test_live_presentation import fixture,render
        live_now=int(time.time())
        live_room=dict(room, fresh_until=live_now+600, hide_at=live_now+1800)
        live_room['playback']=dict(room['playback'],started_at=live_now-20,
                                   estimated_end_at=live_now+100)
        page=fixture(live_now)
        page['community']={'schema':1,'available':True,'generated_at':live_now,
                           'expires_at':live_now+60,'rooms':[live_room]}
        html=render(page,'dashboard')
        self.assertIn('Qualified Taiko NM',html)
        self.assertIn('Taiko catalog artist',html)
        self.assertIn('4.20★',html)
        self.assertIn('<dt>Ruleset</dt><dd>osu!taiko</dd>',html)
        self.assertIn('https://assets.ppy.sh/beatmaps/9001/covers/cover.jpg',html)
        self.assertIn('Estimated 0:20 / 2:00',html)
        self.assertIn('data-nominal-speed="false"',html)
        self.assertEqual((FETCH_SECONDS,FRESH_SECONDS,HIDE_SECONDS),(15,600,1800))
        routes=[]
        def fetcher(_ca,_sha,*,resource):
            routes.append(resource)
            if resource.startswith('/v3/'):raise DirectoryV3Unavailable()
            if resource.startswith('/v2/'):raise DirectoryV2Unavailable()
            return {'protocol_version':1}
        self.assertEqual(fetch_compatible_directory('/invented/ca','a'*64,fetcher=fetcher),{'protocol_version':1})
        self.assertEqual(routes,['/v3/rooms?limit=100','/v2/rooms?limit=100','/v1/rooms?limit=100'])
        with tempfile.TemporaryDirectory() as td:
            cached=Path(td)/'rooms.json';cached.write_text(json.dumps(result))
            retained=last_seen(cached,now=1100)
            self.assertEqual(retained['rooms'][0]['state'],'playing')
            self.assertEqual(retained['rooms'][0]['preset'],'taiko-fixed')
            expired=last_seen(cached,now=1700)
            self.assertEqual(expired['rooms'][0]['state'],'unavailable')
            self.assertIsNone(expired['rooms'][0]['current_map'])
        row['display']['match']['clock_rate']=1.5
        with self.assertRaises(ValueError):project(directory,now=now)
        row['display']['match']['clock_rate']=1.0;row['preset']='taiko-auto';row['display']['settings']['auto_difficulty']=True
        auto=project(directory,now=now,metadata_lookup=lambda preset,bid:{'beatmapset_id':9001,'total_length':120})
        self.assertEqual(auto['rooms'][0]['preset'],'taiko-auto')
        self.assertTrue(auto['rooms'][0]['settings']['auto_difficulty'])

    def test_public_ssr_cards_filter_and_sidebar_keep_taiko_type_and_art(self):
        from tests.test_live_presentation import fixture,render
        value=fixture();now=value['status']['generated_at']
        owner=value['status']['rooms'][0];owner['type']='taiko';owner['settings']['ruleset_id']=1;owner['label']='Owner Taiko'
        room=value['community']['rooms'][0];room['preset']='taiko-fixed';room['settings']['ruleset_id']=1
        room['settings']['keys']=None;room['playback']['clock_rate']=1.0
        home=render(value,'home')
        dashboard=render(value,'dashboard')
        self.assertIn('osu!taiko',home);self.assertIn('TAIKO',home)
        self.assertIn('data-room-type="taiko"',dashboard)
        self.assertIn('data-room-filter="taiko"',dashboard)
        self.assertIn('https://assets.ppy.sh/beatmaps/209/covers/cover.jpg',dashboard)
        self.assertIn('aria-label="Estimated song progress"',dashboard)
        self.assertIn('<dt>Ruleset</dt><dd>osu!taiko</dd>',dashboard)

    def test_render_sources_keep_owner_and_community_classifiers_dynamic(self):
        dashboard=(ROOT/'website/live/template/dashboard.php').read_text()
        php=(ROOT/'website/community/community.php').read_text()
        js=(ROOT/'website/live/template/script.js').read_text()
        feed=(ROOT/'website/community/community.js').read_text()
        self.assertIn('prDashboardRoomType($room)',dashboard)
        self.assertIn("'taiko-fixed'",php)
        self.assertIn("'taiko-fixed'",feed)
        self.assertIn("startsWith('taiko-')?'taiko'",js)


if __name__=='__main__':
    unittest.main()
