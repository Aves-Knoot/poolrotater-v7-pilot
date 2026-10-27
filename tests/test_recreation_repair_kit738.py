"""Kit entry reuses validated archive primitives and exposes only safe errors."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from pilot import recreation_repair as entry,repair_and_start as primitives
from poolrotater_host.config import ConfigError

class Kit(unittest.TestCase):
    def test_reused_kit_verifier_accepts_only_explicit_version_and_all_exact_bytes(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);runtime=b'invented runtime';(root/'runtime.zip').write_bytes(runtime)
            digest=hashlib.sha256(runtime).hexdigest()
            manifest={'runtime_version':'v7.3.8','runtime_sha256':digest,
                      'files':{'runtime.zip':{'bytes':len(runtime),'sha256':digest}}}
            (root/'release.json').write_text(json.dumps(manifest))
            self.assertEqual(primitives.verify_kit(root,expected_version=entry.EXPECTED_VERSION),manifest)
            with self.assertRaises(primitives.RepairError):primitives.verify_kit(root)
            (root/'runtime.zip').write_bytes(b'changed')
            with self.assertRaises(primitives.RepairError):primitives.verify_kit(root,expected_version=entry.EXPECTED_VERSION)
    def test_safe_console_codes_never_echo_provider_or_filesystem_messages(self):
        self.assertEqual(entry.failure_code(ConfigError('repair_original_backup_missing')),'repair_original_backup_missing')
        for error in (RuntimeError('secret=private-canary'),ConfigError('provider response private-canary'),
                      OSError('private-canary')):
            self.assertNotIn('private-canary',entry.failure_code(error))
    def test_legacy_verification_default_remains_dynamic(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);data=b'fixture';(root/'runtime.zip').write_bytes(data)
            digest=hashlib.sha256(data).hexdigest()
            (root/'release.json').write_text(json.dumps({'runtime_version':'invented',
                'runtime_sha256':digest,'files':{'runtime.zip':{'bytes':len(data),'sha256':digest}}}))
            with patch.object(primitives,'EXPECTED_VERSION','invented'):
                self.assertEqual(primitives.verify_kit(root)['runtime_version'],'invented')
