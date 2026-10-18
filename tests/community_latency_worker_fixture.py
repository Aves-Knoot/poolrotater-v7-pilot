"""Real room processes/RPC/full catalogs; invented external provider boundaries."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from tests import test_rolling as native

ROOT = Path(__file__).resolve().parents[1]


class CommunityWorkerLatencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='community-latency-workers-')
        cls.root = Path(cls.temp.name)
        release = cls.root/'v7.1.13'
        shutil.copytree(ROOT/'candidate/source', release)
        native.seal(release, 'v7.1.13')
        cls.releases = [release]

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    cleanup_supervisor = native.ProcessIntegrationTests.cleanup_supervisor
    start = native.ProcessIntegrationTests.start

    def setUp(self):
        native.ProcessIntegrationTests.setUp(self)
        self.counts = {}
        for mode in ('standard', 'mania4', 'mania7'):
            with sqlite3.connect((ROOT/'inputs/full-catalogs'/mode/'catalog.db').as_uri()+'?mode=ro', uri=True) as db:
                self.counts[mode] = db.execute('SELECT COUNT(*) FROM '+('beatmaps' if mode == 'standard' else 'maps')).fetchone()[0]
        self.assertGreater(self.counts['standard'], 100000)
        for alias, cfg in self.supervisor.configs.items():
            mode = 'standard' if cfg['ruleset_id'] == 0 else 'mania'+str(cfg['mania_keys'])
            catalog = ROOT/'inputs/full-catalogs'/mode
            if mode == 'standard':
                cfg['farm_db'] = str(catalog/'catalog.db')
                cfg['standard_dt_db'] = str(catalog/'dt.db')
            else:
                cfg['mania_db'] = str(catalog/'catalog.db')
                cfg['mania_farm_db'] = str(catalog/'farm.db')
                cfg['mania_convert_db'] = ''
            cfg.update(background_player_profiles=True, pool_limit=1200, sr_min=3, sr_max=5,
                       chat_poll_seconds=.5, chat_poll_playing_seconds=.5, freestyle=True)
            path = self.runtime/'instances'/(alias+'.json')
            saved = json.loads(path.read_text())
            saved.update({k:v for k,v in cfg.items() if not k.startswith('_')})
            path.write_text(json.dumps(saved))

    def wait(self, predicate, label, seconds=8):
        deadline = time.monotonic()+seconds
        while not predicate():
            if time.monotonic() > deadline: self.fail(label)
            time.sleep(.02)

    def test_occupied_standard_answers_during_slow_profile_and_4k_keeps_running(self):
        standard, mania = self.start('room1'), self.start('room4')
        entered, release = threading.Event(), threading.Event()
        # Release the invented read before supervisor teardown/authority cleanup.
        self.addCleanup(release.set)
        api = self.supervisor.api
        original_user = api.user
        def slow(uid, **kwargs):
            if uid == 7:
                entered.set()
                if not release.wait(10): raise AssertionError('slow profile was not released')
            return original_user(uid, **kwargs)
        api.user = slow
        rid = standard.status['room_id']
        channel = self.hub.rooms[rid]['chat_channel_id']
        self.hub.shared.route('UserJoined', {'room_id': rid, 'user_id': 7})
        self.wait(entered.is_set, 'optional profile job did not start')
        since = standard.status_at
        command_at = time.monotonic()
        message = {'message_id': 900001, 'sender_id': 7, 'content': '.help'}
        def messages(cid, **kwargs):
            return [message] if cid == channel and (kwargs.get('since') or 0) < message['message_id'] else []
        api.chat_messages = messages
        self.wait(lambda: any(cid == channel and '/commands' in text for cid,text in api.messages),
                  'command response blocked behind optional profile', seconds=5)
        elapsed = time.monotonic()-command_at
        self.assertFalse(release.is_set())
        self.wait(lambda: standard.status_at > since, 'room heartbeat blocked behind optional profile')
        self.assertIsNone(standard.process.poll()); self.assertIsNone(mania.process.poll())
        self.assertEqual(standard.status['players'], 1)
        self.assertIn(mania.status['room_id'], self.hub.rooms)
        release.set()
        self.wait(lambda: standard.status_at > since+.1, 'room did not continue after profile result')
        print('NATIVE_COMMUNITY_RESPONSIVENESS '+json.dumps({'ok':True,'response_seconds':elapsed,
              'full_catalog_rows':self.counts,'background_profile_still_waiting':True,
              'standard_and_4k_workers_alive':True,'external_provider_invented':True}))


if __name__ == '__main__': unittest.main()
