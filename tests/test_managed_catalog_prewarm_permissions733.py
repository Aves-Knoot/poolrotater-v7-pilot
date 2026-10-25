"""Resume the protected root-owned catalog prewarm journal safely."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from poolrotater_host.catalog_export import MODES
from poolrotater_host.catalog_store import CatalogStore
from poolrotater_host.config import ConfigError
from poolrotater_host.managed_host.catalogs import prepare_store


class ProtectedPrewarmPermissionsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name)
        self.root=base/'host';self.root.mkdir(mode=0o755)
        self.path=base/'prefix'/'catalog-stores'/'20000000-0000-4000-8000-000000000002'
        self.path.mkdir(mode=0o755,parents=True)
        for name in ('versions','receipts','.staging','cache'):
            (self.path/name).mkdir(mode=0o755)
        self.binding={'format':1,'installation_id':'20000000-0000-4000-8000-000000000002',
            'generation':'30000000-0000-4000-8000-000000000003','host_root':str(self.root),
            'service_uid':957,'store':str(self.path),'fixture':False,'execution':'pilot-live'}
        self.store=CatalogStore(self.root,self.path,self.binding)
        # Model CatalogStore's nonfixture path checks in a disposable tree.
        # The test runner may drop root, so file ownership is the current test
        # UID here; mode/type/link policy remains the protected-store policy.
        def check_path(path,directory=False):
            path=Path(path)
            if any(item.is_symlink() for item in (path,*path.parents)):
                raise ConfigError('catalog paths must not contain symlinks')
            st=path.stat()
            if (st.st_uid!=os.getuid() or st.st_mode&0o022
                    or directory and not stat.S_ISDIR(st.st_mode)
                    or not directory and (not stat.S_ISREG(st.st_mode) or st.st_nlink!=1)):
                raise ConfigError('catalog store ownership/type/permissions changed')
            return path
        self.store._check_path=check_path
        def write_json(path,value,mode=0o644):
            path.write_text(json.dumps(value,sort_keys=True)+'\n')
            path.chmod(mode)
        write_json(self.path/'binding.json',self.binding)
        (self.path/'bootstrap.json').write_bytes(b'catalog-bootstrap-fixture-bytes')
        (self.path/'bootstrap.json').chmod(0o444)
        self.reference=self.store._reference({mode:hashlib.sha256(mode.encode()).hexdigest()
                                               for mode in MODES})
        write_json(self.path/'current.json',self.reference)
        binding={key:self.binding[key] for key in
                 ('installation_id','generation','host_root','service_uid')}
        self.receipt={'format':1,'binding':binding,
            'bootstrap_sha256':self.store.trust.identity,'before':self.reference,
            'after':self.reference,'phase':'complete'}
        self.record=self.path/'.staging/managed-prewarm.json'
        write_json(self.record,self.receipt)

    def _as_admin(self):
        return patch('poolrotater_host.managed_host.catalogs.os.geteuid',return_value=0)

    def test_existing_root_owned_0644_prewarm_receipt_resumes_idempotently(self):
        self.assertEqual(stat.S_IMODE(self.record.stat().st_mode),0o644)
        with self._as_admin(),patch.object(self.store,'stage',side_effect=AssertionError('must not restage')):
            result=prepare_store(self.store,None,owner_lease_held=True)
        self.assertEqual(result['reference'],self.reference)
        self.assertTrue(result['all_modes_prepared'])
        self.assertFalse(result['existing_modes_replaced'])
        self.assertEqual(json.loads(self.record.read_text()),self.receipt)

    def test_group_or_world_writable_prewarm_receipt_refuses(self):
        self.record.chmod(0o666)
        with self._as_admin(),self.assertRaisesRegex(ConfigError,'^managed_catalog_prewarm_requires_review$'):
            prepare_store(self.store,None,owner_lease_held=True)

    def test_symlinked_prewarm_receipt_refuses(self):
        target=self.path/'.staging/other.json'
        target.write_text(json.dumps(self.receipt))
        self.record.unlink();self.record.symlink_to(target.name)
        with self._as_admin(),self.assertRaisesRegex(ConfigError,'^managed_catalog_prewarm_requires_review$'):
            prepare_store(self.store,None,owner_lease_held=True)


if __name__=='__main__':
    unittest.main()
