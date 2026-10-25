"""Hub Store permits only a disappearing optional SQLite sidecar race."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from poolrotater_host.network.contracts import NetworkError
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.store import Store


class NetworkStoreSidecarRaceTests(unittest.TestCase):
    def _store(self, tmp):
        root = Path(tmp)
        initialize(root)
        return Store(root)

    def test_disappearing_optional_sidecars_are_ignored_after_lexists(self):
        for suffix in ('-wal', '-shm', '-journal'):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                store = self._store(tmp)
                sidecar = Path(str(store.path) + suffix)
                sidecar.write_bytes(b'transient sqlite sidecar')
                sidecar.chmod(0o600)
                original_lstat = Path.lstat

                def disappear_on_lstat(path):
                    if path == sidecar and sidecar.exists():
                        sidecar.unlink()
                    return original_lstat(path)

                with patch.object(Path, 'lstat', new=disappear_on_lstat):
                    store._files()
                self.assertFalse(sidecar.exists())

    def test_missing_or_disappearing_main_database_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.path.unlink()
            with self.assertRaises(FileNotFoundError):
                store._files()

            original_lstat = Path.lstat
            def disappear_main(path):
                if path == store.path:
                    raise FileNotFoundError(path)
                return original_lstat(path)
            with patch.object(Path, 'lstat', new=disappear_main), \
                 patch('poolrotater_host.network.store.os.path.lexists',
                       side_effect=lambda path: Path(path) == store.path):
                with self.assertRaises(FileNotFoundError):
                    store._files()

    def test_existing_unsafe_sidecar_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            target = Path(tmp) / 'outside'
            target.write_text('x')
            Path(str(store.path) + '-wal').symlink_to(target)
            with self.assertRaises(NetworkError) as caught:
                store._files()
            self.assertEqual(caught.exception.code, 'unsafe_hub_database')


if __name__ == '__main__':
    unittest.main()
