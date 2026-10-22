"""Standalone hub kits must carry lazy mode/greeting validation dependencies."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poolrotater_host.network.hub_service import prepare


class ManagedHubPackageClosure(unittest.TestCase):
    def test_fresh_assembly_validates_modes_and_greeting_defaults_without_source_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            kit = Path(temporary) / 'kit'
            receipt = prepare(kit)
            runtime = kit / 'runtime'
            self.assertEqual(hashlib.sha256((runtime / 'inventory.json').read_bytes()).hexdigest(), receipt['inventory_sha256'])
            inventory = json.loads((runtime / 'inventory.json').read_text())['files']
            for name in ('bot/mode.py', 'bot/custom_greetings.py'):
                self.assertEqual((runtime / name).read_bytes(), (ROOT / name).read_bytes())
                self.assertEqual(hashlib.sha256((runtime / name).read_bytes()).hexdigest(), inventory[name])
            code = r'''import sys
from pathlib import Path
runtime=Path(sys.argv[1]);sys.path.insert(0,str(runtime))
from poolrotater_host.managed_host.settings import validate,merge
for preset in ('standard-auto','standard-loved','mania-4k','mania-7k','catch-fixed'):
    value={'preset':preset}
    if preset=='catch-fixed':value.update(min_passcount=0,max_map_age_years=0,freestyle=False)
    validate(value)
assert merge({'preset':'standard-auto'},{'custom_join_greeting_changes':{'Invented':'Hello'}})['custom_join_greetings']['Invented']=='Hello'
for name,module in sys.modules.items():
    if name.startswith(('poolrotater_host','bot')) and getattr(module,'__file__',None):
        assert Path(module.__file__).is_relative_to(runtime)
'''
            result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code, str(runtime)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
