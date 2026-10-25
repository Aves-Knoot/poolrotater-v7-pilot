"""Offline fixtures for the protected six-source public display index builder."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from website.community import build_display_index
from website.community.display_model import DisplayIndex


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _database(path,table,columns,rows):
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE '+table+' ('+columns+')')
        db.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in columns.split(','))+')',rows)


def _catch_payload():
    return {'schema':'catch-native-selection-1',
        'selection':{'map_id':404,'set_id':4404,'variant_id':'4'*64,'title':'Catch Song',
            'artist':'Catch Artist','version':'Catch Difficulty','provider_duration':120,
            'stars':3.75},
        'presentation':{'title_unicode':None,'artist_unicode':None},
        'durations':{'provider_total_seconds':120,'provider_hit_seconds':110,
            'mandatory_scoring_seconds':110},
        'difficulty':{'provider_stars':3.75}}


class DisplayIndexBuilder733Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.sources=[]
        std=self.root/'standard.db'
        _database(std,'beatmaps','beatmap_id INTEGER,artist TEXT,title TEXT,version TEXT,sr REAL,'
            'hit_length INTEGER,beatmapset_id INTEGER,total_length INTEGER,passcount INTEGER,status TEXT,'
            'ranked_date TEXT,ar REAL',[(101,'Standard Artist','Standard Song','Hard',4.2,120,1001,150,12,'ranked','2020-01-01',9)])
        loved=self.root/'loved.db'
        _database(loved,'beatmaps','beatmap_id INTEGER,artist TEXT,title TEXT,version TEXT,sr REAL,'
            'hit_length INTEGER,beatmapset_id INTEGER,total_length INTEGER,passcount INTEGER,status TEXT,'
            'ranked_date TEXT,ar REAL,mode TEXT,convert INTEGER,download_disabled INTEGER,playcount INTEGER',
            [(101,'Standard Artist','Standard Song','Hard',4.2,120,1001,150,0,'loved',None,9,'osu',0,0,7)])
        mania4=self.root/'mania4.db'
        _database(mania4,'maps','beatmap_id INTEGER,artist TEXT,title TEXT,version TEXT,sr REAL,'
            'hit_length INTEGER,mode_int INTEGER,key_count INTEGER,ar REAL',
            [(202,'Four Artist','Four Song','4K',5.1,90,3,4,8)])
        mania7=self.root/'mania7.db'
        _database(mania7,'maps','beatmap_id INTEGER,artist TEXT,title TEXT,version TEXT,sr REAL,'
            'hit_length INTEGER,mode_int INTEGER,key_count INTEGER,ar REAL',
            [(303,'Seven Artist','Seven Song','7K',6.1,100,3,7,8)])
        catch=self.root/'catch.db'
        payload=json.dumps(_catch_payload(),separators=(',',':'))
        _database(catch,'catch_maps','beatmap_id INTEGER,variant_id TEXT,payload TEXT',
            [(404,'4'*64,payload)])
        taiko=self.root/'taiko.db'
        _database(taiko,'taiko_maps','beatmap_id INTEGER,beatmapset_id INTEGER,artist TEXT,title TEXT,'
            'version TEXT,status TEXT,original_mode INTEGER,is_convert INTEGER,source_md5 TEXT,'
            'source_sha256 TEXT,taiko_nm_variant TEXT,sr REAL,hit_length INTEGER,max_pp REAL,'
            'calculator_id TEXT,difficulty_version INTEGER,source_commit TEXT,background_url TEXT,'
            'bpm_min REAL,bpm_max REAL,hit_count INTEGER,big_hits INTEGER,centre_hits INTEGER,rim_hits INTEGER,'
            'peak_hits_per_second REAL,mean_hits_per_second REAL,colour_change_fraction REAL,maximum_mono_streak INTEGER',
            [(505,5005,'Taiko Artist','Taiko Song','Taiko','ranked',1,0,'a'*32,'b'*64,'c'*64,
              5.25,95,180,'calc',1,'d'*40,None,150,170,400,80,90,70,8.5,4.5,.2,3)])
        for mode,table,path in (
            ('standard','beatmaps',std),('loved','beatmaps',loved),
            ('mania_4k','maps',mania4),('mania_7k','maps',mania7),
            ('catch','catch_maps',catch),('taiko','taiko_maps',taiko)):
            self.sources.append({'mode':mode,'table':table,'path':str(path),'sha256':_sha(path)})
        self.inputs=self.root/'inputs.json'
        self.output=self.root/'display.sqlite3'
        self._write_inputs()

    def _write_inputs(self):
        self.inputs.write_text(json.dumps({'format':1,'sources':self.sources}))

    def test_builds_six_verified_modes_and_matches_display_reader(self):
        before={row['path']:row['sha256'] for row in self.sources}
        report=build_display_index.build(self.inputs,self.output)
        self.assertEqual(report['total_rows'],5)
        self.assertEqual(report['rows_by_mode'],{
            'catch':1,'mania_4k':1,'mania_7k':1,'standard':1,'taiko':1})
        self.assertEqual(report['identical_duplicates_ignored'],1)
        self.assertTrue(self.output.stat().st_mode & 0o444)
        self.assertFalse(self.output.stat().st_mode & 0o222)
        self.assertEqual(_sha(self.output),report['sha256'])
        with DisplayIndex(self.output,owner_uid=os.geteuid()) as index:
            self.assertEqual(index.lookup('standard-auto',101)['beatmapset_id'],1001)
            self.assertEqual(index.lookup('standard-fixed',101)['total_length'],150)
            self.assertEqual(index.lookup('mania-4k',202)['artist'],'Four Artist')
            self.assertEqual(index.lookup('mania-7k',303)['sr'],6.1)
            self.assertEqual(index.lookup('catch-fixed',404)['title'],'Catch Song')
            self.assertEqual(index.lookup('taiko-fixed',505)['hit_length'],95)
        for path,digest in before.items():
            self.assertEqual(_sha(path),digest)
            self.assertFalse(Path(path+'-wal').exists())
            self.assertFalse(Path(path+'-shm').exists())

    def test_rejects_wrong_digest_and_incomplete_mode_manifest_without_output(self):
        self.sources[0]['sha256']='0'*64;self._write_inputs()
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            build_display_index.build(self.inputs,self.output)
        self.assertFalse(self.output.exists())
        self.sources[0]['sha256']=_sha(self.sources[0]['path'])
        self.sources.pop();self._write_inputs()
        with self.assertRaisesRegex(ValueError,'all six'):
            build_display_index.build(self.inputs,self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_unapproved_source_columns(self):
        path=Path(self.sources[0]['path'])
        with sqlite3.connect(path) as db:
            db.execute('ALTER TABLE beatmaps ADD COLUMN player_name TEXT')
        self.sources[0]['sha256']=_sha(path);self._write_inputs()
        with self.assertRaisesRegex(ValueError,'columns are not allowed'):
            build_display_index.build(self.inputs,self.output)
        self.assertFalse(self.output.exists())

    def test_conflicting_standard_and_loved_duplicate_fails_closed(self):
        path=Path(self.sources[1]['path'])
        with sqlite3.connect(path) as db:db.execute("UPDATE beatmaps SET title='Conflicting Song'")
        self.sources[1]['sha256']=_sha(path);self._write_inputs()
        with self.assertRaisesRegex(ValueError,'conflicting display metadata'):
            build_display_index.build(self.inputs,self.output)
        self.assertFalse(self.output.exists())


if __name__=='__main__':unittest.main()
