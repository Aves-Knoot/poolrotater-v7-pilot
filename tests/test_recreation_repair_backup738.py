"""Stopped raw/WAL preservation and private canonical SQLite recovery copies."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import os,sqlite3,tempfile,unittest,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from poolrotater_host.config import ConfigError
from poolrotater_host.layout import read_json
from poolrotater_host.recreation_repair_backup import snapshot

class Backup(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'runtime';self.root.mkdir()
        self.prefix=Path(temporary.name)/'code';self.prefix.mkdir()
        for name in ('config','secrets','state'):(self.root/name).mkdir(mode=0o700)
        (self.root/'config/host.json').write_text('{"invented":"settings"}')
        (self.root/'secrets/token.json').write_text('{"invented":"not-a-real-token"}')
        space=patch('poolrotater_host.recreation_repair_backup.os.statvfs',return_value=SimpleNamespace(f_bavail=8*1024**3,f_frsize=1))
        space.start();self.addCleanup(space.stop)
        self.operation=str(uuid.uuid4());self.binding={'installation_id':str(uuid.uuid4()),'before_sha256':'a'*64}
    def backup(self):
        return snapshot(self.root,self.prefix,os.geteuid(),self.operation,self.binding)
    def test_raw_sqlite_wal_is_preserved_and_canonical_backup_contains_committed_rows(self):
        path=self.root/'state/ratings.db';connection=sqlite3.connect(path)
        try:
            connection.execute('PRAGMA journal_mode=WAL');connection.execute('CREATE TABLE votes(value TEXT)')
            connection.execute('INSERT INTO votes VALUES (?)',('invented vote',));connection.commit()
            before={p.name:p.read_bytes()for p in path.parent.iterdir()}
            result=self.backup();directory=Path(result['path']);manifest=read_json(directory/'manifest.json')
            self.assertEqual(result['sqlite_databases'],1)
            self.assertEqual({p.name:p.read_bytes()for p in path.parent.iterdir()},before)
            key=next(iter(manifest['sqlite']))
            with sqlite3.connect(directory/'sqlite'/key)as restored:
                self.assertEqual(restored.execute('SELECT * FROM votes').fetchall(),[('invented vote',)])
            self.assertEqual((directory/'raw/state/ratings.db-wal').read_bytes(),before['ratings.db-wal'])
            self.assertFalse(directory.stat().st_mode&0o077)
            self.assertEqual((directory/'raw/secrets/token.json').stat().st_mode&0o777,0o600)
            self.assertTrue(self.backup()['reused'])
        finally:connection.close()
    def test_source_symlink_refuses_without_reading_target(self):
        (self.root/'state/link').symlink_to('/etc/passwd')
        with self.assertRaises(ConfigError):self.backup()
        self.assertFalse((self.prefix/'recreation-repair-backups'/self.operation).exists())
    def test_tampered_existing_backup_is_not_reused_or_overwritten(self):
        result=self.backup();path=Path(result['path'])/'raw/config/host.json';path.write_text('changed')
        with self.assertRaises(ConfigError):self.backup()
        self.assertEqual(path.read_text(),'changed')
    def test_space_reserve_failure_does_not_publish_partial_backup(self):
        with patch('poolrotater_host.recreation_repair_backup.RESERVE',2**63):
            with self.assertRaises(ConfigError):self.backup()
        self.assertFalse((self.prefix/'recreation-repair-backups'/self.operation).exists())
