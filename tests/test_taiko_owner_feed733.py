"""Owner wiki feeds retain all configured rulesets and their safe map source."""
import importlib.util
import os
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
import importlib.machinery

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


analytics = load('taiko_owner_analytics733', ROOT/'deployment/wiki/analytics.py')
live = load('taiko_owner_live733', ROOT/'deployment/wiki/live.py')


def load_network_exporter(name, owner=True):
    source = types.SimpleNamespace(owner=owner, root=Path('/tmp/owner-feed-test'),
        service='owner-feed-test.service', ledger=Path('/tmp/owner-feed-test/ratings.db'),
        catalog=lambda filename:Path('/tmp/owner-feed-test')/filename,
        config=lambda alias:Path('/tmp/owner-feed-test')/(alias+'.json'),
        room_log=lambda alias:Path('/tmp/owner-feed-test')/(alias+'.log'))
    owner_source=types.ModuleType('owner_source')
    owner_source.selected=lambda:source
    from website.statistics_v71 import network_statistics
    with patch.dict('sys.modules', {'owner_source':owner_source,'network_statistics':network_statistics}):
        return load(name, ROOT/'website/statistics_v71/analytics_exporter.py')


def load_live_using_network_exporter(name, exporter):
    with tempfile.TemporaryDirectory() as td:
        live_path=Path(td)/'live.py'
        live_path.write_text((ROOT/'deployment/wiki/live.py').read_text())
        spec=importlib.util.spec_from_file_location(name,live_path)
        module=importlib.util.module_from_spec(spec)
        actual_loader=importlib.machinery.SourceFileLoader
        def redirect_loader(loader_name,path):
            if path == '/usr/local/sbin/poolrotator-wiki-analytics':
                return actual_loader(loader_name,str(ROOT/'website/statistics_v71/analytics_exporter.py'))
            return actual_loader(loader_name,path)
        owner_source=types.ModuleType('owner_source')
        owner_source.selected=lambda:exporter.SOURCE
        from website.statistics_v71 import network_statistics
        with patch.object(importlib.machinery,'SourceFileLoader',side_effect=redirect_loader):
            with patch.dict('sys.modules', {'owner_source':owner_source,'network_statistics':network_statistics}):
                spec.loader.exec_module(module)
        return module


network_analytics=load_network_exporter('taiko_owner_network_analytics733')
network_live=load_live_using_network_exporter('taiko_owner_network_live733',network_analytics)


