"""Legacy-code rollback never strands initialized context-format state."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
import sqlite3,tempfile,unittest
from unittest.mock import patch
from contextlib import nullcontext
from poolrotater_host.catch_rollback import require_target
from poolrotater_host.config import ConfigError
from poolrotater_host.layout import setup

class CatchRollbackTests(unittest.TestCase):
    def test_legacy_target_allowed_only_before_catch_preparation(self):
        with tempfile.TemporaryDirectory()as td,patch('poolrotater_host.catch_rollback._identity',return_value=nullcontext()):
            root=Path(td)/'host';prefix=Path(td)/'prefix';prefix.mkdir();setup(root,['standard-fixed'])
            args=(root,prefix,1000,{'version':'v7.2.0'},{'version':'v7.1.20'})
            require_target(*args)
            with sqlite3.connect(root/'state/controller-ratings.db')as db:db.execute('CREATE TABLE map_ratings_catch(x INTEGER)')
            with self.assertRaisesRegex(ConfigError,'compatible controller'):require_target(*args)
            require_target(root,prefix,1000,{'version':'v7.2.0'},{'version':'v7.2.1'})
            with self.assertRaises(ConfigError):require_target(root,prefix,1000,{'version':'v7.2.1'},{'version':'v7.1.20'})
    def test_fresh_catch_topology_and_root_transaction_refuse_old_code(self):
        for presets in (['catch-fixed'],['standard-fixed']):
            with self.subTest(presets=presets),tempfile.TemporaryDirectory()as td,patch('poolrotater_host.catch_rollback._identity',return_value=nullcontext()):
                root=Path(td)/'host';prefix=Path(td)/'prefix';prefix.mkdir();setup(root,presets)
                (prefix/'catch-pilot-transition-private.json').write_text('{}')
                with self.assertRaises(ConfigError):require_target(root,prefix,1000,{'version':'v7.2.0'},{'version':'v7.1.20'})
