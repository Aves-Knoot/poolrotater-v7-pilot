"""SQLite optional-sidecar replacement races never hide an unsafe main DB."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from poolrotater_host.diagnostic_logs import common
from poolrotater_host.network.contracts import NetworkError


class DatabaseSidecarRaceTests(unittest.TestCase):
    def _main(self, root):
        path = Path(root) / 'managed-host.sqlite3'
        path.write_bytes(b'not queried by this path')
        path.chmod(0o600)
        return path

    def test_disappearing_optional_sidecars_are_ignored_after_lexists(self):
        for suffix in ('-wal', '-shm', '-journal'):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                main = self._main(tmp)
                sidecar = Path(str(main) + suffix)
                sidecar.write_bytes(b'sidecar')
                sidecar.chmod(0o600)
                original_lstat = Path.lstat

                def disappear_on_lstat(path):
                    if path == sidecar and sidecar.exists():
                        sidecar.unlink()
                    return original_lstat(path)

                with patch.object(Path, 'lstat', new=disappear_on_lstat):
                    common.check_database(main)
                self.assertFalse(sidecar.exists())

    def test_missing_or_disappearing_main_database_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'missing.sqlite3'
            with self.assertRaises(FileNotFoundError):
                common.check_database(missing)

            original_lstat = Path.lstat
            def disappear_main(path):
                if path == missing:
                    raise FileNotFoundError(path)
                return original_lstat(path)
            with patch.object(Path, 'lstat', new=disappear_main), \
                 patch.object(common.os.path, 'lexists', side_effect=lambda path: Path(path) == missing):
                with self.assertRaises(FileNotFoundError):
                    common.check_database(missing)

    def test_existing_unsafe_sidecar_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = self._main(tmp)
            target = Path(tmp) / 'outside'
            target.write_text('x')
            Path(str(main) + '-journal').symlink_to(target)
            with self.assertRaises(NetworkError) as caught:
                common.check_database(main)
            self.assertEqual(caught.exception.code, 'unsafe_log_database')


if __name__ == '__main__':
    unittest.main()