class OwnerModeProjectionTests(unittest.TestCase):
    def test_actual_network_exporter_and_live_fallback_share_mode_contract(self):
        self.assertTrue(network_analytics.SOURCE.owner)
        self.assertTrue(network_live.a.SOURCE.owner)
        for producer in (analytics,network_analytics,network_live.a):
            expected = ['standard', 'standard', 'mania_7k', 'mania_4k']
            self.assertEqual([producer.room_mode(None, {}, i) for i in range(4)], expected)
            self.assertEqual(producer.room_mode(None, {'preset':'taiko-auto'}, 0), 'taiko')
            self.assertEqual(producer.room_mode(None, {'preset':'catch-fixed'}, 1), 'catch')
            self.assertEqual(producer.room_mode(None, {'preset':'mania-7k'}, 0), 'mania_7k')
            self.assertEqual(producer.room_mode(None, {'preset':'taiko-anything'}, 0), 'standard')
            self.assertEqual(producer.room_presentation('standard', 2, {'preset':'standard-auto'}),
                             ('Standard automatic','standard_auto'))

    def test_mode_resolution_preserves_defaults_and_follows_ruleset_one(self):
        expected = ['standard', 'standard', 'mania_7k', 'mania_4k']
        self.assertEqual([analytics.room_mode(None, {}, i) for i in range(4)], expected)
        self.assertEqual(analytics.room_mode(None, {'ruleset_id': 0}, 0), 'standard')
        self.assertEqual(analytics.room_mode(None, {'ruleset_id': 1}, 0), 'taiko')
        self.assertEqual(analytics.room_mode(None, {'ruleset_id': 2}, 3), 'catch')
        self.assertEqual(analytics.room_mode(None, {'ruleset_id': 3, 'keys': 7}, 0), 'mania_7k')
        self.assertEqual(analytics.room_mode(None, {'ruleset_id': 3, 'keys': 4}, 0), 'mania_4k')
        self.assertEqual(analytics.room_mode(1, {}, 2), 'taiko')
        self.assertEqual(analytics.room_mode(None, {'preset':'taiko-auto'}, 0), 'taiko')
        self.assertEqual(analytics.room_mode(None, {'preset':'taiko-fixed'}, 0), 'taiko')
        self.assertEqual(analytics.room_mode(None, {'preset':'catch-fixed'}, 0), 'catch')
        self.assertEqual(analytics.room_mode(None, {'preset':'mania-7k'}, 0), 'mania_7k')
        self.assertEqual(analytics.room_mode(None, {'preset':'taiko-unknown'}, 0), 'standard')
        self.assertEqual(analytics.room_mode(1, {'ruleset_id':0, 'preset':'taiko-auto'}, 2), 'standard')
        self.assertEqual(analytics.room_type({'initial_name':'Taiko room','last_name':'Taiko room','ruleset_id':1}), 'taiko')

    def test_guides_and_settings_preserve_fixed_auto_loved_catch_and_taiko(self):
        self.assertEqual(analytics.room_presentation('standard', 1), ('Standard fixed', 'standard_fixed'))
        self.assertEqual(analytics.room_presentation('mania_7k', 0), ('Mania 7K', 'mania_7k'))
        self.assertEqual(analytics.room_presentation('catch', 0), ('Catch fixed', 'catch'))
        self.assertEqual(analytics.room_presentation('taiko', 0), ('Taiko', 'taiko'))
        self.assertEqual(analytics.room_presentation('standard', 2, {'ruleset_id':0,'auto_difficulty':True}), ('Standard automatic', 'standard_auto'))
        self.assertEqual(analytics.room_presentation('standard', 2, {'preset':'standard-fixed'}), ('Standard fixed', 'standard_fixed'))
        self.assertEqual(analytics.room_presentation('standard', 2, {'preset':'standard-loved'}), ('Standard Loved', 'standard_fixed'))
        self.assertEqual(analytics.room_presentation('standard', 2, {}), ('Standard', 'start'))
        fixed = analytics.public_settings({'ruleset_id':1,'auto_difficulty':False,'sr_min':3,'sr_max':4,'loved_mode':True}, {}, None)
        auto = analytics.public_settings({'ruleset_id':1,'auto_difficulty':True,'sr_min':3,'sr_max':4}, {}, None)
        self.assertEqual((fixed['ruleset_id'], fixed['difficulty'], fixed['loved_mode']), (1, 'Fixed with player votes', True))
        self.assertEqual((auto['ruleset_id'], auto['difficulty']), (1, 'Automatic'))
        for preset, ruleset, keys, difficulty, loved in (
            ('standard-auto',0,None,'Automatic',False), ('standard-fixed',0,None,'Fixed with player votes',False),
            ('standard-loved',0,None,'Fixed with player votes',True), ('catch-fixed',2,None,'Fixed with player votes',False),
            ('mania-4k',3,4,'Fixed with player votes',False), ('mania-7k',3,7,'Fixed with player votes',False),
            ('taiko-fixed',1,None,'Fixed with player votes',False), ('taiko-auto',1,None,'Automatic',False)):
            settings=analytics.public_settings({'preset':preset}, {}, None)
            self.assertEqual((settings['ruleset_id'],settings.get('keys'),settings['difficulty'],settings['loved_mode']),
                             (ruleset,keys,difficulty,loved),preset)

    def test_ruleset_one_is_eligible_for_owner_display_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'display.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE display_maps (mode TEXT, beatmap_id INTEGER, beatmapset_id INTEGER, title TEXT, artist TEXT, version TEXT, total_length REAL, hit_length REAL, sr REAL)')
                db.execute('INSERT INTO display_maps VALUES (?,?,?,?,?,?,?,?,?)', ('taiko', 123, 456, 'Song', 'Artist', 'Taiko', 120, 110, 4.25))
            path.chmod(0o444)
            with patch.object(live, 'DISPLAY', path):
                row = live.display_row('taiko', 123, owner_uid=os.geteuid())
                self.assertEqual(row['beatmapset_id'], 456)
                self.assertIsNone(live.display_row('taiko', 0, owner_uid=os.geteuid()))

    def test_taiko_map_lookup_uses_only_owner_display_index(self):
        calls = []
        source = types.SimpleNamespace(owner=True)
        old_source = getattr(live.a, 'SOURCE', None)
        live.a.SOURCE = source
        try:
            with patch.object(live, 'display_row', side_effect=lambda mode, bid: calls.append((mode, bid)) or {
                'beatmapset_id':456,'title':'Song','artist':'Artist','version':'Taiko',
                'total_length':120,'hit_length':110,'sr':4.25
            }):
                result = live.map_metadata({'beatmap_id':123,'label':'Map #123'}, 'taiko', {}, 1000)
        finally:
            if old_source is None:
                del live.a.SOURCE
            else:
                live.a.SOURCE = old_source
        self.assertEqual(calls, [('taiko', 123)])
        self.assertEqual(result['beatmapset_id'], 456)
        self.assertIn('Artist - Song [Taiko]', result['label'])


if __name__ == '__main__':
    unittest.main()
