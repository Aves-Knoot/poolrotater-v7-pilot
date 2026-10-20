"""A fresh Catch host can initialize shared feedback before its first worker."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import os,sqlite3,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from poolrotater_host.layout import setup
from poolrotater_host.ratings.migration import migrate_ledger
from bot.scoped_ratings import ScopedRatingStore
from bot.ratings import RatingStore

class FreshCatchStateTests(unittest.TestCase):
    def test_cold_catch_observation_can_outlast_the_legacy_single_room_window(self):
        self._late_ready('catch-fixed',True)

    def test_standard_startup_keeps_its_existing_bounded_observation(self):
        self._late_ready('standard-fixed',False)

    def _late_ready(self,preset,accepted):
        from poolrotater_host import pilot_operations as ops
        from poolrotater_host.config import defaults,ConfigError
        now=[0.];state={'ActiveState':'active','MainPID':123}
        def advance(_):now[0]=151.
        with patch.object(ops,'_control',return_value=defaults([preset])), \
                patch.object(ops,'_startup_private_profile',return_value=None), \
                patch.object(ops,'_service_state',return_value=state), \
                patch.object(ops,'_service_observation',side_effect=[None,{'phase':'running','room_count':1}]), \
                patch.object(ops.time,'monotonic',side_effect=lambda:now[0]), \
                patch.object(ops.time,'sleep',side_effect=advance):
            if accepted:self.assertEqual(ops._wait_ready({'root':Path('/invented/first')},{'service_uid':991}),state)
            else:
                with self.assertRaisesRegex(ConfigError,'observation timed out'):
                    ops._wait_ready({'root':Path('/invented/first')},{'service_uid':991})

    def test_stopped_migration_prepares_catch_scope_before_worker_reads(self):
        with tempfile.TemporaryDirectory()as td:
            root=Path(td)/'host';setup(root,['catch-fixed']);mask=os.umask(0o077)
            try:
                result=migrate_ledger(root);self.assertTrue(result['capture_enabled'])
                native=ScopedRatingStore(str(root/'state/controller-ratings.db'),initialize=False,ruleset_id=2)
                self.assertEqual(native.tally(101),(0,0))
                native.rate(101,7,1,map_origin='catch-native-nm',context={'ruleset_id':2})
                self.assertEqual(migrate_ledger(root)['sequence'],1)
                self.assertEqual(native.tally(101),(1,0))
                with sqlite3.connect(native.path)as db:
                    self.assertTrue(db.execute("SELECT 1 FROM sqlite_master WHERE name='autohost_lobby_time_catch'").fetchone())
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM map_ratings').fetchone()[0],0)
            finally:os.umask(mask)
    def test_existing_standard_migration_does_not_initialize_catch(self):
        with tempfile.TemporaryDirectory()as td:
            root=Path(td)/'host';setup(root,['standard-fixed']);mask=os.umask(0o077)
            try:
                migrate_ledger(root)
                with sqlite3.connect(root/'state/controller-ratings.db')as db:
                    self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='map_ratings_catch'").fetchone())
            finally:os.umask(mask)
