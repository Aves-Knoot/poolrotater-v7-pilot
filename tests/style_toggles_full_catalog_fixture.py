"""Real full selection catalogs and native workers; invented external services."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace

from bot.standard_pool import StandardPool
from tests import test_rolling as native
from tests import worker_responsiveness_full_catalog_fixture as full

ROOT = Path(__file__).resolve().parents[1]
PREPARED = ROOT / 'validation/owner-pilot/conversion-catalog-qualification/prepared'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def binding():
    return dict(source_sha256=sha(PREPARED / 'standard/catalog.db'),
                dt_sha256=sha(PREPARED / 'standard/dt.db'))


class FullCatalogStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='style-controls-full-')
        cls.root = Path(cls.temp.name)
        release = cls.root / 'v7.1.9'
        source = Path(os.environ.get('POOLROTATER_STYLE_SOURCE',
                                    str(ROOT / 'validation/v7.1.8-maintenance/release/source')))
        for name in native.source_files(source):
            target = release / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        if 'POOLROTATER_STYLE_SOURCE' not in os.environ:
            for name in ('bot/bot.py', 'bot/standard_pool.py', 'bot/languages.py'):
                shutil.copy2(ROOT / name, release / name)
        native.seal(release, 'v7.1.9')
        cls.releases = [release]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    cleanup_supervisor = native.ProcessIntegrationTests.cleanup_supervisor
    start = native.ProcessIntegrationTests.start

    def setUp(self):
        full.FullCatalogWorkerTests.setUp(self)
        self.supervisor.chat_websocket = SimpleNamespace(enabled=True, connected=True, generation=1,
            subscribe=lambda *args: None, unsubscribe=lambda *args: None, start=lambda: None, stop=lambda: None)
        for alias, cfg in self.supervisor.configs.items():
            if cfg['ruleset_id'] != 0:
                continue
            cfg.update(standard_catalog_binding=binding(), sr_min=3.5, sr_max=6,
                       min_passcount=1000, max_length=240, command_spam_burst=100,
                       chat_websocket_enabled=True)
            path = self.runtime / 'instances' / (alias + '.json')
            saved = json.loads(path.read_text())
            saved.update({key: value for key, value in cfg.items() if not key.startswith('_')})
            path.write_text(json.dumps(saved))

    def wait(self, predicate, label, seconds=25):
        end = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() > end:
                logs = '\n'.join(p.read_text()[-5000:] for p in (self.runtime / 'logs').rglob('*.log'))
                self.fail(label + '\n' + logs)
            time.sleep(.05)

    def test_real_catalog_style_union_dt_constraints_and_history(self):
        pool = StandardPool(str(PREPARED / 'standard/catalog.db'),
                            standard_dt_db=str(PREPARED / 'standard/dt.db'),
                            standard_catalog_binding=binding(), sr_min=3.5, sr_max=6,
                            max_length=240, min_passcount=1000, limit=250)
        pool.set_dt_override(False)
        started = time.monotonic()
        self.assertTrue(pool.set_style_toggle('stream', True))
        self.assertTrue(pool.set_style_toggle('jump', True))
        self.assertFalse(any(m.is_dt for m in pool.maps))
        self.assertTrue(pool.set_dt_override(True))
        self.assertTrue(pool.dt_catalog_ready())
        self.assertEqual(pool.active_styles(), ('stream', 'jump'))
        self.assertTrue(any(m.is_dt for m in pool.maps))
        self.assertTrue(all(3.5 <= m.sr <= 6 and m.hit_length <= 240 and m.passcount >= 1000
                            and (m.is_stream or m.is_jump) for m in pool.maps))
        held = [m.beatmap_id for m in pool.maps[:5]]
        pool.reserve_playlist_maps(held)
        seen = []
        for _ in range(150):
            chosen = pool.next_map()
            self.assertIsNotNone(chosen)
            self.assertNotIn(chosen.beatmap_id, held)
            seen.append(chosen)
            pool.record_round(chosen.beatmap_id)
        self.assertTrue(any(m.is_stream for m in seen))
        self.assertTrue(any(m.is_jump for m in seen))
        self.assertTrue(any(m.is_dt for m in seen))
        print(json.dumps(dict(ok=True, full_catalog_rows=self.counts,
                              mixed_stream_jump_and_dt=True, selections=150,
                              selection_seconds=round(time.monotonic()-started, 3))))

    def test_native_chat_controls_keep_queue_and_playing_sibling(self):
        worker = self.start('room1')
        sibling = self.start('room2')
        rid, pid = worker.status['room_id'], worker.process.pid
        sibling_id, sibling_pid = sibling.status['room_id'], sibling.process.pid
        for target in (worker, sibling):
            self.hub.shared.route('UserJoined', {'room_id': target.status['room_id'], 'user_id': 7})
            self.hub.shared.route('MatchStarted', {'room_id': target.status['room_id']})
        self.wait(lambda: all(w.status.get('state') == 'PLAYING' for w in (worker, sibling)), 'matches running')
        visible = worker.status['public']['queue']

        def style():
            return worker.status.get('network', {}).get('display', {}).get('settings', {}).get('selection_style')

        def command(index, text):
            worker.peer.emit('chat', dict(kind='messages', generation=1, channel_id=worker.broker.channel,
                                          messages=[dict(message_id=90000+index, sender_id=7, content=text)]))

        command(1, '.stream on')
        self.wait(lambda: style() == 'stream', 'stream enabled')
        command(2, '.aim on')
        self.wait(lambda: style() == 'stream+jump', 'stream and aim combined')
        command(3, '.pool')
        self.wait(lambda: any('mode stream+jump' in row[1] for row in self.supervisor.api.messages), 'combined pool reply')
        command(4, '.t')
        self.wait(lambda: any('left—' in row[1] for row in self.supervisor.api.messages), 'short remaining-time reply')
        command(5, '.jump off')
        self.wait(lambda: style() == 'stream', 'aim off retains stream')
        self.assertEqual(worker.status['public']['queue'], visible)
        self.assertEqual((worker.status['room_id'], worker.process.pid), (rid, pid))
        self.assertEqual((sibling.status['room_id'], sibling.process.pid), (sibling_id, sibling_pid))
        self.assertEqual(sibling.status['state'], 'PLAYING')
        self.assertFalse(any(name in ('CloseRoom', 'KickPlayer', 'AbortMatch') for name, _ in self.hub.calls))
        print(json.dumps(dict(ok=True, full_catalog_rows=self.counts, native_live_chat_commands=True,
                              queue_unchanged=True, playing_sibling_unchanged=True)))


if __name__ == '__main__':
    unittest.main()
