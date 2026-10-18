"""Real administrator/service UID checks in the provisioning namespace."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('provisioning')
from pathlib import Path
import os
import unittest
from unittest.mock import patch
from poolrotater_host.network import resources,operations
from poolrotater_host.network.contracts import NetworkError
from poolrotater_host.config import ConfigError

class ResourceFixture(unittest.TestCase):
    def setUp(self):
        self.base=Path('/runtime')/self._testMethodName;self.base.mkdir(mode=0o755)
        self.assertFalse(Path('/var/lib/poolrotater-v7-pilot/owner').exists())
        self.assertEqual(os.geteuid(),0)

    def test_create_and_idempotent_exact_root_owned_slice(self):
        self.assertTrue(resources.ensure(self.base));self.assertFalse(resources.ensure(self.base))
        p=self.base/resources.NAME
        self.assertEqual(p.stat().st_uid,0);self.assertEqual(p.stat().st_mode&0o777,0o644)
        self.assertEqual(p.read_text(),resources.TEXT)

    def test_concurrent_or_unknown_slice_never_overwritten(self):
        p=self.base/resources.NAME;p.write_text('foreign');p.chmod(0o644)
        with self.assertRaises(NetworkError):resources.ensure(self.base)
        self.assertEqual(p.read_text(),'foreign')

    def test_symlink_or_writable_authority_refused(self):
        p=self.base/resources.NAME;p.symlink_to('/dev/null')
        with self.assertRaises(ConfigError):resources.ensure(self.base)
        p.unlink();resources.ensure(self.base);p.chmod(0o666)
        with self.assertRaises(ConfigError):resources.ensure(self.base)

    def test_service_uid_cannot_install_resource_units(self):
        os.seteuid(1000)
        try:
            with self.assertRaises(NetworkError):resources.ensure(self.base)
        finally:os.seteuid(0)

    def test_exact_legacy_and_new_network_units_admitted_custom_unit_refused(self):
        with patch.object(operations.pilot,'UNIT_BASE',self.base):
            for instance in ('owner','two'):
                path=self.base/operations.name(instance)
                new=operations.unit_text(instance)
                old=new.replace('Slice=poolrotater-v7-transfer.slice\n','').replace('Nice=17\nCPUQuota=50%','Nice=15')
                for body in (old,new):
                    path.write_text(body);path.chmod(0o644)
                    self.assertEqual(operations._checked_unit(instance),path)
                path.write_text(new+'ExecStartPost=/bin/true\n')
                with self.assertRaises(ConfigError):operations._checked_unit(instance)

if __name__=='__main__':unittest.main(verbosity=2)
