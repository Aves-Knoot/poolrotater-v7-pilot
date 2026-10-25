"""Standalone service kits must carry every Taiko preview/validation dependency."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from poolrotater_host.network.hub_service import prepare


class TaikoServiceAssetsTests(unittest.TestCase):
    def test_fresh_hub_kit_imports_taiko_without_checkout_fallback(self):
        scratch=Path.cwd()/'scratch/taiko-hub-assets733'
        scratch.mkdir(mode=0o700,parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            destination=Path(temporary)/'kit'
            result=prepare(destination)
            self.assertTrue(result['ok'])
            runtime=destination/'runtime'
            inventory=json.loads((runtime/'inventory.json').read_text())
            for name in ('bot/taiko_pool.py','bot/taiko_difficulty.py',
                         'poolrotater_host/catalog_taiko.py'):
                self.assertEqual(inventory['files'][name],hashlib.sha256((runtime/name).read_bytes()).hexdigest())
            script='''import sys\nfrom pathlib import Path\nroot=Path(sys.argv[1]);sys.path.insert(0,str(root))\nfrom bot.taiko_pool import TaikoPool\nfrom bot.taiko_difficulty import read_calibration\nfrom poolrotater_host.bookings.preview import ExportPreviewProvider\nassert Path(sys.modules['bot.taiko_pool'].__file__).is_relative_to(root)\nprint('standalone-taiko-assets-ok')\n'''
            done=subprocess.run([sys.executable,'-I','-S','-B','-c',script,str(runtime)],
                                capture_output=True,text=True,timeout=20)
            self.assertEqual(done.returncode,0,done.stderr)
            self.assertIn('standalone-taiko-assets-ok',done.stdout)

    def test_full_taiko_preview_uses_qualified_ruleset_pool(self):
        from poolrotater_host.bookings.preview import ExportPreviewProvider
        catalog=Path(__file__).resolve().parents[2]/'catalog/taiko-v1'
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'exports';root.mkdir(mode=0o700)
            shutil.copytree(catalog,root/'taiko')
            provider=ExportPreviewProvider(root)
            for preset in ('taiko-fixed','taiko-auto'):
                result=provider(preset,{'sr_min':3.,'sr_max':4.,
                    'selection_filters':{'length':{'min':60.,'max':120.,'mode':'hard'}}})
                self.assertGreater(result['count'],0)
                for row in result['samples']:
                    self.assertLessEqual(row['sr'],4.);self.assertGreaterEqual(row['sr'],3.)
                    self.assertLessEqual(row['hit_length'],120.)
                    self.assertGreaterEqual(row['hit_length'],60.)
            self.assertTrue(all(entry['pool'].taiko_mode for entry in provider._base_pool_cache.values()))
